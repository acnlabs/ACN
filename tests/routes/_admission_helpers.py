"""Shared test fixtures for ADR-0004 Slice 2.3 admission route tests.

The four ``test_subnet_admission_*.py`` files all need the same
service stubs (subnet / agent / join_flow) and the same TestClient
wiring. Centralising here keeps each file under ~300 lines and
ensures the mock surface is consistent — a change to
``SubnetService`` admission method shape only needs to update one
fixture file.

Not a ``conftest.py`` (which would auto-apply to every test under
``tests/routes/``): each admission test imports the fixtures
explicitly via ``pytest_plugins = ["tests.routes._admission_helpers"]``
at module top so the broader regression suite is unaffected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.api import app
from acn.core.entities import SubnetAllowlist, SubnetJoinRequest
from acn.core.exceptions import (
    AgentNotFoundException,
    SubnetNotFoundException,
)
from acn.routes.dependencies import (
    get_agent_service,
    get_join_flow_service,
    get_subnet_service,
    get_webhook_service,
)

# Canonical actors used across admission tests. The owner key
# authenticates as the owner of ``subnet-1``; the invitee key
# authenticates as the would-be member. Other-key is for
# cross-tenant 403 checks. Stub agent_service.get_agent_by_api_key
# returns the matching agent MagicMock.
OWNER_AGENT_ID = "agent-owner"
INVITEE_AGENT_ID = "agent-invitee"
OTHER_AGENT_ID = "agent-other"
OWNER_KEY = "owner-key"
INVITEE_KEY = "invitee-key"
OTHER_KEY = "other-key"

SUBNET_ID = "subnet-1"


def _make_agent(agent_id: str) -> MagicMock:
    a = MagicMock()
    a.agent_id = agent_id
    a.name = agent_id
    a.subnet_ids = []
    return a


def _make_subnet(
    subnet_id: str = SUBNET_ID,
    owner: str = OWNER_AGENT_ID,
    join_policy: str = "approval",
    harness_url: str | None = None,
    harness_secret: str | None = None,
) -> MagicMock:
    sn = MagicMock()
    sn.subnet_id = subnet_id
    sn.owner = owner
    sn.join_policy = join_policy
    sn.harness_url = harness_url
    sn.harness_secret = harness_secret
    sn.is_private = join_policy == "approval"
    sn.member_agent_ids = set()
    return sn


def make_join_request(
    *,
    request_id: str = "req-1",
    subnet_id: str = SUBNET_ID,
    agent_id: str = INVITEE_AGENT_ID,
    kind: str = "join_request",
    status: str = "pending",
    initiated_by: str | None = None,
    decided_by: str | None = None,
    note: str | None = None,
) -> SubnetJoinRequest:
    """Build a real :class:`SubnetJoinRequest` entity for route tests."""
    if initiated_by is None:
        initiated_by = (
            agent_id
            if kind == "join_request"
            else (OWNER_AGENT_ID if kind == "invitation" else "system:allowlist")
        )
    decided_at = datetime.now(UTC) if status != "pending" else None
    return SubnetJoinRequest(
        request_id=request_id,
        subnet_id=subnet_id,
        agent_id=agent_id,
        kind=kind,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        initiated_by=initiated_by,
        decided_by=decided_by,
        decided_at=decided_at,
        note=note,
    )


def make_allowlist_entry(
    *,
    subnet_id: str = SUBNET_ID,
    agent_id: str = INVITEE_AGENT_ID,
    added_by: str = OWNER_AGENT_ID,
) -> SubnetAllowlist:
    return SubnetAllowlist(
        subnet_id=subnet_id,
        agent_id=agent_id,
        added_by=added_by,
    )


@pytest.fixture
def stub_agent_service():
    """Three known agents + three known API keys."""
    svc = AsyncMock()
    agents = {
        OWNER_AGENT_ID: _make_agent(OWNER_AGENT_ID),
        INVITEE_AGENT_ID: _make_agent(INVITEE_AGENT_ID),
        OTHER_AGENT_ID: _make_agent(OTHER_AGENT_ID),
    }
    keys = {
        OWNER_KEY: agents[OWNER_AGENT_ID],
        INVITEE_KEY: agents[INVITEE_AGENT_ID],
        OTHER_KEY: agents[OTHER_AGENT_ID],
    }

    async def _by_key(key: str):
        return keys.get(key)

    async def _get_agent(agent_id: str):
        if agent_id in agents:
            return agents[agent_id]
        raise AgentNotFoundException(agent_id)

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_key)
    svc.get_agent = AsyncMock(side_effect=_get_agent)
    svc.join_subnet = AsyncMock(return_value=None)
    svc.leave_subnet = AsyncMock(return_value=None)
    svc._agents = agents
    return svc


@pytest.fixture
def stub_subnet_service():
    """SubnetService with ``subnet-1`` (approval policy, owner=agent-owner).

    Returns an ``AsyncMock`` configured with the admission method
    surface used by ``acn/routes/subnet_admission.py``. Individual
    tests configure per-method ``return_value`` / ``side_effect``
    as needed.
    """
    svc = AsyncMock()
    subnet = _make_subnet()

    async def _get_subnet(subnet_id: str):
        if subnet_id == SUBNET_ID:
            return subnet
        raise SubnetNotFoundException(subnet_id)

    svc.get_subnet = AsyncMock(side_effect=_get_subnet)
    svc._subnet = subnet
    # Admission methods — default to AsyncMock so individual tests
    # can override via ``.return_value`` / ``.side_effect``.
    svc.add_allowlist = AsyncMock()
    svc.remove_allowlist = AsyncMock(return_value=True)
    svc.list_allowlist = AsyncMock(return_value=[])
    svc.approve_join_request = AsyncMock()
    svc.reject_join_request = AsyncMock()
    svc.withdraw_join_request = AsyncMock()
    svc.list_join_requests = AsyncMock(return_value=[])
    svc.invite_agent = AsyncMock()
    svc.accept_invitation = AsyncMock()
    svc.reject_invitation = AsyncMock()
    svc.cancel_invitation = AsyncMock()
    svc.list_invitations = AsyncMock(return_value=[])
    svc.list_pending_invitations_for_agent = AsyncMock(return_value=[])
    svc.load_join_request_or_404 = AsyncMock()
    svc.add_member = AsyncMock()
    svc.remove_member = AsyncMock()
    return svc


@pytest.fixture
def stub_webhook_service():
    svc = AsyncMock()
    svc.send_to = AsyncMock(return_value=True)
    return svc


@pytest.fixture
def stub_join_flow_service():
    """Pass-through JoinFlowService; admission routes don't use it directly.

    The fixture is wired so ``do_join_subnet`` (still reachable via
    the canonical join URL) sees a stub instead of the lifespan-
    wired real service. Default is a bare AsyncMock; tests that
    exercise ``POST /agents/{a}/subnets/{s}`` configure
    ``join_subnet.return_value`` per-branch.
    """
    svc = AsyncMock()
    svc.join_subnet = AsyncMock()
    return svc


@pytest.fixture
def wire(
    stub_agent_service,
    stub_subnet_service,
    stub_webhook_service,
    stub_join_flow_service,
):
    """Install dependency overrides + yield the four-tuple of stubs.

    Cleans up the overrides on test exit so cross-test pollution
    can't leak through ``app.dependency_overrides``.
    """
    overrides: dict[Any, Any] = {
        get_agent_service: lambda: stub_agent_service,
        get_subnet_service: lambda: stub_subnet_service,
        get_webhook_service: lambda: stub_webhook_service,
        get_join_flow_service: lambda: stub_join_flow_service,
    }
    for key, fn in overrides.items():
        app.dependency_overrides[key] = fn
    try:
        yield (
            stub_agent_service,
            stub_subnet_service,
            stub_webhook_service,
            stub_join_flow_service,
        )
    finally:
        for key in overrides:
            app.dependency_overrides.pop(key, None)


def auth_headers(api_key: str = OWNER_KEY) -> dict[str, str]:
    """Bearer header for the given key."""
    return {"Authorization": f"Bearer {api_key}"}
