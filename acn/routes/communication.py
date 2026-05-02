"""Communication API Routes

Clean Architecture implementation: Route → MessageService → MessageRouter
"""

import uuid

import structlog  # type: ignore[import-untyped]
from a2a.compat.v0_3.types import Message, TextPart  # type: ignore[import-untyped]
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..core.errors import ACN_DEFAULT_RESPONSES, ACNHTTPError, ErrorCode
from ..core.exceptions import AgentNotFoundException, PolicyRejected
from ..infrastructure.messaging.broadcast_service import (
    BroadcastResult,
    BroadcastStrategy,
)
from ..monitoring.audit import AuditEventType
from .dependencies import (  # type: ignore[import-untyped]
    WALLET_RATE_LIMIT,
    AgentApiKeyDep,
    AuditDep,
    BroadcastDep,
    InternalTokenDep,
    MessageServiceDep,
    MetricsDep,
    RouterDep,
    _wallet_rate_limit_key,
    assert_system_caller,
    limiter,
)

router = APIRouter(
    prefix="/api/v1/communication",
    tags=["communication"],
    responses=ACN_DEFAULT_RESPONSES,
)
logger = structlog.get_logger()


def _payload_to_a2a_message(payload: dict) -> Message:
    """Build an A2A ``Message`` with a fresh per-request ``messageId``.

    The ``a2a`` Python models require ``messageId`` on every ``Message``;
    constructing with only ``role`` + ``parts`` raises
    ``pydantic.ValidationError`` and surfaces to callers as HTTP 500 (H4
    sanitised).  One UUID4 per accepted HTTP request is the correct
    envelope identity for REST-originated traffic.
    """
    return Message(
        message_id=str(uuid.uuid4()),
        role="user",
        parts=[TextPart(text=str(payload))],
    )


def _broadcast_result_to_http_responses(result: BroadcastResult) -> list[dict]:
    """Adapt a ``BroadcastResult`` to the legacy HTTP per-target shape.

    The previous HTTP path (``MessageService.broadcast_message``)
    returned ``list[{"agent_id": ..., "status": ..., ...}]`` — agent
    id was *inside* each item. ``BroadcastService`` keeps results
    keyed by agent id (``dict[agent_id, per_target]``) because it's
    a richer in-memory shape. The HTTP wire contract still uses the
    list form to preserve backward-compat with existing SDK clients
    parsing ``responses[]``.

    Per-target normalisation maps ``BroadcastService`` shapes back
    to the historical ``status`` taxonomy:

    * dict with ``error`` key → ``{status: "failed", error: ...}``
      (network / 5xx / etc.).
    * dict with ``status == "rejected"`` → kept verbatim, the
      ``reason`` / ``reject_reason`` fields are already aligned.
    * any other dict (e.g. inbox short-circuit
      ``{"status": "inbox", "route_id": ...}``) → forwarded under
      ``status: "success"`` with ``response`` carrying the dict —
      same shape callers used to see.
    * Pydantic-style ``SendMessageResponse`` model →
      ``status: "success"`` and ``response`` carrying the
      ``model_dump()`` representation.
    """
    out: list[dict] = []
    for agent_id, per_target in result.results.items():
        if isinstance(per_target, dict):
            if "error" in per_target:
                out.append(
                    {
                        "agent_id": agent_id,
                        "status": "failed",
                        "error": per_target["error"],
                    }
                )
            elif per_target.get("status") == "rejected":
                out.append({"agent_id": agent_id, **per_target})
            else:
                out.append(
                    {
                        "agent_id": agent_id,
                        "status": "success",
                        "response": per_target,
                    }
                )
        else:
            response_dump = (
                per_target.model_dump()
                if hasattr(per_target, "model_dump")
                else per_target
            )
            out.append(
                {
                    "agent_id": agent_id,
                    "status": "success",
                    "response": response_dump,
                }
            )
    return out


