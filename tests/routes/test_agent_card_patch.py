"""Tests for ``PATCH /api/v1/agents/{id}/agent-card``."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException
from acn.routes.dependencies import _api_key_cache, get_agent_service, limiter


@pytest.fixture(autouse=True)
def _reset_state():
    limiter.enabled = False
    _api_key_cache.clear()
    yield
    limiter.enabled = True
    _api_key_cache.clear()
    app.dependency_overrides.clear()


@pytest.fixture
def stub_agent_service():
    svc = AsyncMock()
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

    async def _update(
        agent_id: str,
        *,
        agent_card=None,
        agent_card_url=None,
        update_card=False,
        update_card_url=False,
    ):
        if agent_id != "agent-target":
            raise AgentNotFoundException(agent_id)
        result = MagicMock()
        result.agent_id = agent_id
        result.agent_card = agent_card if update_card else {"name": "Stored"}
        result.agent_card_url = (
            agent_card_url if update_card_url else "https://stored.example/card.json"
        )
        return result

    svc.update_agent_card = AsyncMock(side_effect=_update)
    return svc


def _wire(svc) -> None:
    app.dependency_overrides[get_agent_service] = lambda: svc


def test_anonymous_returns_401(stub_agent_service):
    _wire(stub_agent_service)
    with TestClient(app) as client:
        r = client.patch(
            "/api/v1/agents/agent-target/agent-card",
            json={"agent_card_url": "https://evil.example/card.json"},
        )
    assert r.status_code == 401, r.text
    stub_agent_service.update_agent_card.assert_not_awaited()


def test_cross_agent_key_returns_403(stub_agent_service):
    _wire(stub_agent_service)
    with TestClient(app) as client:
        r = client.patch(
            "/api/v1/agents/agent-target/agent-card",
            json={"agent_card_url": "https://evil.example/card.json"},
            headers={"Authorization": "Bearer other-key"},
        )
    assert r.status_code == 403, r.text
    stub_agent_service.update_agent_card.assert_not_awaited()


def test_empty_body_rejected(stub_agent_service):
    _wire(stub_agent_service)
    with TestClient(app) as client:
        r = client.patch(
            "/api/v1/agents/agent-target/agent-card",
            json={},
            headers={"Authorization": "Bearer owner-key"},
        )
    assert r.status_code == 422, r.text
    stub_agent_service.update_agent_card.assert_not_awaited()


def test_owner_replaces_card_and_url(stub_agent_service):
    _wire(stub_agent_service)
    card = {"name": "MyAgent", "url": "https://agent.example/a2a"}
    with TestClient(app) as client:
        r = client.patch(
            "/api/v1/agents/agent-target/agent-card",
            json={
                "agent_card": card,
                "agent_card_url": "https://agent.example/.well-known/agent-card.json",
            },
            headers={"Authorization": "Bearer owner-key"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_card"]["name"] == "MyAgent"
    assert body["agent_card_url"].endswith("agent-card.json")
    kwargs = stub_agent_service.update_agent_card.await_args.kwargs
    assert kwargs["update_card"] is True
    assert kwargs["update_card_url"] is True


def test_empty_card_clears_snapshot(stub_agent_service):
    _wire(stub_agent_service)
    with TestClient(app) as client:
        r = client.patch(
            "/api/v1/agents/agent-target/agent-card",
            json={"agent_card": {}},
            headers={"Authorization": "Bearer owner-key"},
        )
    assert r.status_code == 200, r.text
    kwargs = stub_agent_service.update_agent_card.await_args.kwargs
    assert kwargs["agent_card"] is None


def test_null_clears_card_only(stub_agent_service):
    _wire(stub_agent_service)
    with TestClient(app) as client:
        r = client.patch(
            "/api/v1/agents/agent-target/agent-card",
            json={"agent_card": None},
            headers={"Authorization": "Bearer owner-key"},
        )
    assert r.status_code == 200, r.text
    kwargs = stub_agent_service.update_agent_card.await_args.kwargs
    assert kwargs["update_card"] is True
    assert kwargs["agent_card"] is None
    assert kwargs["update_card_url"] is False
    assert r.json()["agent_card"] is None
    assert r.json()["agent_card_url"] == "https://stored.example/card.json"


def test_non_http_url_rejected(stub_agent_service):
    _wire(stub_agent_service)
    with TestClient(app) as client:
        r = client.patch(
            "/api/v1/agents/agent-target/agent-card",
            json={"agent_card_url": "ftp://example.com/card.json"},
            headers={"Authorization": "Bearer owner-key"},
        )
    assert r.status_code == 422, r.text
    stub_agent_service.update_agent_card.assert_not_awaited()
