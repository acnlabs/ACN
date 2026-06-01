"""Shared business logic for agent-side subnet membership operations.

Both `routes/subnets.py` (legacy `/api/v1/subnets/{agent_id}/subnets/…`
paths) and `routes/agent_subnets.py` (canonical `/api/v1/agents/{agent_id}/
subnets/…` paths) call into these helpers so the two route surfaces stay
behaviourally identical, byte-for-byte. The only thing each route owns is
its own URL + OpenAPI metadata (e.g. `deprecated=True` on the legacy
path); every ACL, side-effect, and response payload lives here.

Keeping logic out of route modules also lets the new canonical handlers
live in their own thin file rather than bloating `routes/registry.py`
(already 2300+ lines).
"""

from __future__ import annotations

import structlog  # type: ignore[import-untyped]
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from ..core.errors import ACNHTTPError, ErrorCode
from ..core.exceptions import (
    AgentNotFoundException,
    AlreadyMemberError,
    JoinFlowError,
    SubnetNotFoundException,
)
from ..protocols.ap2 import WebhookEventType
from ..protocols.ap2.webhook import WebhookService
from ..services import AgentService, SubnetService
from ..services._join_flow_result import JoinFlowPendingResult
from ..services.join_flow_service import JoinFlowService
from ..services.subnet_service import (
    REASON_NOT_PARENT_MEMBER,
    SubnetInvariantError,
)
from ._subnet_admission import (
    _map_join_flow_error,
    join_flow_result_to_response,
)

logger = structlog.get_logger()


def _subnet_parent_id(subnet: object) -> str | None:
    """Extract ``Subnet.parent_slug`` defensively.

    ADR-0003 Phase 3 — the field is added to the join/leave webhook
    payload's ``data`` block. Legacy ``MagicMock``-based test stubs
    don't set the attribute and return a fresh ``MagicMock`` for any
    auto-attribute access; the ``isinstance(str)`` guard makes those
    paths return ``None`` (matching a top-level subnet's payload
    shape) instead of leaking a non-JSON-serialisable mock into
    webhook bodies.
    """
    raw = getattr(subnet, "parent_slug", None)
    return raw if isinstance(raw, str) else None


def _require_self(agent_info: dict, path_agent_id: str) -> None:
    """Reject if the API key's agent_id doesn't match the path `agent_id`.

    Agents may only manage their own subnet membership.
    """
    if agent_info["agent_id"] != path_agent_id:
        raise ACNHTTPError(
            ErrorCode.API_KEY_AGENT_MISMATCH,
            403,
            details={
                "path_agent": path_agent_id,
                "key_agent": agent_info["agent_id"],
            },
        )