async def _record_broadcast_policy_rejections(
    metrics,
    responses: list[dict],
    path: str,
) -> None:
    """Bump ``acn_messages_rejected_by_policy_total`` for each
    per-target ``status == "rejected"`` entry in a broadcast response
    set.

    Aggregates by ``reason`` first so we make at most one Redis
    `INCR` per reason bucket (typically 1) instead of one per
    rejected target — a fan-out of N closed recipients turns into
    O(unique_reasons) writes rather than O(N).

    Falls back to ``policy_unknown_mode`` if a response somehow
    omits the reason field, which keeps the metric label set
    bounded (we never accept arbitrary reasons from the response
    body — they would inflate cardinality and let a misbehaving
    target define new metric series).
    """
    if not responses:
        return
    reason_counts: dict[str, int] = {}
    for r in responses:
        if r.get("status") != "rejected":
            continue
        reason = r.get("reason") or "policy_unknown_mode"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    for reason, count in reason_counts.items():
        await metrics.inc_counter(
            "messages_rejected_by_policy_total",
            value=count,
            labels={"path": path, "reason": reason},
        )


class SendMessageRequest(BaseModel):
    from_agent: str = Field(..., max_length=128)
    target_agent: str = Field(..., max_length=128)
    # `message` is an A2A Message envelope; its size is bounded by the
    # global BodySizeLimitMiddleware (security audit H6) — we don't enforce a
    # Pydantic-level dict cap because per-key length policing here would
    # double-count the body cap and only trade attack surface for false
    # negatives.
    message: dict
    priority: str = Field(default="normal", max_length=32)


class BroadcastRequest(BaseModel):
    from_agent: str = Field(..., max_length=128)
    message: dict  # bounded by BodySizeLimitMiddleware (H6)
    strategy: str = Field(default="parallel", max_length=32)
    target_subnet: str | None = Field(default=None, max_length=128)
    target_tags: list[str] | None = Field(default=None, max_length=50)


class BroadcastByTagRequest(BaseModel):
    from_agent: str = Field(..., max_length=128)
    tags: list[str] = Field(..., max_length=50)
    message: dict  # bounded by BodySizeLimitMiddleware (H6)
    limit: int | None = Field(default=None, ge=1, le=10_000)


