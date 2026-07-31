"""Tests for ``POST /api/v1/agents/{id}/performance/refresh``."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException
from acn.routes.dependencies import (
    _api_key_cache,
    get_agent_service,
    limiter,
)
from acn.routes.tasks import set_task_service

VALID_INTERNAL_TOKEN = "test-internal-token-min-32-chars-padding"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    limiter.enabled = False
    _api_key_cache.clear()
    monkeypatch.setenv("INTERNAL_API_TOKEN", VALID_INTERNAL_TOKEN)
    yield
    limiter.enabled = True
    _api_key_cache.clear()
    app.dependency_overrides.clear()


@pytest.fixture
def stub_services():
    agent_svc = AsyncMock()
    target = MagicMock()
    target.agent_id = "agent-target"
    target.metadata = {}

    async def _by_api_key(key: str):
        if key == "owner-key":
            return target
        return None

    agent_svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)

    async def _get_agent(agent_id: str):
        if agent_id != "agent-target":
            raise AgentNotFoundException(agent_id)
        return target

    agent_svc.get_agent = AsyncMock(side_effect=_get_agent)

    async def _refresh(agent_id: str, items, *, min_samples=3):
        return {
            "settled": 3,
            "success": 2,
            "completion_rate": 0.6667,
            "updated_at": "2026-07-31T00:00:00Z",
        }

    agent_svc.refresh_performance_from_history = AsyncMock(side_effect=_refresh)

    task_svc = AsyncMock()
    task_svc.get_agent_task_history = AsyncMock(
        return_value=[
            {"status": "completed"},
            {"status": "completed"},
            {"status": "rejected"},
        ]
    )

    app.dependency_overrides[get_agent_service] = lambda: agent_svc
    set_task_service(task_svc)
    return agent_svc, task_svc


def test_refresh_performance_owner_key(stub_services):
    agent_svc, task_svc = stub_services
    client = TestClient(app)
    resp = client.post(
        "/api/v1/agents/agent-target/performance/refresh",
        headers={"Authorization": "Bearer owner-key"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == "agent-target"
    assert body["performance"]["completion_rate"] == 0.6667
    task_svc.get_agent_task_history.assert_awaited_once()
    agent_svc.refresh_performance_from_history.assert_awaited_once()


def test_refresh_performance_internal_token(stub_services):
    agent_svc, task_svc = stub_services
    with patch(
        "acn.routes.dependencies.settings.internal_api_token",
        VALID_INTERNAL_TOKEN,
    ):
        client = TestClient(app)
        resp = client.post(
            "/api/v1/agents/agent-target/performance/refresh",
            headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == "agent-target"
    assert body["performance"]["completion_rate"] == 0.6667
    task_svc.get_agent_task_history.assert_awaited_once()
    agent_svc.refresh_performance_from_history.assert_awaited_once()


def test_refresh_performance_not_found(stub_services):
    client = TestClient(app)
    resp = client.post(
        "/api/v1/agents/missing/performance/refresh",
        headers={"Authorization": "Bearer owner-key"},
    )
    assert resp.status_code in (403, 404), resp.text
