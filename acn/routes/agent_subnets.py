"""Canonical agent-side subnet membership endpoints.

These three endpoints live at the *correct* place in the URL tree —
under `/api/v1/agents/{agent_id}/…` alongside every other agent-side
operation (heartbeat / claim / transfer / wallets / …). They replace the
legacy paths still served by `routes/subnets.py` at the awkward
`/api/v1/subnets/{agent_id}/subnets/…` shape (kept for back-compat,
marked `deprecated=True` in OpenAPI).

Both surfaces call into `_subnet_membership.py` for the actual business
logic, so they are guaranteed to be byte-for-byte identical. This file
owns only the URL + OpenAPI metadata.
"""

from __future__ import annotations

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter

from ..core.errors import ACN_DEFAULT_RESPONSES
from ._subnet_membership import (
    do_get_agent_subnets,
    do_join_subnet,
    do_leave_subnet,
)
from .dependencies import (  # type: ignore[import-untyped]
    AgentApiKeyDep,
    AgentIdPath,
    AgentServiceDep,
    JoinFlowServiceDep,
    SubnetIdPath,
    SubnetServiceDep,
    WebhookServiceDep,
)

logger = structlog.get_logger()

router = APIRouter(
    prefix="/api/v1/agents",
    tags=["subnets"],
    responses=ACN_DEFAULT_RESPONSES,
)


@router.post("/{agent_id}/subnets/{subnet_id}")
async def join_subnet(
    agent_id: AgentIdPath,
    subnet_id: SubnetIdPath,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
    webhook_service: WebhookServiceDep = None,
    join_flow_service: JoinFlowServiceDep = None,
):
    """Agent joins a subnet (requires Agent API Key).

    ADR-0004 Phase 2 Slice 2.3 — behaviour now branches on
    ``subnet.join_policy`` and the caller's relationship to the
    subnet (member, owner, pending invitation, allowlist hit, or
    fresh applicant). Response status varies per branch (200 for
    immediate admission, 202 for pending join_request) — see
    ``acn/routes/_subnet_admission.py::join_flow_result_to_response``
    for the per-branch shape table.
    """
    return await do_join_subnet(
        agent_id=agent_id,
        subnet_id=subnet_id,
        agent_info=agent_info,
        subnet_service=subnet_service,
        agent_service=agent_service,
        webhook_service=webhook_service,
        join_flow_service=join_flow_service,
    )


@router.delete("/{agent_id}/subnets/{subnet_id}")
async def leave_subnet(
    agent_id: AgentIdPath,
    subnet_id: SubnetIdPath,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
    webhook_service: WebhookServiceDep = None,
):
    """Agent leaves a subnet (requires Agent API Key).

    Symmetric with `join_subnet`. Removes the agent from the subnet's
    `member_agent_ids` and fires an `agent.left_subnet` webhook to the
    subnet's Org Harness if one is registered.
    """
    return await do_leave_subnet(
        agent_id=agent_id,
        subnet_id=subnet_id,
        agent_info=agent_info,
        subnet_service=subnet_service,
        agent_service=agent_service,
        webhook_service=webhook_service,
    )


@router.get("/{agent_id}/subnets")
async def get_agent_subnets(
    agent_id: AgentIdPath,
    agent_info: AgentApiKeyDep,
    agent_service: AgentServiceDep = None,
):
    """Get the list of subnets `agent_id` belongs to (requires Agent API Key).

    An agent may only query its own subnet membership.
    """
    return await do_get_agent_subnets(
        agent_id=agent_id,
        agent_info=agent_info,
        agent_service=agent_service,
    )
