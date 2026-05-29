"""Route-level tests for ADR-0001: subnet creator must be a member.

ACN stores subnet membership as a bidirectional pair:
- ``subnet.member_agent_ids`` (subnet store)
- ``agent.subnet_ids`` (agent store)

Both must be written for the membership to be visible to every consumer.
``SubnetService.create_subnet`` only writes the subnet side via
``subnet.add_member(owner)``; the route handler ``POST /api/v1/subnets``
must mirror this with an ``agent_service.join_subnet(owner, slug)``
call so the agent side is also written.

Without this, freshly created subnets show ``member_count=0`` in any
consumer that derives the count from ``agent.subnet_ids`` — the common
path. Pre-fix, ``agentplanet/frontend::buildSubnetHalos`` displays a
long tail of "ghost subnets" with member_count 0.

Tracking: ``docs/adr/0001-subnet-creator-must-be-member.md``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.monitoring.audit import AuditEventType
from acn.routes.dependencies import (
    get_agent_service,
    get_subnet_service,
)


@pytest.fixture
def stub_agent_service():
    """``owner-key`` resolves to ``agent-target``."""
    svc = AsyncMock()

    target = MagicMock()
    target.agent_id = "agent-target"
    target.name = "Target"
    target.subnet_ids = []

    async def _by_api_key(key: str):
        return {"owner-key": target}.get(key)

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)
    svc.get_agent = AsyncMock(return_value=target)
    svc.join_subnet = AsyncMock(return_value=None)
    return svc


def _make_subnet_mock(slug: str = "subnet-new-abc123", owner: str = "agent-target"):
    sn = MagicMock()
    sn.slug = slug
    sn.owner = owner
    sn.is_private = False
    sn.harness_url = None
    sn.harness_secret = None
    sn.member_agent_ids = {owner}
    # ADR-0004: the route writes ``subnet.join_policy`` into
    # ``SubnetCreateResponse.join_policy`` (Literal-typed). Without an
    # explicit string here MagicMock auto-generates a child mock and
    # Pydantic rejects it with ``literal_error`` → 400.
    sn.join_policy = "open"
    return sn


@pytest.fixture
def stub_subnet_service():
    svc = AsyncMock()

    async def _create_subnet(**kwargs):
        # The real service writes subnet.add_member(owner) internally.
        # Reflect that on the returned mock so tests asserting on the
        # subnet side see what production sees.
        return _make_subnet_mock(
            slug=kwargs["slug"],
            owner=kwargs["owner"],
        )

    svc.create_subnet = AsyncMock(side_effect=_create_subnet)
    svc.delete_subnet = AsyncMock(return_value=True)
    return svc


# Pin an explicit slug in test request bodies so we can assert on it
# without depending on the route's _generate_subnet_id() randomness.
_EXPLICIT_SUBNET_ID = "subnet-explicit-test-001"


def _wire(agent_svc, subnet_svc) -> None:
    app.dependency_overrides[get_agent_service] = lambda: agent_svc
    app.dependency_overrides[get_subnet_service] = lambda: subnet_svc


# ---------------------------------------------------------------------------
# POST /api/v1/subnets — ADR-0001 contract
# ---------------------------------------------------------------------------


class TestCreateSubnetMembership:
    """The bug this ADR fixes: agent-side membership write was missing."""

    def test_create_subnet_writes_agent_side_membership(
        self, stub_agent_service, stub_subnet_service
    ):
        """The regression test: this is the assertion that would have
        caught the original ghost-subnet bug.

        After creating a subnet, ``agent_service.join_subnet`` must have
        been called with (owner, new_subnet_id). Without this call, the
        owner's ``agent.subnet_ids`` never receives the new subnet, and
        every consumer that derives ``member_count`` from agent records
        sees 0.
        """
        _wire(stub_agent_service, stub_subnet_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets",
                headers={"Authorization": "Bearer owner-key"},
                json={"name": "Demo Subnet", "slug": _EXPLICIT_SUBNET_ID},
            )

        assert r.status_code == 200, r.text
        # The subnet-side write happens inside SubnetService.create_subnet
        stub_subnet_service.create_subnet.assert_awaited_once()
        # The agent-side write must mirror it (this is what was missing)
        stub_agent_service.join_subnet.assert_awaited_once_with(
            "agent-target", _EXPLICIT_SUBNET_ID
        )

    def test_create_subnet_response_unchanged_when_join_succeeds(
        self, stub_agent_service, stub_subnet_service
    ):
        """API contract: response shape and status remain identical."""
        _wire(stub_agent_service, stub_subnet_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets",
                headers={"Authorization": "Bearer owner-key"},
                json={"name": "Demo Subnet", "slug": _EXPLICIT_SUBNET_ID},
            )

        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "created"
        assert body["slug"] == _EXPLICIT_SUBNET_ID
        assert body["is_public"] is True

    def test_create_subnet_rolls_back_when_agent_join_fails(
        self, stub_agent_service, stub_subnet_service
    ):
        """If the agent-side write fails, the half-created subnet must be
        rolled back so callers don't see a phantom record.

        Without rollback, a write-asymmetric subnet would leak into the
        same ghost-subnet class the ADR is closing.
        """
        stub_agent_service.join_subnet.side_effect = RuntimeError(
            "agent store unavailable"
        )

        _wire(stub_agent_service, stub_subnet_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets",
                headers={"Authorization": "Bearer owner-key"},
                json={"name": "Demo Subnet", "slug": _EXPLICIT_SUBNET_ID},
            )

        assert r.status_code == 500
        # Subnet creation was attempted
        stub_subnet_service.create_subnet.assert_awaited_once()
        # Then rolled back
        stub_subnet_service.delete_subnet.assert_awaited_once_with(
            _EXPLICIT_SUBNET_ID, "agent-target"
        )

    def test_create_subnet_unauthenticated_does_not_mutate(
        self, stub_agent_service, stub_subnet_service
    ):
        _wire(stub_agent_service, stub_subnet_service)

        with TestClient(app) as client:
            r = client.post("/api/v1/subnets", json={"name": "x"})

        assert 400 <= r.status_code < 500
        stub_subnet_service.create_subnet.assert_not_awaited()
        stub_agent_service.join_subnet.assert_not_awaited()

    def test_create_public_subnet_emits_subnet_created_audit(
        self, stub_agent_service, stub_subnet_service
    ):
        _wire(stub_agent_service, stub_subnet_service)
        with (
            patch("acn.routes.subnets.get_audit_singleton", return_value=object()),
            patch("acn.routes.subnets.fire_and_forget_event") as fire,
            TestClient(app) as client,
        ):
            r = client.post(
                "/api/v1/subnets",
                headers={"Authorization": "Bearer owner-key"},
                json={"name": "Demo Subnet", "slug": _EXPLICIT_SUBNET_ID},
            )

        assert r.status_code == 200, r.text
        fire.assert_called_once()
        kwargs = fire.call_args.kwargs
        assert kwargs["event_type"] == AuditEventType.SUBNET_CREATED
        assert kwargs["target_id"] == _EXPLICIT_SUBNET_ID
        assert kwargs["details"]["is_private"] is False
        assert kwargs["details"]["public_broadcast_eligible"] is True

    def test_create_private_subnet_emits_internal_audit_but_marks_non_public(
        self, stub_agent_service, stub_subnet_service
    ):
        async def _private_create(**kwargs):
            sn = _make_subnet_mock(slug=kwargs["slug"], owner=kwargs["owner"])
            sn.is_private = True
            sn.join_policy = "approval"
            return sn

        stub_subnet_service.create_subnet = AsyncMock(side_effect=_private_create)
        _wire(stub_agent_service, stub_subnet_service)
        with (
            patch("acn.routes.subnets.get_audit_singleton", return_value=object()),
            patch("acn.routes.subnets.fire_and_forget_event") as fire,
            TestClient(app) as client,
        ):
            r = client.post(
                "/api/v1/subnets",
                headers={"Authorization": "Bearer owner-key"},
                json={
                    "name": "Private Subnet",
                    "slug": _EXPLICIT_SUBNET_ID,
                    "is_private": True,
                },
            )

        assert r.status_code == 200, r.text
        fire.assert_called_once()
        kwargs = fire.call_args.kwargs
        assert kwargs["event_type"] == AuditEventType.SUBNET_CREATED
        assert kwargs["details"]["is_private"] is True
        assert kwargs["details"]["public_broadcast_eligible"] is False
