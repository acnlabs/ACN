"""Tests for agent self-deletion (unclaimed direct + claimed request/confirm).

Before this, ``DELETE /agents/{id}`` required an Auth0 owner JWT, so a
self-registered agent (API key only, never claimed) could never be
deleted — a permanent dead row. And a claimed agent had no way to ask
to be removed without its human owner doing it manually.

Two layers are pinned here:

* **Service** (``AgentService``) — the request/confirm/cancel state
  machine: token hashing, expiry, owner check, unclaimed guard.
* **Routes** — the auth branching on ``POST /{id}/deletion-request``
  (unclaimed/internal → immediate; claimed API key → pending), the
  owner-confirmed ``/confirm`` path, cancel, ADR-0006 subnet guard, and
  the redacted ``pending_deletion`` serializer marker.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.entities import Agent, ClaimStatus
from acn.core.exceptions import AgentNotFoundException
from acn.routes.dependencies import (
    _api_key_cache,
    get_agent_service,
    get_subnet_service,
    limiter,
)
from acn.services.agent_service import AgentService, hash_api_key

# =========================================================================
# Service-layer unit tests (in-memory fake repository)
# =========================================================================


class _FakeRepo:
    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}

    async def find_by_id(self, agent_id: str):
        return self.agents.get(agent_id)

    async def save(self, agent: Agent):
        self.agents[agent.agent_id] = agent

    async def delete(self, agent_id: str) -> bool:
        return self.agents.pop(agent_id, None) is not None


def _make_service() -> tuple[AgentService, _FakeRepo]:
    repo = _FakeRepo()
    svc = AgentService(repo)
    svc.follow_service = None
    svc.payment_discovery = None
    return svc, repo


def _claimed_agent(agent_id: str = "agent-1", owner: str = "user-x") -> Agent:
    return Agent(
        agent_id=agent_id,
        name="Claimed Agent",
        owner=owner,
        claim_status=ClaimStatus.CLAIMED,
    )


@pytest.mark.asyncio
async def test_request_deletion_stores_hashed_token_and_expiry():
    svc, repo = _make_service()
    await repo.save(_claimed_agent())

    agent, token = await svc.request_deletion("agent-1")

    req = agent.metadata["deletion_request"]
    assert req["token_hash"] == hash_api_key(token)
    # Plaintext token must NOT be stored anywhere on the entity.
    assert token not in str(agent.metadata)
    assert datetime.fromisoformat(req["expires_at"]) > datetime.now(UTC)


@pytest.mark.asyncio
async def test_request_deletion_rejects_unclaimed():
    svc, repo = _make_service()
    await repo.save(Agent(agent_id="a2", name="Solo", owner=None, claim_status=ClaimStatus.UNCLAIMED))
    with pytest.raises(ValueError, match="unclaimed"):
        await svc.request_deletion("a2")


@pytest.mark.asyncio
async def test_confirm_deletion_happy_path_deletes():
    svc, repo = _make_service()
    await repo.save(_claimed_agent())
    _, token = await svc.request_deletion("agent-1")

    result = await svc.confirm_deletion("agent-1", "user-x", token)

    assert result is True
    assert await repo.find_by_id("agent-1") is None


@pytest.mark.asyncio
async def test_confirm_deletion_wrong_owner_raises_permission():
    svc, repo = _make_service()
    await repo.save(_claimed_agent())
    _, token = await svc.request_deletion("agent-1")
    with pytest.raises(PermissionError):
        await svc.confirm_deletion("agent-1", "user-intruder", token)
    # Agent must survive a failed confirm.
    assert await repo.find_by_id("agent-1") is not None


@pytest.mark.asyncio
async def test_confirm_deletion_bad_token_raises():
    svc, repo = _make_service()
    await repo.save(_claimed_agent())
    await svc.request_deletion("agent-1")
    with pytest.raises(ValueError, match="[Ii]nvalid"):
        await svc.confirm_deletion("agent-1", "user-x", "not-the-token")


@pytest.mark.asyncio
async def test_confirm_deletion_no_pending_request_raises():
    svc, repo = _make_service()
    await repo.save(_claimed_agent())
    with pytest.raises(ValueError, match="[Nn]o pending"):
        await svc.confirm_deletion("agent-1", "user-x", "whatever")


@pytest.mark.asyncio
async def test_confirm_deletion_expired_request_raises_and_clears():
    svc, repo = _make_service()
    await repo.save(_claimed_agent())
    _, token = await svc.request_deletion("agent-1")
    # Force the stored request into the past.
    agent = await repo.find_by_id("agent-1")
    agent.metadata["deletion_request"]["expires_at"] = (
        datetime.now(UTC) - timedelta(hours=1)
    ).isoformat()
    await repo.save(agent)

    with pytest.raises(ValueError, match="expired"):
        await svc.confirm_deletion("agent-1", "user-x", token)
    # Expired request is cleared; agent remains.
    refreshed = await repo.find_by_id("agent-1")
    assert refreshed is not None
    assert "deletion_request" not in (refreshed.metadata or {})


@pytest.mark.asyncio
async def test_cancel_deletion_clears_marker_idempotently():
    svc, repo = _make_service()
    await repo.save(_claimed_agent())
    await svc.request_deletion("agent-1")

    agent = await svc.cancel_deletion("agent-1")
    assert "deletion_request" not in (agent.metadata or {})
    # Idempotent: cancelling again is fine.
    agent2 = await svc.cancel_deletion("agent-1")
    assert "deletion_request" not in (agent2.metadata or {})


# =========================================================================
# Serializer: pending_deletion marker is surfaced, token_hash redacted
# =========================================================================


def test_serializer_redacts_token_hash_and_surfaces_marker():
    from acn.routes.registry import _agent_entity_to_info

    agent = _claimed_agent()
    agent.metadata = {
        "deletion_request": {
            "token_hash": "SECRET-HASH-MUST-NOT-LEAK",
            "requested_at": "2026-05-29T00:00:00+00:00",
            "expires_at": "2026-06-01T00:00:00+00:00",
        }
    }

    for strip in (True, False):
        info = _agent_entity_to_info(agent, is_online=True, strip_sensitive=strip)
        assert "deletion_request" not in info.metadata
        assert "SECRET-HASH-MUST-NOT-LEAK" not in str(info.metadata)
        assert info.metadata["pending_deletion"] == {
            "requested_at": "2026-05-29T00:00:00+00:00",
            "expires_at": "2026-06-01T00:00:00+00:00",
        }


# =========================================================================
# Route-layer tests
# =========================================================================

VALID_INTERNAL_TOKEN = "test-internal-token-min-32-chars-padding"


@pytest.fixture(autouse=True)
def _reset_state():
    limiter.enabled = False
    _api_key_cache.clear()
    yield
    limiter.enabled = True
    _api_key_cache.clear()
    app.dependency_overrides.clear()


@pytest.fixture
def stub_services():
    """Returns (agent_service, subnet_service) AsyncMock stubs.

    Set ``svc.target_owner`` to ``None`` (unclaimed) or a string (claimed)
    to control the deletion-request branch. ``subnet_service.owned`` lets a
    test simulate the ADR-0006 'still owns subnets' rejection.
    """
    svc = AsyncMock()
    svc.target_owner = None  # unclaimed by default

    target = MagicMock()
    target.agent_id = "agent-target"

    other = MagicMock()
    other.agent_id = "agent-other"

    async def _by_api_key(key: str):
        if key == "owner-key":
            return target
        if key == "other-key":
            return other
        return None

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)

    async def _get_agent(agent_id: str):
        if agent_id == "agent-target":
            a = MagicMock()
            a.agent_id = agent_id
            a.owner = svc.target_owner
            a.claim_status = (
                ClaimStatus.CLAIMED if svc.target_owner else ClaimStatus.UNCLAIMED
            )
            a.metadata = {
                "deletion_request": {"expires_at": "2026-06-01T00:00:00+00:00"}
            }
            return a
        raise AgentNotFoundException(agent_id)

    svc.get_agent = AsyncMock(side_effect=_get_agent)
    svc.unregister_agent = AsyncMock(return_value=True)

    async def _request_deletion(agent_id: str):
        a = MagicMock()
        a.metadata = {"deletion_request": {"expires_at": "2026-06-01T00:00:00+00:00"}}
        return a, "plain-token"

    svc.request_deletion = AsyncMock(side_effect=_request_deletion)
    svc.confirm_deletion = AsyncMock(return_value=True)
    svc.cancel_deletion = AsyncMock(return_value=MagicMock())

    subnet_svc = AsyncMock()
    subnet_svc.owned = []
    subnet_svc.list_subnets = AsyncMock(side_effect=lambda owner=None: subnet_svc.owned)

    return svc, subnet_svc


def _wire(svc, subnet_svc) -> None:
    app.dependency_overrides[get_agent_service] = lambda: svc
    app.dependency_overrides[get_subnet_service] = lambda: subnet_svc


class TestDeletionRequest:
    def test_unclaimed_self_delete_is_immediate(self, stub_services):
        svc, subnet_svc = stub_services
        svc.target_owner = None  # unclaimed
        _wire(svc, subnet_svc)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/deletion-request",
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "deleted"
        svc.unregister_agent.assert_awaited_once()
        svc.request_deletion.assert_not_awaited()

    def test_claimed_self_delete_is_pending(self, stub_services):
        svc, subnet_svc = stub_services
        svc.target_owner = "user-x"  # claimed
        _wire(svc, subnet_svc)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/deletion-request",
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "pending_confirmation"
        assert "/confirm-delete?token=" in body["confirm_url"]
        svc.request_deletion.assert_awaited_once()
        svc.unregister_agent.assert_not_awaited()

    def test_internal_token_force_deletes_claimed(self, stub_services):
        svc, subnet_svc = stub_services
        svc.target_owner = "user-x"  # claimed, but internal can force
        _wire(svc, subnet_svc)
        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/agents/agent-target/deletion-request",
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "deleted"
        svc.unregister_agent.assert_awaited_once()

    def test_anonymous_rejected(self, stub_services):
        svc, subnet_svc = stub_services
        _wire(svc, subnet_svc)
        with TestClient(app) as client:
            r = client.post("/api/v1/agents/agent-target/deletion-request")
        assert r.status_code == 401, r.text
        svc.unregister_agent.assert_not_awaited()

    def test_cross_agent_key_rejected(self, stub_services):
        svc, subnet_svc = stub_services
        _wire(svc, subnet_svc)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/deletion-request",
                headers={"Authorization": "Bearer other-key"},
            )
        assert r.status_code == 403, r.text
        svc.unregister_agent.assert_not_awaited()

    def test_owns_subnets_blocks_with_409(self, stub_services):
        svc, subnet_svc = stub_services
        svc.target_owner = None
        subnet_svc.owned = [MagicMock(slug="my-subnet")]
        _wire(svc, subnet_svc)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/deletion-request",
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 409, r.text
        svc.unregister_agent.assert_not_awaited()


class TestDeletionConfirm:
    def test_owner_confirm_deletes(self, stub_services, monkeypatch):
        svc, subnet_svc = stub_services
        svc.target_owner = "user-x"

        async def _fake_verify_token(*args, **kwargs):
            return {"sub": "user-x", "permissions": ["acn:write"], "type": "user"}

        monkeypatch.setattr("acn.auth.middleware.verify_token", _fake_verify_token)
        _wire(svc, subnet_svc)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/deletion-request/confirm",
                json={"token": "plain-token"},
                headers={"Authorization": "Bearer owner-jwt"},
            )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "deleted"
        svc.confirm_deletion.assert_awaited_once()

    def test_confirm_missing_permission_rejected(self, stub_services, monkeypatch):
        svc, subnet_svc = stub_services

        async def _fake_verify_token(*args, **kwargs):
            return {"sub": "user-x", "permissions": []}  # lacks acn:write

        monkeypatch.setattr("acn.auth.middleware.verify_token", _fake_verify_token)
        _wire(svc, subnet_svc)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/deletion-request/confirm",
                json={"token": "plain-token"},
                headers={"Authorization": "Bearer owner-jwt"},
            )
        assert r.status_code == 403, r.text
        svc.confirm_deletion.assert_not_awaited()

    def test_confirm_invalid_token_returns_400(self, stub_services, monkeypatch):
        svc, subnet_svc = stub_services
        svc.confirm_deletion = AsyncMock(side_effect=ValueError("Invalid deletion token."))

        async def _fake_verify_token(*args, **kwargs):
            return {"sub": "user-x", "permissions": ["acn:write"]}

        monkeypatch.setattr("acn.auth.middleware.verify_token", _fake_verify_token)
        _wire(svc, subnet_svc)
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/deletion-request/confirm",
                json={"token": "wrong"},
                headers={"Authorization": "Bearer owner-jwt"},
            )
        assert r.status_code == 400, r.text


class TestDeletionCancel:
    def test_cancel_returns_cancelled(self, stub_services):
        svc, subnet_svc = stub_services
        _wire(svc, subnet_svc)
        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/agents/agent-target/deletion-request",
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "cancelled"
        svc.cancel_deletion.assert_awaited_once()
