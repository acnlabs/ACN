"""Tests for ``GET/PATCH /api/v1/agents/{id}/delivery`` (ADR-0012 Mode A↔B).

Delivery transport is orthogonal to ``communication_policy.mode``. Runtime
routing still keys off endpoint presence; this surface lets a push-mode
agent migrate without re-joining.

Contract:

* Auth — owner API key or X-Internal-Token (same as ``/endpoint``).
* GET — derived ``direct`` | ``relay`` | ``none``.
* PATCH relay — requires push mode; clears endpoint via ``switch_to_relay``.
* PATCH direct — requires push mode + reachable endpoint.
* Bare ``PATCH /endpoint`` null while open still 400 (regression).

Note: ``TestClient`` is used *without* the context-manager form so the
app lifespan (Redis) is not entered — these tests stub AgentService only.
"""

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
from acn.routes.registry import _derive_delivery, _gateway_websocket_url


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
    svc.stored_mode = "open"
    svc.stored_endpoint = "https://agent.example.com/a2a"

    target = MagicMock()
    target.agent_id = "agent-target"
    target.name = "Target"

    other = MagicMock()
    other.agent_id = "agent-other"
    other.name = "Other"

    async def _by_api_key(key: str):
        if key == "owner-key":
            return target
        if key == "other-key":
            return other
        return None

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)

    async def _get_agent(agent_id: str):
        if agent_id == "agent-target":
            existing = MagicMock()
            existing.agent_id = agent_id
            existing.communication_policy = {"mode": svc.stored_mode}
            existing.endpoint = svc.stored_endpoint
            existing.a2a_endpoint = svc.stored_endpoint
            return existing
        raise AgentNotFoundException(agent_id)

    svc.get_agent = AsyncMock(side_effect=_get_agent)

    async def _switch_to_relay(agent_id: str):
        if agent_id != "agent-target":
            raise AgentNotFoundException(agent_id)
        svc.stored_endpoint = None
        result = MagicMock()
        result.agent_id = agent_id
        result.endpoint = None
        result.a2a_endpoint = None
        return result

    async def _set_direct(agent_id: str, endpoint: str):
        if agent_id != "agent-target":
            raise AgentNotFoundException(agent_id)
        svc.stored_endpoint = endpoint
        result = MagicMock()
        result.agent_id = agent_id
        result.endpoint = endpoint
        result.a2a_endpoint = endpoint
        return result

    async def _update_endpoint(agent_id: str, endpoint):
        if agent_id != "agent-target":
            raise AgentNotFoundException(agent_id)
        svc.stored_endpoint = endpoint or None
        result = MagicMock()
        result.agent_id = agent_id
        result.endpoint = endpoint or None
        result.a2a_endpoint = endpoint or None
        return result

    svc.switch_to_relay = AsyncMock(side_effect=_switch_to_relay)
    svc.set_direct_delivery = AsyncMock(side_effect=_set_direct)
    svc.update_endpoint = AsyncMock(side_effect=_update_endpoint)
    return svc


@pytest.fixture
def client(stub_agent_service):
    app.dependency_overrides[get_agent_service] = lambda: stub_agent_service
    # Do not enter the context manager — that would run lifespan (Redis).
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Derive helper
# --------------------------------------------------------------------------- #


class TestDeriveDelivery:
    def test_open_with_endpoint_is_direct(self):
        assert _derive_delivery(mode="open", endpoint="https://x/a2a") == "direct"

    def test_open_without_endpoint_is_relay(self):
        assert _derive_delivery(mode="open", endpoint=None) == "relay"

    def test_allowlist_without_endpoint_is_relay(self):
        assert _derive_delivery(mode="allowlist", endpoint=None) == "relay"

    def test_manifest_is_none(self):
        assert _derive_delivery(mode="manifest", endpoint=None) == "none"

    def test_closed_is_none(self):
        assert _derive_delivery(mode="closed", endpoint="https://x") == "none"


class TestGatewayWebsocketUrl:
    def test_https_becomes_wss(self):
        assert (
            _gateway_websocket_url("https://api.acnlabs.dev", "ag1")
            == "wss://api.acnlabs.dev/ws/ag1"
        )

    def test_http_becomes_ws(self):
        assert (
            _gateway_websocket_url("http://localhost:8000", "ag1")
            == "ws://localhost:8000/ws/ag1"
        )


# --------------------------------------------------------------------------- #
# GET
# --------------------------------------------------------------------------- #


