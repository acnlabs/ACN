"""Route-level tests for the pluggable Org Harness endpoints.

Covers:
- ``PATCH /api/v1/subnets/{slug}/harness`` — owner can register / clear,
  non-owner gets 403 ``OWNERSHIP_MISMATCH``, missing subnet gets 404.
- ``POST /api/v1/subnets/{agent_id}/subnets/{slug}`` (join) — when the
  subnet has a registered harness, ``WebhookService.send_to`` is called with
  the ``agent.joined_subnet`` event.
- ``DELETE /api/v1/subnets/{agent_id}/subnets/{slug}`` (leave) — same
  contract for ``agent.left_subnet``.
- Harness-webhook failure during join/leave must NOT surface a 5xx to the
  client.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import SubnetNotFoundException
from acn.protocols.ap2.webhook import WebhookEventType
from acn.routes.dependencies import (
    get_agent_service,
    get_join_flow_service,
    get_subnet_service,
    get_webhook_service,
)
from acn.services._join_flow_result import JoinFlowJoinedOpenResult
from tests.routes.conftest import _assert_flat_shape

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_agent_service():
    """``owner-key`` → ``agent-target``; ``other-key`` → ``agent-other``."""
    svc = AsyncMock()

    target = MagicMock()
    target.agent_id = "agent-target"
    target.name = "Target"
    target.subnet_ids = ["subnet-1"]

    other = MagicMock()
    other.agent_id = "agent-other"
    other.name = "Other"

    async def _by_api_key(key: str):
        return {"owner-key": target, "other-key": other}.get(key)

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)
    svc.get_agent = AsyncMock(return_value=target)
    svc.join_subnet = AsyncMock(return_value=None)
    svc.leave_subnet = AsyncMock(return_value=None)
    return svc


def _make_subnet_mock(
    slug: str = "subnet-1",
    owner: str = "agent-target",
    harness_url: str | None = None,
    harness_secret: str | None = None,
):
    sn = MagicMock()
    sn.slug = slug
    sn.owner = owner
    sn.harness_url = harness_url
    sn.harness_secret = harness_secret
    sn.is_private = False
    sn.member_agent_ids = set()
    return sn


@pytest.fixture
def stub_subnet_service():
    svc = AsyncMock()
    default = _make_subnet_mock()

    async def _get_subnet(slug: str):
        if slug == "subnet-1":
            return default
        raise SubnetNotFoundException(slug)

    svc.get_subnet = AsyncMock(side_effect=_get_subnet)
    svc.add_member = AsyncMock(return_value=None)
    svc.remove_member = AsyncMock(return_value=None)
    svc.update_harness = AsyncMock()
    svc._default_subnet = default  # let tests mutate / re-use it
    return svc


@pytest.fixture
def stub_webhook_service():
    svc = AsyncMock()
    svc.send_to = AsyncMock(return_value=True)
    svc.send_event = AsyncMock(return_value=True)
    return svc


@pytest.fixture(autouse=True)
def stub_join_flow_service(stub_subnet_service):
    """JoinFlowService stub forwarding open-branch to stub_subnet_service.

    Auto-applied so the harness webhook tests (which exercise the
    join path under the new ADR-0004 Slice 2.3 dispatcher) pick up
    a stubbed JoinFlowService instead of the real lifespan-wired
    instance. See ``tests/routes/test_agent_subnets.py`` for the
    same pattern + extended rationale.
    """
    svc = AsyncMock()

    async def _join_subnet(slug: str, agent_id: str):
        await stub_subnet_service.get_subnet(slug)
        await stub_subnet_service.add_member(slug, agent_id)
        return JoinFlowJoinedOpenResult(slug=slug, agent_id=agent_id)

    svc.join_subnet = AsyncMock(side_effect=_join_subnet)
    app.dependency_overrides[get_join_flow_service] = lambda: svc
    try:
        yield svc
    finally:
        app.dependency_overrides.pop(get_join_flow_service, None)


def _wire(agent_svc, subnet_svc, webhook_svc=None) -> None:
    app.dependency_overrides[get_agent_service] = lambda: agent_svc
    app.dependency_overrides[get_subnet_service] = lambda: subnet_svc
    if webhook_svc is not None:
        app.dependency_overrides[get_webhook_service] = lambda: webhook_svc


# ---------------------------------------------------------------------------
# PATCH /api/v1/subnets/{slug}/harness
# ---------------------------------------------------------------------------


class TestPatchSubnetHarness:
    def test_owner_can_register_harness(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        updated = _make_subnet_mock(
            harness_url="https://paperclip.example/acn",
            harness_secret="topsecret",
        )
        stub_subnet_service.update_harness.return_value = updated

        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/subnets/subnet-1/harness",
                headers={"Authorization": "Bearer owner-key"},
                json={
                    "harness_url": "https://paperclip.example/acn",
                    "harness_secret": "topsecret",
                },
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "updated"
        assert body["slug"] == "subnet-1"
        assert body["harness_url"] == "https://paperclip.example/acn"
        assert body["harness_registered"] is True
        # Secret must NEVER be echoed back over the wire
        assert "harness_secret" not in body

        stub_subnet_service.update_harness.assert_awaited_once_with(
            slug="subnet-1",
            owner="agent-target",
            harness_url="https://paperclip.example/acn",
            harness_secret="topsecret",
        )

    def test_owner_can_clear_harness_with_null(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        updated = _make_subnet_mock(harness_url=None, harness_secret=None)
        stub_subnet_service.update_harness.return_value = updated

        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/subnets/subnet-1/harness",
                headers={"Authorization": "Bearer owner-key"},
                json={"harness_url": None, "harness_secret": None},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["harness_url"] is None
        assert body["harness_registered"] is False

    def test_non_owner_returns_403_ownership_mismatch(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        stub_subnet_service.update_harness.side_effect = PermissionError(
            "Owner mismatch: agent-other != agent-target"
        )

        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/subnets/subnet-1/harness",
                headers={"Authorization": "Bearer other-key"},
                json={"harness_url": "https://evil.example", "harness_secret": "pwn"},
            )

        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "ownership_mismatch"
        assert body["details"]["slug"] == "subnet-1"
        assert "reason" in body["details"]

    def test_missing_subnet_returns_404(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        stub_subnet_service.update_harness.side_effect = SubnetNotFoundException(
            "ghost"
        )
        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/subnets/ghost/harness",
                headers={"Authorization": "Bearer owner-key"},
                json={"harness_url": "https://x", "harness_secret": None},
            )

        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "subnet_not_found"
        assert body["details"] == {"slug": "ghost"}

    def test_unauthenticated_does_not_succeed(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        """Without Bearer credentials the endpoint MUST NOT execute the
        update — anything that lands as 4xx and never hits the service is
        acceptable (the exact code is FastAPI's choice between 401/403/422
        depending on which dependency raises first)."""
        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/subnets/subnet-1/harness",
                json={"harness_url": "https://x", "harness_secret": None},
            )

        assert 400 <= r.status_code < 500
        stub_subnet_service.update_harness.assert_not_awaited()


# ---------------------------------------------------------------------------
# Join / leave subnet → agent webhook delivery
# ---------------------------------------------------------------------------


class TestJoinLeaveWebhookDelivery:
    def test_join_with_registered_harness_fires_agent_joined_event(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        stub_subnet_service._default_subnet.harness_url = "https://h.example/hook"
        stub_subnet_service._default_subnet.harness_secret = "hs"

        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets/agent-target/subnets/subnet-1",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        assert r.json()["status"] == "joined"

        stub_webhook_service.send_to.assert_awaited_once()
        kw = stub_webhook_service.send_to.await_args.kwargs
        assert kw["url"] == "https://h.example/hook"
        assert kw["secret"] == "hs"
        assert kw["event"] == WebhookEventType.AGENT_JOINED_SUBNET
        assert kw["data"]["slug"] == "subnet-1"
        assert kw["data"]["agent_id"] == "agent-target"

    def test_join_without_registered_harness_skips_send_to(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        # default subnet has harness_url=None
        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets/agent-target/subnets/subnet-1",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200
        stub_webhook_service.send_to.assert_not_awaited()

    def test_leave_with_registered_harness_fires_agent_left_event(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        stub_subnet_service._default_subnet.harness_url = "https://h.example/hook"
        stub_subnet_service._default_subnet.harness_secret = None  # no secret

        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/subnets/agent-target/subnets/subnet-1",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        assert r.json()["status"] == "left"

        stub_webhook_service.send_to.assert_awaited_once()
        kw = stub_webhook_service.send_to.await_args.kwargs
        assert kw["url"] == "https://h.example/hook"
        assert kw["secret"] is None  # explicitly unsigned
        assert kw["event"] == WebhookEventType.AGENT_LEFT_SUBNET

    def test_harness_delivery_failure_does_not_500_the_join(
        self, stub_agent_service, stub_subnet_service, stub_webhook_service
    ):
        """If Paperclip's harness URL is dead, the agent's join request must
        still succeed (200). The webhook is best-effort, not a transaction."""
        stub_subnet_service._default_subnet.harness_url = "https://dead.example"
        stub_subnet_service._default_subnet.harness_secret = "k"
        stub_webhook_service.send_to.side_effect = RuntimeError("conn refused")

        _wire(stub_agent_service, stub_subnet_service, stub_webhook_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets/agent-target/subnets/subnet-1",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        assert r.json()["status"] == "joined"
        stub_webhook_service.send_to.assert_awaited_once()