@router.post("/send")
@limiter.limit("60/minute")
# L418: secondary per-wallet ceiling. Stacked with the per-agent
# ``60/minute`` above so a wallet fan-out across many agents can't
# bypass the per-agent limit. See ``_wallet_rate_limit_key`` for the
# rationale on the dual-bucket vs fallback choice.
@limiter.limit(WALLET_RATE_LIMIT, key_func=_wallet_rate_limit_key)
async def send_message(
    request: Request,
    body: SendMessageRequest,
    agent_info: AgentApiKeyDep,
    message_service: MessageServiceDep = None,
    metrics: MetricsDep = None,
    audit: AuditDep = None,
):
    """Send message to specific agent (requires Agent API Key, 60/min per IP)

    The authenticated agent must match the `from_agent` field to prevent spoofing.
    Clean Architecture: Route → MessageService → Repository + MessageRouter
    """
    if agent_info["agent_id"] != body.from_agent:
        raise ACNHTTPError(
            ErrorCode.FROM_AGENT_MISMATCH,
            403,
            details={
                "authenticated_as": agent_info["agent_id"],
                "from_agent": body.from_agent,
            },
        )
    try:
        message = _payload_to_a2a_message(body.message)

        result = await message_service.send_message(
            from_agent_id=body.from_agent,
            to_agent_id=body.target_agent,
            message=message,
            priority=body.priority,
        )

        await metrics.inc_message_count(
            from_agent=body.from_agent,
            to_agent=body.target_agent,
            status="success",
        )

        await audit.log_event(
            event_type=AuditEventType.MESSAGE_SENT,
            actor_id=body.from_agent,
            actor_type="agent",
            target_id=body.target_agent,
            target_type="agent",
            message_id=result.get("message_id"),
        )

        logger.info("message_sent", from_agent=body.from_agent, to_agent=body.target_agent)

        return result

    except AgentNotFoundException as e:
        logger.info(
            "message_send_target_not_found",
            from_agent=body.from_agent,
            to_agent=body.target_agent,
        )
        await metrics.inc_message_count(
            from_agent=body.from_agent,
            to_agent=body.target_agent,
            status="not_found",
        )
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": body.target_agent},
        ) from e

    except PolicyRejected as e:
        # Recipient's communication_policy denied this sender. Phase 1
        # mapping (see "Phase 1 网关执行点决策"): single-send returns
        # HTTP 403 with a structured detail so clients can branch on
        # the reason code without parsing free-form strings.
        logger.info(
            "message_rejected_by_policy",
            from_agent=body.from_agent,
            to_agent=body.target_agent,
            reason=e.reason,
        )
        # Two metric writes by design (see Step 2.5 in
        # docs/features/acn-communication-economic-model.md):
        #   * `messages_total{status="rejected"}` keeps existing
        #     traffic-shape dashboards working without a schema change.
        #   * `messages_rejected_by_policy_total{path,reason}` is the
        #     new fine-grained signal — operators can split a spike
        #     between single/internal/broadcast paths and policy_closed
        #     vs. policy_unknown_mode without grepping logs.
        await metrics.inc_message_count(
            from_agent=body.from_agent,
            to_agent=body.target_agent,
            status="rejected",
        )
        await metrics.inc_counter(
            "messages_rejected_by_policy_total",
            labels={"path": "single", "reason": e.reason},
        )
        # Audit only on single-send paths — broadcast/subnet/DLQ paths
        # would flood the audit stream at fan-out scale and rely on
        # metrics + structured logs instead.
        await audit.log_event(
            event_type=AuditEventType.MESSAGE_REJECTED,
            actor_id=body.from_agent,
            actor_type="agent",
            target_id=body.target_agent,
            target_type="agent",
            details={
                "reason": e.reason,
                "reject_reason": e.reject_reason,
                "path": "single",
            },
        )
        raise ACNHTTPError(
            ErrorCode.COMMUNICATION_REJECTED,
            403,
            details={
                "reason": e.reason,
                "reject_reason": e.reject_reason,
            },
        ) from e

    except Exception as e:
        logger.error("message_send_failed", error=str(e))
        await metrics.inc_message_count(
            from_agent=body.from_agent,
            to_agent=body.target_agent,
            status="error",
        )
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/broadcast")
@limiter.limit("10/minute")
# L418: same dual-bucket pattern as ``/send``. Broadcast already has
# a tight per-agent budget (10/min), but the wallet ceiling still
# matters: it caps cross-agent fan-out from one wallet (e.g. 100
# agents × 10/min = 1 000 broadcasts/min absent this gate).
@limiter.limit(WALLET_RATE_LIMIT, key_func=_wallet_rate_limit_key)
async def broadcast_message(
    request: Request,
    body: BroadcastRequest,
    agent_info: AgentApiKeyDep,
    broadcast_service: BroadcastDep = None,
    metrics: MetricsDep = None,
):
    """Broadcast message to multiple agents (requires Agent API Key, 10/min per IP)

    The authenticated agent must match the ``from_agent`` field to prevent spoofing.

    Phase 2 Group C #9 / review v2 P1 #7: this route now goes through
    ``BroadcastService.broadcast`` (was ``MessageService.broadcast_message``).
    The legacy double-track is collapsed — HTTP and A2A entries share
    the same parallel fan-out + Redis-persisted broadcast log + first-class
    ``broadcast_id`` traceability. See
    ``docs/features/acn-communication-economic-model.md`` L608–L614.
    """
    if agent_info["agent_id"] != body.from_agent:
        raise ACNHTTPError(
            ErrorCode.FROM_AGENT_MISMATCH,
            403,
            details={
                "authenticated_as": agent_info["agent_id"],
                "from_agent": body.from_agent,
            },
        )
    try:
        message = _payload_to_a2a_message(body.message)

        # Normalise to lowercase before the enum lookup. The pre-Group-C
        # ``MessageService.broadcast_message`` matched ``best_effort``
        # via raw string equality (``if strategy != "best_effort"``)
        # which silently accepted ``"BEST_EFFORT"`` only insofar as it
        # consistently fell into the *non*-best_effort branch — i.e.
        # uppercase users never got real best-effort behaviour. The
        # convergence's strict ``BroadcastStrategy(body.strategy)`` is
        # technically more correct (loud 422) but would break SDKs
        # that happened to send uppercase. ``.lower()`` is strictly
        # more permissive than the legacy contract and matches HTTP
        # convention — no regression risk, the only "lost" behaviour
        # is "uppercase silently maps to wrong branch", which was a
        # bug. P2-2 in the 9fb38b9 audit.
        try:
            strategy = BroadcastStrategy(body.strategy.lower())
        except ValueError as ve:
            raise ACNHTTPError(
                ErrorCode.UNKNOWN_STRATEGY,
                422,
                details={
                    "strategy": body.strategy,
                    "expected": ["parallel", "sequential", "best_effort"],
                },
            ) from ve

        result = await broadcast_service.broadcast(
            from_agent=body.from_agent,
            message=message,
            subnet_id=body.target_subnet,
            tags=body.target_tags,
            strategy=strategy,
        )

        responses = _broadcast_result_to_http_responses(result)

        await metrics.inc_counter(
            "broadcast_sent",
            labels={"type": "broadcast", "status": "success"},
        )

        # Per-target policy rejections are recorded as metric only
        # (see Step 2.5 in the proposal): a broadcast can fan out to
        # hundreds of targets, so emitting an audit event per rejected
        # target would dominate the audit stream. The metric is
        # bucketed by reason so a spike still tells the operator
        # which policy mode is producing the rejections.
        await _record_broadcast_policy_rejections(metrics, responses, "broadcast_target")

        logger.info(
            "message_broadcasted",
            from_agent=body.from_agent,
            broadcast_id=result.broadcast_id,
            target_count=result.total,
            success_count=result.success,
        )

        return {
            "status": "broadcasted",
            "broadcast_id": result.broadcast_id,
            "from_agent": body.from_agent,
            "responses": responses,
            "total": result.total,
            "successful": result.success,
        }

    except AgentNotFoundException as e:
        logger.info("broadcast_sender_not_found", from_agent=body.from_agent)
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": body.from_agent},
        ) from e

    except ACNHTTPError:
        # Strategy-validation 422 (and any other 4xx raised inside the
        # try block) is part of the API contract — let the central
        # ACNHTTPError handler translate it directly without the
        # generic 500 wrapper below. Mirrors the previous
        # ``except HTTPException: raise`` guard but scoped to ACN's
        # 4xx exception tree so non-ACN HTTPExceptions still fall
        # through to the catch-all.
        raise

    except Exception as e:
        logger.error("broadcast_failed", error=str(e))
        await metrics.inc_counter(
            "broadcast_sent",
            labels={"type": "broadcast", "status": "error"},
        )
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/broadcast-by-tag")
@limiter.limit("10/minute")
# L418: shares the wallet bucket with ``/broadcast`` and ``/send``
# — they all consume the same per-wallet budget so an attacker can't
# multiplex across endpoints to lift the effective ceiling.
@limiter.limit(WALLET_RATE_LIMIT, key_func=_wallet_rate_limit_key)
async def broadcast_by_tag(
    request: Request,
    body: BroadcastByTagRequest,
    agent_info: AgentApiKeyDep,
    broadcast_service: BroadcastDep = None,
    metrics: MetricsDep = None,
):
    """Broadcast to agents with specific tags (requires Agent API Key, 10/min per IP)

    The authenticated agent must match the ``from_agent`` field to prevent spoofing.

    Phase 2 Group C #9 / review v2 P1 #7: same convergence as
    ``/broadcast`` — uses ``BroadcastService.broadcast(tags=...)``,
    returns ``broadcast_id``.
    """
    if agent_info["agent_id"] != body.from_agent:
        raise ACNHTTPError(
            ErrorCode.FROM_AGENT_MISMATCH,
            403,
            details={
                "authenticated_as": agent_info["agent_id"],
                "from_agent": body.from_agent,
            },
        )
    try:
        message = _payload_to_a2a_message(body.message)

        result = await broadcast_service.broadcast(
            from_agent=body.from_agent,
            message=message,
            tags=body.tags,
            strategy=BroadcastStrategy.PARALLEL,
        )

        responses = _broadcast_result_to_http_responses(result)

        # Record total_sent and success_count before truncation so the
        # response accurately reflects actual delivery outcomes, not just
        # what fits in the returned slice.
        total_sent = result.total
        success_count = result.success
        # Tally policy rejections from the *full* response set (i.e.
        # before the optional ``body.limit`` truncation a few lines down)
        # so the metric counts every rejection that actually happened,
        # not just the ones that fit in the returned slice.
        await _record_broadcast_policy_rejections(metrics, responses, "broadcast_target")
        if body.limit:
            responses = responses[: body.limit]
        await metrics.inc_counter(
            "broadcast_sent",
            labels={"type": "tag_broadcast", "status": "success"},
        )

        logger.info(
            "tag_broadcast_completed",
            from_agent=body.from_agent,
            broadcast_id=result.broadcast_id,
            tags=body.tags,
            total_sent=total_sent,
            returned=len(responses),
        )

        return {
            "status": "broadcasted",
            "broadcast_id": result.broadcast_id,
            "from_agent": body.from_agent,
            "tags": body.tags,
            "responses": responses,
            "total": total_sent,
            "returned": len(responses),
            "successful": success_count,
        }

    except AgentNotFoundException as e:
        logger.info("tag_broadcast_sender_not_found", from_agent=body.from_agent)
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": body.from_agent},
        ) from e

    except Exception as e:
        logger.error("tag_broadcast_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/history/{agent_id}")
