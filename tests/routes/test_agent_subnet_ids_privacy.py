"""ACL V6 B3 — agent subnet_ids privacy contract.

``GET /api/v1/agents/{id}`` and ``GET /api/v1/agents`` (search):

- Anonymous / unauthenticated callers: only public subnet slugs in
  ``subnet_ids``.
- User JWT (any): only public subnet slugs (user owning the agent is
  NOT an exception — they must use the agent's own API key to see the
  full list).
- API key = the agent itself (self): full subnet_ids list.
- acn:admin: full subnet_ids list.
- API key ≠ the agent (unrelated agent): only public subnet slugs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException
from acn.routes.dependencies import get_agent_service, get_subnet_service

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(
    agent_id: str = "agent-target",
    *,
    owner: str = "user-owner",
    subnet_ids: list[str] | None = None,
) -> MagicMock:
    a = MagicMock()
    a.agent_id = agent_id
    a.owner = owner
    a.name = "Target"
    a.description = None
    a.subnet_ids = subnet_ids or ["public", "subnet-private-abc"]
    a.tags = []
    a.endpoint = "https://example.com/agent"
    a.agent_card_url = None
    a.agent_card = None
    a.metadata = {}
    a.claim_status = None
    a.referrer_id = None
    a.verification_code = "acn-XXXX"
    a.registered_at = datetime(2026, 1, 1, tzinfo=UTC)
    a.last_heartbeat = None
    a.wallet_address = None
    a.wallet_addresses = None
    a.accepts_payment = False
    a.payment_methods = []
    a.social_card_url = None
    return a


def _make_public_subnet(subnet_id: str) -> MagicMock:
    s = MagicMock()
    s.subnet_id = subnet_id
    s.is_private = False
    return s


def _fake_verify_token(
    *, sub: str, caller_type: str = "agent", permissions: list[str] | None = None
) -> Any:
    async def _impl(*args, **kwargs):
        return {"sub": sub, "type": caller_type, "permissions": permissions or []}
    return _impl


@pytest.fixture
def stub_agent_service():
    target = _make_agent()
    svc = AsyncMock()

    async def _get_agent(agent_id: str):
        if agent_id == "agent-target":
            return target
        raise AgentNotFoundException(agent_id)

    svc.get_agent = AsyncMock(side_effect=_get_agent)
    svc.is_alive = AsyncMock(return_value=True)
    svc.batch_alive = AsyncMock(return_value={"agent-target"})
    svc.search_agents = AsyncMock(return_value=[target])
    return svc


@pytest.fixture
def stub_subnet_service():
    svc = AsyncMock()
    # Only "public" is a public subnet
    svc.list_public_subnets = AsyncMock(return_value=[_make_public_subnet("public")])
    return svc


@pytest.fixture(autouse=True)
def wire(stub_agent_service, stub_subnet_service):
    app.dependency_overrides[get_agent_service] = lambda: stub_agent_service
    app.dependency_overrides[get_subnet_service] = lambda: stub_subnet_service
    yield
    app.dependency_overrides.pop(get_agent_service, None)
    app.dependency_overrides.pop(get_subnet_service, None)


# ---------------------------------------------------------------------------
# GET /agents/{id}
# ---------------------------------------------------------------------------


class TestGetAgentSubnetIdsPrivacy:
    def test_anon_sees_only_public_slugs(self):
        """No auth → only public subnet slugs in subnet_ids."""
        with TestClient(app) as client:
            r = client.get("/api/v1/agents/agent-target")

        assert r.status_code == 200, r.text
        assert r.json()["subnet_ids"] == ["public"]

    def test_user_jwt_sees_only_public_slugs(self, monkeypatch):
        """User JWT (even owner) → only public slugs."""
        monkeypatch.setattr(
            "acn.routes.registry.verify_token",
            _fake_verify_token(sub="user-owner", caller_type="user"),
        )
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/agent-target",
                headers={"Authorization": "Bearer user-token"},
            )

        assert r.status_code == 200, r.text
        assert r.json()["subnet_ids"] == ["public"]

    def test_unrelated_agent_apikey_sees_only_public_slugs(self, monkeypatch):
        """API key for a different agent → only public slugs."""
        monkeypatch.setattr(
            "acn.routes.registry.verify_token",
            _fake_verify_token(sub="agent-other", caller_type="agent"),
        )
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/agent-target",
                headers={"Authorization": "Bearer other-key"},
            )

        assert r.status_code == 200, r.text
        assert r.json()["subnet_ids"] == ["public"]

    def test_self_apikey_sees_full_subnet_ids(self, monkeypatch):
        """API key = the agent itself → full subnet_ids including private."""
        monkeypatch.setattr(
            "acn.routes.registry.verify_token",
            _fake_verify_token(sub="agent-target", caller_type="agent"),
        )
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/agent-target",
                headers={"Authorization": "Bearer self-key"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert "subnet-private-abc" in body["subnet_ids"]
        assert "public" in body["subnet_ids"]

    def test_admin_sees_full_subnet_ids(self, monkeypatch):
        """acn:admin → full subnet_ids."""
        monkeypatch.setattr(
            "acn.routes.registry.verify_token",
            _fake_verify_token(
                sub="admin-user",
                caller_type="user",
                permissions=["acn:admin"],
            ),
        )
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/agent-target",
                headers={"Authorization": "Bearer admin-token"},
            )

        assert r.status_code == 200, r.text
        assert "subnet-private-abc" in r.json()["subnet_ids"]


# ---------------------------------------------------------------------------
# GET /agents (search / list)
# ---------------------------------------------------------------------------


class TestSearchAgentsSubnetIdsPrivacy:
    def test_anon_sees_only_public_slugs(self):
        """Unauthenticated list → only public slugs per agent."""
        with TestClient(app) as client:
            r = client.get("/api/v1/agents?status=all")

        assert r.status_code == 200, r.text
        for ag in r.json()["agents"]:
            assert ag["subnet_ids"] == ["public"], ag["subnet_ids"]

    def test_user_jwt_sees_only_public_slugs(self, monkeypatch):
        """User JWT on list endpoint → only public slugs."""
        monkeypatch.setattr(
            "acn.routes.registry.verify_token",
            _fake_verify_token(sub="user-owner", caller_type="user"),
        )
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents?status=all",
                headers={"Authorization": "Bearer user-token"},
            )

        assert r.status_code == 200, r.text
        for ag in r.json()["agents"]:
            assert ag["subnet_ids"] == ["public"]

    def test_admin_sees_full_subnet_ids(self, monkeypatch):
        """acn:admin on list → full subnet_ids per agent."""
        monkeypatch.setattr(
            "acn.routes.registry.verify_token",
            _fake_verify_token(
                sub="admin-user",
                caller_type="user",
                permissions=["acn:admin"],
            ),
        )
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents?status=all",
                headers={"Authorization": "Bearer admin-token"},
            )

        assert r.status_code == 200, r.text
        for ag in r.json()["agents"]:
            assert "subnet-private-abc" in ag["subnet_ids"]