class TestGetDelivery:
    def test_anonymous_returns_401(self, client):
        r = client.get("/api/v1/agents/agent-target/delivery")
        assert r.status_code == 401, r.text

    def test_open_with_endpoint_reports_direct(self, client):
        r = client.get(
            "/api/v1/agents/agent-target/delivery",
            headers={"Authorization": "Bearer owner-key"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["delivery"] == "direct"
        assert body["endpoint"] == "https://agent.example.com/a2a"
        assert body["communication_mode"] == "open"

    def test_open_without_endpoint_reports_relay(self, client, stub_agent_service):
        stub_agent_service.stored_endpoint = None
        r = client.get(
            "/api/v1/agents/agent-target/delivery",
            headers={"Authorization": "Bearer owner-key"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["delivery"] == "relay"

    def test_manifest_reports_none(self, client, stub_agent_service):
        stub_agent_service.stored_mode = "manifest"
        stub_agent_service.stored_endpoint = None
        r = client.get(
            "/api/v1/agents/agent-target/delivery",
            headers={"Authorization": "Bearer owner-key"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["delivery"] == "none"


# --------------------------------------------------------------------------- #
# PATCH A → B (relay)
# --------------------------------------------------------------------------- #


class TestPatchRelay:
    def test_open_to_relay_clears_endpoint(self, client, stub_agent_service):
        r = client.patch(
            "/api/v1/agents/agent-target/delivery",
            json={"delivery": "relay"},
            headers={"Authorization": "Bearer owner-key"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["delivery"] == "relay"
        assert body["endpoint"] is None
        assert body["communication_mode"] == "open"
        assert body["next_step_hint"]
        hint = body["next_step_hint"].lower()
        assert "listen" in hint
        assert "wss://" in hint or "ws://" in hint
        assert "get http" not in hint
        stub_agent_service.switch_to_relay.assert_awaited_once_with("agent-target")

    def test_relay_with_endpoint_rejected_at_validation(self, client, stub_agent_service):
        r = client.patch(
            "/api/v1/agents/agent-target/delivery",
            json={
                "delivery": "relay",
                "endpoint": "https://agent.example.com/a2a",
            },
            headers={"Authorization": "Bearer owner-key"},
        )
        assert r.status_code == 422, r.text
        stub_agent_service.switch_to_relay.assert_not_awaited()

    def test_manifest_to_relay_rejected(self, client, stub_agent_service):
        stub_agent_service.stored_mode = "manifest"
        stub_agent_service.stored_endpoint = None
        r = client.patch(
            "/api/v1/agents/agent-target/delivery",
            json={"delivery": "relay"},
            headers={"Authorization": "Bearer owner-key"},
        )
        assert r.status_code == 400, r.text
        assert "delivery_requires_push_mode" in r.text
        stub_agent_service.switch_to_relay.assert_not_awaited()

    def test_cross_agent_key_returns_403(self, client, stub_agent_service):
        r = client.patch(
            "/api/v1/agents/agent-target/delivery",
            json={"delivery": "relay"},
            headers={"Authorization": "Bearer other-key"},
        )
        assert r.status_code == 403, r.text
        stub_agent_service.switch_to_relay.assert_not_awaited()


# --------------------------------------------------------------------------- #
# PATCH B → A (direct)
# --------------------------------------------------------------------------- #


class TestPatchDirect:
    def test_relay_to_direct_persists_endpoint(self, client, stub_agent_service):
        stub_agent_service.stored_endpoint = None
        with patch(
            "acn.routes.registry._check_endpoint_reachability",
            new=AsyncMock(return_value=True),
        ), patch(
            "acn.routes.registry._probe_a2a_handshake",
            new=AsyncMock(return_value=True),
        ):
            r = client.patch(
                "/api/v1/agents/agent-target/delivery",
                json={
                    "delivery": "direct",
                    "endpoint": "https://agent.example.com/a2a",
                },
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["delivery"] == "direct"
        assert body["endpoint"] == "https://agent.example.com/a2a"
        assert body["a2a_handshake_ok"] is True
        stub_agent_service.set_direct_delivery.assert_awaited_once()

    def test_direct_without_endpoint_rejected(self, client, stub_agent_service):
        r = client.patch(
            "/api/v1/agents/agent-target/delivery",
            json={"delivery": "direct"},
            headers={"Authorization": "Bearer owner-key"},
        )
        assert r.status_code == 422, r.text
        stub_agent_service.set_direct_delivery.assert_not_awaited()

    def test_unreachable_endpoint_hard_fails(self, client, stub_agent_service):
        stub_agent_service.stored_endpoint = None
        from fastapi import HTTPException

        with patch(
            "acn.routes.registry._check_endpoint_reachability",
            new=AsyncMock(
                side_effect=HTTPException(
                    status_code=400, detail="Endpoint did not respond"
                )
            ),
        ):
            r = client.patch(
                "/api/v1/agents/agent-target/delivery",
                json={
                    "delivery": "direct",
                    "endpoint": "https://agent.example.com/a2a",
                },
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 400, r.text
        stub_agent_service.set_direct_delivery.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Regression: bare PATCH /endpoint clear in open still 400
# --------------------------------------------------------------------------- #


def test_bare_endpoint_clear_in_open_still_rejected(client, stub_agent_service):
    """Intentional A→B must go through /delivery — not a silent clear."""
    stub_agent_service.stored_mode = "open"
    r = client.patch(
        "/api/v1/agents/agent-target/endpoint",
        json={"endpoint": None},
        headers={"Authorization": "Bearer owner-key"},
    )
    assert r.status_code == 400, r.text
    assert "endpoint_required_for_mode" in r.text
    stub_agent_service.update_endpoint.assert_not_awaited()
    stub_agent_service.switch_to_relay.assert_not_awaited()
