"""Tests for ``PATCH /api/v1/agents/{id}/endpoint``.

This is the pull→push upgrade path. Before it existed, an agent that
registered without an endpoint (manifest / pull-only mode) had no way to
start receiving direct delivery short of re-registering — which mints a
new ``agent_id`` and orphans the agent's identity, reputation, and subnet
memberships. SKILL.md documented this upgrade flow against an endpoint
that did not exist; this route makes the documentation true.

Contract pinned here (route → service seam):

* **Authorization** — only the agent itself (Bearer API key) or
  ACN-internal tooling (X-Internal-Token) may mutate the endpoint.
  Anonymous and cross-agent callers fail before persistence.
* **Set** — a provided URL is reachability-probed (hard-fail 400, same
  as registration); on success the service persists it and the response
  reports ``endpoint_reachable=True``.
* **Validation** — the shared ``_validate_agent_endpoint_url`` helper
  rejects the ACN gateway host at request-parse time (422), identical to
  registration.
* **Clear** — ``endpoint=null`` reverts to pull-only, but is rejected
  (400) while the agent is in a push mode (``open`` / ``allowlist``)
  because that would advertise a delivery mode with nowhere to deliver.
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
    """AgentService stub with a configurable stored mode.

    Set ``svc.stored_mode`` to control what ``get_agent`` reports as the
    target agent's current ``communication_policy.mode`` — the clear-path
    guard branches on it. ``update_endpoint`` echoes the input into a
    MagicMock so tests can assert what was persisted.
    """
    svc = AsyncMock()
    svc.stored_mode = "manifest"

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
            return existing
        raise AgentNotFoundException(agent_id)

    svc.get_agent = AsyncMock(side_effect=_get_agent)

    async def _update_endpoint(agent_id: str, endpoint):
        if agent_id != "agent-target":
            raise AgentNotFoundException(agent_id)
        result = MagicMock()
        result.agent_id = agent_id
        result.endpoint = endpoint or None
        result.a2a_endpoint = endpoint or None
        return result

    svc.update_endpoint = AsyncMock(side_effect=_update_endpoint)
    return svc


def _wire(svc) -> None:
    app.dependency_overrides[get_agent_service] = lambda: svc


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


class TestAuth:
    def test_anonymous_returns_401(self, stub_agent_service):
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/endpoint",
                json={"endpoint": "https://agent.example.com/a2a"},
            )
        assert r.status_code == 401, r.text
        stub_agent_service.update_endpoint.assert_not_awaited()

    def test_cross_agent_key_returns_403(self, stub_agent_service):
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/endpoint",
                json={"endpoint": "https://agent.example.com/a2a"},
                headers={"Authorization": "Bearer other-key"},
            )
        assert r.status_code == 403, r.text
        stub_agent_service.update_endpoint.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Set endpoint (pull → push upgrade)
# --------------------------------------------------------------------------- #


class TestSetEndpoint:
    def test_set_reachable_endpoint_persists_and_reports_reachable(
        self, stub_agent_service
    ):
        _wire(stub_agent_service)
        with patch(
            "acn.routes.registry._check_endpoint_reachability",
            new=AsyncMock(return_value=True),
        ):
            with TestClient(app) as client:
                r = client.patch(
                    "/api/v1/agents/agent-target/endpoint",
                    json={"endpoint": "https://agent.example.com/a2a"},
                    headers={"Authorization": "Bearer owner-key"},
                )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["endpoint"] == "https://agent.example.com/a2a"
        assert body["endpoint_reachable"] is True
        stub_agent_service.update_endpoint.assert_awaited_once()
        assert (
            stub_agent_service.update_endpoint.await_args.kwargs["endpoint"]
            == "https://agent.example.com/a2a"
        )

    def test_set_unreachable_endpoint_hard_fails_without_persisting(
        self, stub_agent_service
    ):
        """Reachability is a hard block at registration; the upgrade path
        must apply the same gate so a dead URL can't black-hole inbound."""
        _wire(stub_agent_service)
        from fastapi import HTTPException

        with patch(
            "acn.routes.registry._check_endpoint_reachability",
            new=AsyncMock(
                side_effect=HTTPException(
                    status_code=400, detail="Endpoint did not respond"
                )
            ),
        ):
            with TestClient(app) as client:
                r = client.patch(
                    "/api/v1/agents/agent-target/endpoint",
                    json={"endpoint": "https://agent.example.com/a2a"},
                    headers={"Authorization": "Bearer owner-key"},
                )

        assert r.status_code == 400, r.text
        stub_agent_service.update_endpoint.assert_not_awaited()

    def test_acn_gateway_host_rejected_at_validation(self, stub_agent_service):
        """Same gateway-host guard as registration — must 422 before the
        route body runs, so no probe / persistence happens."""
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/endpoint",
                json={"endpoint": "https://api.acnlabs.dev/api/v1/agents/x"},
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 422, r.text
        stub_agent_service.update_endpoint.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Clear endpoint (push → pull downgrade)
# --------------------------------------------------------------------------- #


class TestClearEndpoint:
    def test_clear_in_manifest_mode_succeeds(self, stub_agent_service):
        stub_agent_service.stored_mode = "manifest"
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/endpoint",
                json={"endpoint": None},
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["endpoint"] is None
        assert body["endpoint_reachable"] is False
        stub_agent_service.update_endpoint.assert_awaited_once()
        assert stub_agent_service.update_endpoint.await_args.kwargs["endpoint"] is None

    def test_clear_in_open_mode_rejected(self, stub_agent_service):
        """Clearing while ``open`` would leave the agent advertising push
        delivery with no endpoint — must 400 and not persist."""
        stub_agent_service.stored_mode = "open"
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/endpoint",
                json={"endpoint": None},
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 400, r.text
        assert "endpoint_required_for_mode" in r.text
        stub_agent_service.update_endpoint.assert_not_awaited()

    def test_clear_in_allowlist_mode_rejected(self, stub_agent_service):
        stub_agent_service.stored_mode = "allowlist"
        _wire(stub_agent_service)
        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/endpoint",
                json={"endpoint": None},
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 400, r.text
        stub_agent_service.update_endpoint.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Not found
# --------------------------------------------------------------------------- #


def test_unknown_agent_returns_404(stub_agent_service):
    """An owner key that resolves, but a path agent_id that doesn't exist.

    Use the internal token so auth passes regardless of ownership, then
    the route's own ``get_agent`` 404s.
    """
    _wire(stub_agent_service)
    valid_internal = "test-internal-token-min-32-chars-padding"
    with patch(
        "acn.routes.dependencies.settings.internal_api_token",
        valid_internal,
    ):
        with patch(
            "acn.routes.registry._check_endpoint_reachability",
            new=AsyncMock(return_value=True),
        ):
            with TestClient(app) as client:
                r = client.patch(
                    "/api/v1/agents/ghost-agent/endpoint",
                    json={"endpoint": "https://agent.example.com/a2a"},
                    headers={"X-Internal-Token": valid_internal},
                )
    assert r.status_code == 404, r.text
