"""ACL V6 B8 — ?confirm=true safety guard on destructive DELETE endpoints.

Both ``DELETE /api/v1/subnets/{slug}`` and
``DELETE /api/v1/agents/{agent_id}`` now require ``?confirm=true``.
Omitting it or passing ``?confirm=false`` must return ``400 INVALID_REQUEST``
with a hint in ``details``, leaving the resource untouched.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException, SubnetNotFoundException
from acn.routes.dependencies import get_agent_service, get_subnet_service

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_subnet(slug: str = "subnet-1", owner: str = "agent-owner") -> MagicMock:
    s = MagicMock()
    s.slug = slug
    s.owner = owner
    s.is_private = False
    s.is_public = True
    s.member_count = 0
    s.name = "Test Subnet"
    s.description = None
    s.security_config = {}
    s.created_at = "2026-01-01T00:00:00Z"
    s.metadata = {}
    s.parent_slug = None
    s.lifecycle = "persistent"
    return s


@pytest.fixture
def stub_agent_service():
    svc = AsyncMock()
    owner_agent = MagicMock()
    owner_agent.agent_id = "agent-owner"
    owner_agent.name = "Owner"

    async def _by_api_key(key: str):
        if key == "owner-key":
            return owner_agent
        return None

    async def _get_agent(agent_id: str):
        if agent_id == "agent-owner":
            return owner_agent
        raise AgentNotFoundException(agent_id)

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)
    svc.get_agent = AsyncMock(side_effect=_get_agent)
    svc.search_agents = AsyncMock(return_value=[])
    svc.unregister_agent = AsyncMock(return_value=True)
    svc.is_alive = AsyncMock(return_value=True)
    svc.batch_alive = AsyncMock(return_value={"agent-owner"})
    return svc


@pytest.fixture
def stub_subnet_service():
    svc = AsyncMock()
    subnet = _make_subnet()

    async def _get_subnet(slug: str):
        if slug == "subnet-1":
            return subnet
        raise SubnetNotFoundException(slug)

    svc.get_subnet = AsyncMock(side_effect=_get_subnet)
    svc.delete_subnet = AsyncMock(return_value=True)
    svc.list_public_subnets = AsyncMock(return_value=[subnet])
    svc.list_subnets = AsyncMock(return_value=[])
    svc.get_subnet_children = AsyncMock(return_value=[])
    return svc


@pytest.fixture(autouse=True)
def wire(stub_agent_service, stub_subnet_service):
    app.dependency_overrides[get_agent_service] = lambda: stub_agent_service
    app.dependency_overrides[get_subnet_service] = lambda: stub_subnet_service
    yield
    app.dependency_overrides.pop(get_agent_service, None)
    app.dependency_overrides.pop(get_subnet_service, None)


# ---------------------------------------------------------------------------
# Helper — stub require_permission("acn:write") for agent DELETE tests
# ---------------------------------------------------------------------------


def _fake_require_permission(sub: str, permissions: list[str] | None = None):
    """Return a dependency factory that patches ``require_permission``."""

    async def _impl(*args, **kwargs):
        return {"sub": sub, "type": "user", "permissions": permissions or ["acn:write"]}

    return _impl


# ---------------------------------------------------------------------------
# DELETE /subnets/{slug}
# ---------------------------------------------------------------------------


class TestDeleteSubnetConfirmGuard:
    def test_missing_confirm_returns_400(self):
        """No ``?confirm`` → 400 INVALID_REQUEST, subnet untouched."""
        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/subnets/subnet-1",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 400, r.text
        body = r.json()
        assert body["error_code"] == "invalid_request"
        assert "confirm=true" in body["details"].get("hint", "").lower()

    def test_confirm_false_returns_400(self):
        """``?confirm=false`` is not accepted."""
        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/subnets/subnet-1?confirm=false",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 400, r.text
        assert r.json()["error_code"] == "invalid_request"

    def test_confirm_true_deletes_subnet(self, stub_subnet_service):
        """``?confirm=true`` proceeds to delete and returns 200."""
        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/subnets/subnet-1?confirm=true",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        assert r.json() == {"status": "deleted", "slug": "subnet-1"}
        stub_subnet_service.delete_subnet.assert_awaited_once_with("subnet-1", "agent-owner")

    def test_missing_confirm_does_not_call_service(self, stub_subnet_service):
        """Service must never be called when confirm is absent."""
        with TestClient(app) as client:
            client.delete(
                "/api/v1/subnets/subnet-1",
                headers={"Authorization": "Bearer owner-key"},
            )

        stub_subnet_service.delete_subnet.assert_not_awaited()


# ---------------------------------------------------------------------------
# DELETE /agents/{agent_id}
# ---------------------------------------------------------------------------


class TestDeleteAgentConfirmGuard:
    def test_missing_confirm_returns_400(self, monkeypatch):
        """No ``?confirm`` → 400 INVALID_REQUEST, agent untouched."""
        from acn.routes import registry as reg_module

        monkeypatch.setattr(
            reg_module,
            "require_permission",
            lambda _perm: _fake_require_permission("agent-owner"),
        )
        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/agents/agent-owner",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 400, r.text
        body = r.json()
        assert body["error_code"] == "invalid_request"
        assert "confirm=true" in body["details"].get("hint", "").lower()

    def test_confirm_false_returns_400(self, monkeypatch):
        from acn.routes import registry as reg_module

        monkeypatch.setattr(
            reg_module,
            "require_permission",
            lambda _perm: _fake_require_permission("agent-owner"),
        )
        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/agents/agent-owner?confirm=false",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 400, r.text
        assert r.json()["error_code"] == "invalid_request"

    def test_confirm_true_unregisters_agent(self, monkeypatch, stub_agent_service):
        """``?confirm=true`` proceeds to unregister and returns 200."""
        from acn.routes import registry as reg_module

        monkeypatch.setattr(
            reg_module,
            "require_permission",
            lambda _perm: _fake_require_permission("agent-owner"),
        )
        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/agents/agent-owner?confirm=true",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        assert r.json() == {"status": "unregistered", "agent_id": "agent-owner"}
        # Verify the service was reached (exact token_owner depends on auth mode)
        stub_agent_service.unregister_agent.assert_awaited_once()

    def test_missing_confirm_does_not_call_service(self, monkeypatch, stub_agent_service):
        """Service must never be called when confirm is absent."""
        from acn.routes import registry as reg_module

        monkeypatch.setattr(
            reg_module,
            "require_permission",
            lambda _perm: _fake_require_permission("agent-owner"),
        )
        with TestClient(app) as client:
            client.delete(
                "/api/v1/agents/agent-owner",
                headers={"Authorization": "Bearer owner-key"},
            )

        stub_agent_service.unregister_agent.assert_not_awaited()
