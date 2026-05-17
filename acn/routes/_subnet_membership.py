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

from ..core.errors import ACNHTTPError, ErrorCode
from ..core.exceptions import AgentNotFoundException, SubnetNotFoundException
from ..protocols.ap2 import WebhookEventType
from ..protocols.ap2.webhook import WebhookService
from ..services import AgentService, SubnetService
from ..services.subnet_service import (
    REASON_NOT_PARENT_MEMBER,
    SubnetNestingError,
)

logger = structlog.get_logger()


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
    subnet_id: str,
    agent_info: dict,
    subnet_service: SubnetService,
    agent_service: AgentService,
    webhook_service: WebhookService | None,
) -> dict:
    """Join `agent_id` into `subnet_id`. Idempotent at the service layer.

    Side effects:
    - `agent.subnet_ids` gains `subnet_id`
    - `subnet.member_agent_ids` gains `agent_id`
    - `agent.joined_subnet` Org Harness webhook is fired best-effort
    """
    _require_self(agent_info, agent_id)

    # Verify subnet exists (and capture entity for harness-webhook delivery).
    try:
        subnet = await subnet_service.get_subnet(subnet_id)
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"subnet_id": subnet_id},
        ) from e

    # ADR-0003 child-subnet pre-check. ``SubnetService.add_member``
    # would reject the same way, but doing it *before* the
    # agent-side ``join_subnet`` write keeps state consistent — a
    # mid-flight rejection otherwise leaves ``agent.subnet_ids``
    # containing a subnet whose ``member_agent_ids`` doesn't include
    # the agent (the dreaded half-joined state). We keep the
    # service-layer check as defence-in-depth for the admin path.
    #
    # ``getattr`` + ``isinstance`` guard lets legacy MagicMock-based
    # stubs (which don't set ``parent_subnet_id`` and return a
    # ``MagicMock`` auto-attribute for it) skip this branch — the
    # real ``Subnet`` entity always populates it with ``str | None``.
    parent_subnet_id_raw = getattr(subnet, "parent_subnet_id", None)
    parent_subnet_id = (
        parent_subnet_id_raw if isinstance(parent_subnet_id_raw, str) else None
    )
    if parent_subnet_id is not None:
        parent = None
        try:
            parent = await subnet_service.get_subnet(parent_subnet_id)
        except SubnetNotFoundException:
            pass
        if parent is None or agent_id not in parent.member_agent_ids:
            raise ACNHTTPError(
                ErrorCode.NOT_SUBNET_MEMBER,
                403,
                details={
                    "reason": REASON_NOT_PARENT_MEMBER,
                    "subnet_id": subnet_id,
                    "agent_id": agent_id,
                    "parent_subnet_id": parent_subnet_id,
                },
            )

    try:
        await agent_service.join_subnet(agent_id, subnet_id)
        try:
            await subnet_service.add_member(subnet_id, agent_id)
        except SubnetNestingError as nest_err:
            # Race window between the pre-check above and the
            # service-layer write (parent membership changed
            # concurrently). Roll back the agent-side write so we
            # don't leave a half-joined state, then surface the
            # 403 with the same canonical reason as the pre-check.
            try:
                await agent_service.leave_subnet(agent_id, subnet_id)
            except Exception as rollback_err:  # noqa: BLE001
                logger.warning(
                    "join_subnet_rollback_failed",
                    agent_id=agent_id,
                    subnet_id=subnet_id,
                    error=str(rollback_err),
                )
            raise ACNHTTPError(
                ErrorCode.NOT_SUBNET_MEMBER,
                403,
                details={
                    "reason": nest_err.reason,
                    "subnet_id": subnet_id,
                    "agent_id": agent_id,
                },
            ) from nest_err

        logger.info("agent_joined_subnet", agent_id=agent_id, subnet_id=subnet_id)

        if subnet.harness_url and webhook_service is not None:
            try:
                await webhook_service.send_to(
                    url=subnet.harness_url,
                    secret=subnet.harness_secret,
                    event=WebhookEventType.AGENT_JOINED_SUBNET,
                    task_id=subnet_id,  # no task; use subnet_id for trace correlation
                    data={
                        "subnet_id": subnet_id,
                        "agent_id": agent_id,
                    },
                )
            except Exception as e:  # noqa: BLE001 - never break join on webhook failure
                logger.warning(
                    "subnet_harness_webhook_failed",
                    subnet_id=subnet_id,
                    agent_id=agent_id,
                    webhook_event="agent.joined_subnet",
                    error=str(e),
                )

        return {"status": "joined", "agent_id": agent_id, "subnet_id": subnet_id}
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e
    except ACNHTTPError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error("join_subnet_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to join subnet") from e


async def do_leave_subnet(
    *,
    agent_id: str,
    subnet_id: str,
    agent_info: dict,
    subnet_service: SubnetService,
    agent_service: AgentService,
    webhook_service: WebhookService | None,
) -> dict:
    """Leave `subnet_id`. Mirrors `do_join_subnet` semantics."""
    _require_self(agent_info, agent_id)

    # Capture subnet up-front so we still know the harness_url even if the
    # subnet later gets unmodified (it doesn't, but keeps symmetry with join).
    try:
        subnet = await subnet_service.get_subnet(subnet_id)
    except SubnetNotFoundException:
        subnet = None  # let downstream raise the canonical error

    try:
        await agent_service.leave_subnet(agent_id, subnet_id)
        await subnet_service.remove_member(subnet_id, agent_id)

        logger.info("agent_left_subnet", agent_id=agent_id, subnet_id=subnet_id)

        if subnet and subnet.harness_url and webhook_service is not None:
            try:
                await webhook_service.send_to(
                    url=subnet.harness_url,
                    secret=subnet.harness_secret,
                    event=WebhookEventType.AGENT_LEFT_SUBNET,
                    task_id=subnet_id,
                    data={
                        "subnet_id": subnet_id,
                        "agent_id": agent_id,
                    },
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "subnet_harness_webhook_failed",
                    subnet_id=subnet_id,
                    agent_id=agent_id,
                    webhook_event="agent.left_subnet",
                    error=str(e),
                )

        return {"status": "left", "agent_id": agent_id, "subnet_id": subnet_id}
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
            details={"subnet_id": subnet_id},
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