async def get_message_history(
    agent_id: str,
    agent_info: AgentApiKeyDep,
    limit: int = Query(default=100, le=1000),
    ack: bool = Query(default=False),
    message_service: MessageServiceDep = None,
):
    """Get offline inbox for agent (requires Agent API Key)

    Returns messages that were sent to this agent while it was unreachable.
    This is a pending-delivery inbox, not a full message archive.

    An agent may only retrieve its own inbox.

    - `limit`: max messages to return (newest first)
    - `ack=true`: clear the entire inbox after retrieval; caller should use a
      large enough `limit` (or the default 100) to avoid silently discarding
      un-returned messages
    """
    if agent_info["agent_id"] != agent_id:
        raise ACNHTTPError(
            ErrorCode.API_KEY_AGENT_MISMATCH,
            403,
            details={
                "path_agent": agent_id,
                "key_agent": agent_info["agent_id"],
            },
        )
    try:
        history = await message_service.get_message_history(
            agent_id=agent_id,
            limit=limit,
            consume=ack,
        )

        logger.info("inbox_retrieved", agent_id=agent_id, count=len(history), ack=ack)

        return {
            "agent_id": agent_id,
            "messages": history,
            "count": len(history),
            "limit": limit,
            "ack": ack,
        }

    except AgentNotFoundException as e:
        logger.info("inbox_retrieve_agent_not_found", agent_id=agent_id)
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e

    except Exception as e:
        logger.error("inbox_retrieve_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


class AckInboxRequest(BaseModel):
    # Cap the batch — H6 body cap already limits total bytes, but list
    # length is the more meaningful semantic guard for ack endpoints.
    route_ids: list[str] = Field(..., max_length=500)


@router.post("/history/{agent_id}/ack")
@limiter.limit("120/minute")
async def ack_message_history(
    request: Request,
    agent_id: str,
    body: AckInboxRequest,
    agent_info: AgentApiKeyDep,
    message_service: MessageServiceDep = None,
):
    """Precisely acknowledge (remove) specific messages from an agent's inbox.

    Unlike ``GET /history/{agent_id}?ack=true`` which clears the *entire* inbox,
    this endpoint removes only the messages whose ``route_id`` values are listed
    in the request body.  Useful when an agent fetches messages in small batches
    and wants to acknowledge only the batch it has successfully processed.

    An agent may only modify its own inbox (API key must match agent_id).

    Body: ``{"route_ids": ["abc123", "def456", ...]}``
    """
    if agent_info["agent_id"] != agent_id:
        raise ACNHTTPError(
            ErrorCode.API_KEY_AGENT_MISMATCH,
            403,
            details={
                "path_agent": agent_id,
                "key_agent": agent_info["agent_id"],
            },
        )
    try:
        acked = await message_service.ack_message_history(
            agent_id=agent_id,
            route_ids=body.route_ids,
        )
        logger.info("inbox_acked", agent_id=agent_id, acked=acked)
        return {"agent_id": agent_id, "acked": acked}

    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e

    except Exception as e:
        logger.error("inbox_ack_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/retry-dlq")
async def retry_dead_letter_queue(
    _: InternalTokenDep,
    max_retries: int = Query(default=3, le=10),
    router: RouterDep = None,
):
    """Retry messages from dead letter queue (requires X-Internal-Token)

    Infrastructure operation restricted to ACN operators.
    Note: Uses MessageRouter directly (infrastructure operation)
    """
    try:
        retried = await router.retry_dlq(max_retries=max_retries)

        logger.info("dlq_retry_completed", retried=retried)

        return {"retried": retried, "max_retries": max_retries}

    except Exception as e:
        logger.error("dlq_retry_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


# ---------------------------------------------------------------------------
# Internal channel — for ACN-trusted backend services (e.g. agentplanet
# backend dispatching chat-mention notifications to ACN agents).
#
# Why a dedicated endpoint rather than reusing /send with a system API key:
#   1. /send enforces ``agent_info.agent_id == body.from_agent`` to prevent
#      one agent's leaked key from being used to forge messages "from"
#      another agent. Reusing that path for system traffic would require
#      registering a real ACN agent for every backend service ("ghost
#      agent"), which pollutes search/analytics/billing dimensions and
#      adds an API-key persistence problem on the backend.
#   2. The internal channel uses ``X-Internal-Token`` (a single shared
#      operator secret already in use for admin/operator endpoints), and
#      restricts ``from_agent`` to the reserved ``system:<slug>``
#      namespace via ``assert_system_caller``. This bounds the blast
#      radius if the token leaks: an attacker can impersonate
#      ``system:*`` callers but **cannot** forge messages "from" any real
#      registered agent (whose ids are UUID4s, never matching the
#      ``system:`` namespace).
#
# Audit / metrics behaviour matches the public /send so blue teams can
# correlate system traffic with peer-agent traffic on the same dashboards.
# ---------------------------------------------------------------------------
@router.post("/internal/send")
@limiter.limit("600/minute")
async def internal_send_message(
    request: Request,
    body: SendMessageRequest,
    _: InternalTokenDep,
    message_service: MessageServiceDep = None,
    metrics: MetricsDep = None,
    audit: AuditDep = None,
):
    """Send message via the ACN internal channel (requires X-Internal-Token).

    Constraints (vs. public ``/send``):
      * ``from_agent`` must live in the ``system:<slug>`` namespace —
        anything else is rejected with HTTP 422 by ``assert_system_caller``
        (defence-in-depth so a leaked internal token cannot impersonate a
        real registered agent).
      * Higher rate limit (600/min) since legitimate backend services emit
        bursts of fan-out notifications during chat activity. The limit is
        still bounded so a runaway loop can't DoS the message router.
      * No ``agent_info`` returned — there's no authenticated agent here,
        only an authenticated *service*.
    """
    assert_system_caller(body.from_agent)

    try:
        message = _payload_to_a2a_message(body.message)

        result = await message_service.send_message(
            from_agent_id=body.from_agent,
            to_agent_id=body.target_agent,
            message=message,
            priority=body.priority,
        )

        await metrics.inc_message_count(
            from_agent=body.from_agent,
            to_agent=body.target_agent,
            status="success",
        )

        # Tag the audit record so analysts can distinguish system-channel
        # traffic from peer-agent traffic at a glance. ``actor_type`` is
        # the standard discriminator used elsewhere (see send_message
        # above which uses "agent").
        await audit.log_event(
            event_type=AuditEventType.MESSAGE_SENT,
            actor_id=body.from_agent,
            actor_type="system",
            target_id=body.target_agent,
            target_type="agent",
            message_id=result.get("message_id"),
        )

        logger.info(
            "internal_message_sent",
            from_agent=body.from_agent,
            to_agent=body.target_agent,
        )

        return result

    except AgentNotFoundException as e:
        logger.info(
            "internal_message_target_not_found",
            from_agent=body.from_agent,
            to_agent=body.target_agent,
        )
        await metrics.inc_message_count(
            from_agent=body.from_agent,
            to_agent=body.target_agent,
            status="not_found",
        )
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": body.target_agent},
        ) from e

    except PolicyRejected as e:
        # Defensive: ``assert_system_caller`` already guarantees
        # ``from_agent`` lives in ``system:*``, and PolicyCheckService
        # exempts that namespace, so this branch should be unreachable
        # under correct configuration. We keep it for the edge case
        # where the exemption rule is changed without updating this
        # route — the structured 403 keeps the failure mode obvious
        # rather than masquerading as a 500.
        logger.error(
            "internal_message_unexpectedly_rejected_by_policy",
            from_agent=body.from_agent,
            to_agent=body.target_agent,
            reason=e.reason,
        )
        await metrics.inc_message_count(
            from_agent=body.from_agent,
            to_agent=body.target_agent,
            status="rejected",
        )
        await metrics.inc_counter(
            "messages_rejected_by_policy_total",
            labels={"path": "internal", "reason": e.reason},
        )
        # Audit at WARNING-level intent: a system: caller hitting
        # a policy reject signals the exemption rule was changed
        # without updating this route — analysts must see it.
        # (AuditLogger doesn't take a log level via log_event; we
        # rely on event_type=MESSAGE_REJECTED + actor_type="system"
        # to make this distinguishable in the stream.)
        await audit.log_event(
            event_type=AuditEventType.MESSAGE_REJECTED,
            actor_id=body.from_agent,
            actor_type="system",
            target_id=body.target_agent,
            target_type="agent",
            details={
                "reason": e.reason,
                "reject_reason": e.reject_reason,
                "path": "internal",
                "unexpected": True,
            },
        )
        raise ACNHTTPError(
            ErrorCode.COMMUNICATION_REJECTED,
            403,
            details={
                "reason": e.reason,
                "reject_reason": e.reject_reason,
            },
        ) from e

    except Exception as e:
        logger.error("internal_message_send_failed", error=str(e))
        await metrics.inc_message_count(
            from_agent=body.from_agent,
            to_agent=body.target_agent,
            status="error",
        )
        raise HTTPException(status_code=500, detail=str(e)) from e