async def do_join_subnet(
    *,
    agent_id: str,
    slug: str,
    agent_info: dict,
    subnet_service: SubnetService,
    agent_service: AgentService,
    webhook_service: WebhookService | None,
    join_flow_service: JoinFlowService,
) -> JSONResponse:
    """Join `agent_id` into `slug` via the ADR-0004 §join six-branch flow.

    ADR-0004 Phase 2 Slice 2.3 rewrite — the legacy direct
    ``add_member + agent_service.join_subnet`` path is now a
    ``join_flow_service.join_subnet`` dispatch, which handles the
    open-subnet, owner-self-join, allowlist-hit, pending-invitation
    auto-accept, and pending-join-request fall-through branches in
    one place.

    Response shape per branch lives in
    :func:`acn.routes._subnet_admission.join_flow_result_to_response`
    (5 of the 6 branches are 200s, branch 6 is 202).

    Side effects (branches 1-5 only — branch 6 is 202, no membership
    change yet):

    - ``subnet.member_agent_ids`` gains ``agent_id``
      (already done inside ``join_flow_service.join_subnet``).
    - ``agent.subnet_ids`` gains ``slug`` (written here — the
      service layer deliberately leaves the back-reference to the
      route layer per ADR §"Why a separate service").
    - ``agent.joined_subnet`` Org Harness webhook fires best-effort.
    """
    _require_self(agent_info, agent_id)

    # Verify subnet exists (and capture entity for harness-webhook
    # delivery). The same ``Subnet`` reference is used for the
    # parent-membership pre-check and the webhook payload, so a
    # single fetch covers both.
    try:
        subnet = await subnet_service.get_subnet(slug)
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"slug": slug},
        ) from e

    # ADR-0003 child-subnet pre-check. The service layer
    # (``SubnetService.add_member``) re-asserts this invariant, but
    # checking up-front lets us return the canonical 403 surface
    # **before** ``JoinFlowService`` writes any
    # ``allowlist_auto`` / ``join_request`` row. Without the
    # pre-check, branches 5 and 6 would create a row before
    # ``add_member`` raises, leaving an orphan audit-trail entry
    # for a join that never happened.
    parent_slug = _subnet_parent_id(subnet)
    if parent_slug is not None:
        parent = None
        try:
            parent = await subnet_service.get_subnet(parent_slug)
        except SubnetNotFoundException:
            pass
        if parent is None or agent_id not in parent.member_agent_ids:
            raise ACNHTTPError(
                ErrorCode.NOT_SUBNET_MEMBER,
                403,
                details={
                    "reason": REASON_NOT_PARENT_MEMBER,
                    "slug": slug,
                    "agent_id": agent_id,
                    "parent_slug": parent_slug,
                },
            )

    # Dispatch the six-branch decision tree. ``JoinFlowService``
    # owns every membership-table write (add_member on branches
    # 1-5, save() on branches 5 + 6) and emits the matching join-
    # flow webhook via its internal publisher (currently the
    # no-op stub until Slice 2.4). All we do here is translate the
    # result into HTTP shape and add the agent-side back-reference.
    try:
        result = await join_flow_service.join_subnet(slug, agent_id)
    except AlreadyMemberError as e:
        # ADR §State machine edges "Agent self-join a subnet they
        # are already in" → 409 ALREADY_MEMBER. Bubble through the
        # join-flow error mapper so the response shape stays
        # consistent across every JoinFlowError surface.
        raise _map_join_flow_error(e) from e
    except JoinFlowError as e:
        raise _map_join_flow_error(e) from e
    except SubnetInvariantError as nest_err:
        # Race window between the parent-membership pre-check above
        # and ``add_member``'s defence-in-depth check (parent
        # membership changed concurrently). Branches 5 + 6 may
        # have written a row before raising — that row is a
        # historical artefact of the attempt, not a leak; the
        # admin replay tool can prune it. Surface the canonical
        # 403 with the same reason as the pre-check.
        raise ACNHTTPError(
            ErrorCode.NOT_SUBNET_MEMBER,
            403,
            details={
                "reason": nest_err.reason,
                "slug": slug,
                "agent_id": agent_id,
            },
        ) from nest_err
    except ACNHTTPError:
        raise
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — unexpected service-layer failure
        logger.error("join_subnet_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to join subnet") from e

    status_code, body = join_flow_result_to_response(result)

    # Branch 6 (pending) returns 202 — the caller is NOT yet a
    # member, so we skip the agent-side back-reference write and
    # the AGENT_JOINED_SUBNET webhook. Both events fire when the
    # owner approves (handled by the approve_join_request handler,
    # not here).
    if isinstance(result, JoinFlowPendingResult):
        return JSONResponse(status_code=status_code, content=body)

    # Branches 1-5 — caller is now a member. Write the agent-side
    # back-reference. Failure here leaves a half-joined state
    # (subnet has the member; agent doesn't know about the
    # subnet); best-effort roll back ``subnet.member_agent_ids``
    # so the next retry sees a clean slate. We do NOT roll back
    # any join_request / invitation row CAS that branches 3-5
    # performed — those audit rows are valid history of the
    # successful service-layer decision.
    try:
        await agent_service.join_subnet(agent_id, slug)
    except AgentNotFoundException as e:
        # Agent disappeared between auth and join — improbable
        # but possible if the agent was deleted concurrently.
        try:
            await subnet_service.remove_member(slug, agent_id)
        except Exception as rollback_err:  # noqa: BLE001
            logger.warning(
                "join_subnet_back_ref_rollback_failed",
                agent_id=agent_id,
                slug=slug,
                error=str(rollback_err),
            )
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e
    except Exception as e:  # noqa: BLE001
        # Same half-joined recovery; treat as 500.
        try:
            await subnet_service.remove_member(slug, agent_id)
        except Exception as rollback_err:  # noqa: BLE001
            logger.warning(
                "join_subnet_back_ref_rollback_failed",
                agent_id=agent_id,
                slug=slug,
                error=str(rollback_err),
            )
        logger.error(
            "join_subnet_back_ref_failed",
            agent_id=agent_id,
            slug=slug,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Failed to join subnet") from e

    logger.info(
        "agent_joined_subnet",
        agent_id=agent_id,
        slug=slug,
        branch=type(result).__name__,
    )

    # ADR-0003 harness webhook — fires for every successful join
    # regardless of branch. The ADR-0004 join-flow webhooks
    # (subnet.join_requested / .approved / .invitation_accepted /
    # etc.) are emitted independently by JoinFlowService via its
    # event publisher; the AGENT_JOINED_SUBNET event below stays
    # the ADR-0003 "an agent is now a member" signal.
    if subnet.harness_url and webhook_service is not None:
        try:
            await webhook_service.send_to(
                url=subnet.harness_url,
                secret=subnet.harness_secret,
                event=WebhookEventType.AGENT_JOINED_SUBNET,
                task_id=slug,
                data={
                    "slug": slug,
                    "agent_id": agent_id,
                    "parent_slug": parent_slug,
                },
                outbox=False,  # membership lifecycle: fire-and-forget, reconcile via GET /allowlist
            )
        except Exception as e:  # noqa: BLE001 - never break join on webhook failure
            logger.warning(
                "subnet_harness_webhook_failed",
                slug=slug,
                agent_id=agent_id,
                webhook_event="agent.joined_subnet",
                error=str(e),
            )

    return JSONResponse(status_code=status_code, content=body)


async def do_leave_subnet(
    *,
    agent_id: str,
    slug: str,
    agent_info: dict,
    subnet_service: SubnetService,
    agent_service: AgentService,
    webhook_service: WebhookService | None,
) -> dict:
    """Leave `slug`. Mirrors `do_join_subnet` semantics."""
    _require_self(agent_info, agent_id)

    # Capture subnet up-front so we still know the harness_url even if the
    # subnet later gets unmodified (it doesn't, but keeps symmetry with join).
    try:
        subnet = await subnet_service.get_subnet(slug)
    except SubnetNotFoundException:
        subnet = None  # let downstream raise the canonical error

    try:
        await agent_service.leave_subnet(agent_id, slug)
        await subnet_service.remove_member(slug, agent_id)

        logger.info("agent_left_subnet", agent_id=agent_id, slug=slug)

        if subnet and subnet.harness_url and webhook_service is not None:
            try:
                await webhook_service.send_to(
                    url=subnet.harness_url,
                    secret=subnet.harness_secret,
                    event=WebhookEventType.AGENT_LEFT_SUBNET,
                    task_id=slug,
                    data={
                        "slug": slug,
                        "agent_id": agent_id,
                        # ADR-0003 Phase 3 — see do_join_subnet for
                        # the contract; symmetric on leave so
                        # harnesses get hierarchy on both edges.
                        "parent_slug": _subnet_parent_id(subnet),
                    },
                    outbox=False,  # membership lifecycle: fire-and-forget, reconcile via GET /allowlist
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "subnet_harness_webhook_failed",
                    slug=slug,
                    agent_id=agent_id,
                    webhook_event="agent.left_subnet",
                    error=str(e),
                )

        return {"status": "left", "agent_id": agent_id, "slug": slug}
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"slug": slug},
        ) from e
    except ACNHTTPError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error("leave_subnet_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to leave subnet") from e


async def do_get_agent_subnets(
    *,
    agent_id: str,
    agent_info: dict,
    agent_service: AgentService,
) -> dict:
    """Return the list of subnets `agent_id` belongs to.

    An agent may only query its own subnet membership.
    """
    _require_self(agent_info, agent_id)
    try:
        agent = await agent_service.get_agent(agent_id)
        return {"agent_id": agent_id, "subnets": agent.subnet_ids}
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e
