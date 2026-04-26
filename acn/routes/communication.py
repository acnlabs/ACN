"""Communication API Routes

Clean Architecture implementation: Route → MessageService → MessageRouter
"""

import structlog  # type: ignore[import-untyped]
from a2a.types import Message, TextPart  # type: ignore[import-untyped]
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..core.exceptions import AgentNotFoundException
from ..monitoring.audit import AuditEventType
from .dependencies import (  # type: ignore[import-untyped]
    AgentApiKeyDep,
    AuditDep,
    InternalTokenDep,
    MessageServiceDep,
    MetricsDep,
    RouterDep,
    assert_system_caller,
    limiter,
)

router = APIRouter(prefix="/api/v1/communication", tags=["communication"])
logger = structlog.get_logger()


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
        raise HTTPException(
            status_code=403,
            detail="Authenticated agent does not match from_agent field",
        )
    try:
        message = Message(
            role="user",
            parts=[TextPart(text=str(body.message))],
        )

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
        logger.error("message_send_failed", error=str(e))
        await metrics.inc_message_count(
            from_agent=body.from_agent,
            to_agent=body.target_agent,
            status="not_found",
        )
        raise HTTPException(status_code=404, detail=str(e)) from e

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
async def broadcast_message(
    request: Request,
    body: BroadcastRequest,
    agent_info: AgentApiKeyDep,
    message_service: MessageServiceDep = None,
    metrics: MetricsDep = None,
):
    """Broadcast message to multiple agents (requires Agent API Key, 10/min per IP)

    The authenticated agent must match the `from_agent` field to prevent spoofing.
    Clean Architecture: Route → MessageService → Repository + MessageRouter
    """
    if agent_info["agent_id"] != body.from_agent:
        raise HTTPException(
            status_code=403,
            detail="Authenticated agent does not match from_agent field",
        )
    try:
        message = Message(
            role="user",
            parts=[TextPart(text=str(body.message))],
        )

        responses = await message_service.broadcast_message(
            from_agent_id=body.from_agent,
            message=message,
            subnet_id=body.target_subnet,
            tags=body.target_tags,
            strategy=body.strategy,
        )

        success_count = len([r for r in responses if r.get("status") == "success"])
        await metrics.inc_counter(
            "broadcast_sent",
            labels={"type": "broadcast", "status": "success"},
        )

        logger.info(
            "message_broadcasted",
            from_agent=body.from_agent,
            target_count=len(responses),
            success_count=success_count,
        )

        return {
            "status": "broadcasted",
            "from_agent": body.from_agent,
            "responses": responses,
            "total": len(responses),
            "successful": success_count,
        }

    except AgentNotFoundException as e:
        logger.error("broadcast_failed", error=str(e))
        raise HTTPException(status_code=404, detail=str(e)) from e

    except Exception as e:
        logger.error("broadcast_failed", error=str(e))
        await metrics.inc_counter(
            "broadcast_sent",
            labels={"type": "broadcast", "status": "error"},
        )
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/broadcast-by-tag")
@limiter.limit("10/minute")
async def broadcast_by_tag(
    request: Request,
    body: BroadcastByTagRequest,
    agent_info: AgentApiKeyDep,
    message_service: MessageServiceDep = None,
    metrics: MetricsDep = None,
):
    """Broadcast to agents with specific tags (requires Agent API Key, 10/min per IP)

    The authenticated agent must match the `from_agent` field to prevent spoofing.
    Clean Architecture: Route → MessageService → Repository
    """
    if agent_info["agent_id"] != body.from_agent:
        raise HTTPException(
            status_code=403,
            detail="Authenticated agent does not match from_agent field",
        )
    try:
        message = Message(
            role="user",
            parts=[TextPart(text=str(body.message))],
        )

        responses = await message_service.broadcast_message(
            from_agent_id=body.from_agent,
            message=message,
            tags=body.tags,
            strategy="parallel",
        )

        # Record total_sent and success_count before truncation so the
        # response accurately reflects actual delivery outcomes, not just
        # what fits in the returned slice.
        total_sent = len(responses)
        success_count = len([r for r in responses if r.get("status") == "success"])
        if body.limit:
            responses = responses[: body.limit]
        await metrics.inc_counter(
            "broadcast_sent",
            labels={"type": "tag_broadcast", "status": "success"},
        )

        logger.info(
            "tag_broadcast_completed",
            from_agent=body.from_agent,
            tags=body.tags,
            total_sent=total_sent,
            returned=len(responses),
        )

        return {
            "status": "broadcasted",
            "from_agent": body.from_agent,
            "tags": body.tags,
            "responses": responses,
            "total": total_sent,
            "returned": len(responses),
            "successful": success_count,
        }

    except AgentNotFoundException as e:
        logger.error("tag_broadcast_failed", error=str(e))
        raise HTTPException(status_code=404, detail=str(e)) from e

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
        raise HTTPException(
            status_code=403,
            detail="API key does not match agent_id",
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
        logger.error("inbox_retrieve_failed", error=str(e))
        raise HTTPException(status_code=404, detail=str(e)) from e

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
        raise HTTPException(
            status_code=403,
            detail="API key does not match agent_id",
        )
    try:
        acked = await message_service.ack_message_history(
            agent_id=agent_id,
            route_ids=body.route_ids,
        )
        logger.info("inbox_acked", agent_id=agent_id, acked=acked)
        return {"agent_id": agent_id, "acked": acked}

    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

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
        message = Message(
            role="user",
            parts=[TextPart(text=str(body.message))],
        )

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
        logger.error("internal_message_send_failed", error=str(e))
        await metrics.inc_message_count(
            from_agent=body.from_agent,
            to_agent=body.target_agent,
            status="not_found",
        )
        raise HTTPException(status_code=404, detail=str(e)) from e

    except Exception as e:
        logger.error("internal_message_send_failed", error=str(e))
        await metrics.inc_message_count(
            from_agent=body.from_agent,
            to_agent=body.target_agent,
            status="error",
        )
        raise HTTPException(status_code=500, detail=str(e)) from e
