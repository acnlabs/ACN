"""Tests for ``GET /api/v1/agents/{id}/endpoint`` access control.

Phase 1 review finding (L421):
    The endpoint formerly returned the agent's *real* backend URL
    to anonymous callers. That URL is the one piece of data the
    ACN proxy was designed to hide — once a caller has it, they
    can speak to the agent without ever entering ACN, defeating
    every ``communication_policy`` gate (proxy / router /
    subnet_manager). Treating the endpoint as low-sensitivity made
    the entire ``closed`` mode toothless against any attacker who
    could enumerate agent IDs.

The fix wires ``OwnerOrInternalDep`` (``verify_owner_or_internal``)
in front of the route. These tests pin the wire-level contract:

* anonymous callers get **401** with no leakage,
* a Bearer key for a *different* agent gets **403**,
* a Bearer key for the *same* agent gets the real endpoint back —
  the agent introspecting its own metadata stays a supported
  workflow,
* a valid ``X-Internal-Token`` returns the real endpoint without
  needing an API key (ops / platform path),
* an *invalid* ``X-Internal-Token`` fails closed with **403**
  rather than silently falling through to API-key auth — a
  half-correct internal token is far more likely to be
  misconfiguration than an attacker who *also* has a valid owner
  API key, and conflating the two would mask the misconfig.

Why the route still 404s on missing agents: the ``Agent
not found`` shape predates the auth gate and is thrown after
authorization succeeds. We pin that the auth gate runs *first*
(401/403 before 404) so an unauthenticated probe cannot use
404-vs-200 timing to enumerate which IDs exist.
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


VALID_INTERNAL_TOKEN = "test-internal-token-min-32-chars-padding"


@pytest.fixture(autouse=True)
def _reset_state():
    # Disable the SlowAPI limiter — these tests don't exercise rate
    # limiting and an enabled limiter would re-introduce a Redis
    # dependency for the limiter's own bookkeeping.
    limiter.enabled = False
    # Auth-key cache is process-global. Without clearing it, a Bearer
    # key resolved by a previous test would short-circuit
    # ``_resolve_agent_by_bearer`` and bypass the per-test mocks.
    _api_key_cache.clear()
    yield
    limiter.enabled = True
    _api_key_cache.clear()
    app.dependency_overrides.clear()


@pytest.fixture
def stub_agent_service():
    """Return a service stub that knows about a single agent.

    ``get_agent_by_api_key`` resolves ``"owner-key"`` to
    ``agent-target`` (so owner-API-key auth maps to the right
    agent_id) and rejects everything else with ``None``.
    ``get_agent`` returns a real-endpoint-bearing entity.
    """
    svc = AsyncMock()

    target = MagicMock()
    target.agent_id = "agent-target"
    target.name = "Target"
    target.endpoint = "https://target.example.com/a2a"

    other = MagicMock()
    other.agent_id = "agent-other"
    other.name = "Other"
    other.endpoint = "https://other.example.com/a2a"

    async def _by_api_key(key: str):
        if key == "owner-key":
            return target
        if key == "other-key":
            return other
        return None

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)

    async def _get_agent(agent_id: str):
        if agent_id == "agent-target":
            return target
        raise AgentNotFoundException(agent_id)

    svc.get_agent = AsyncMock(side_effect=_get_agent)
    return svc


def _wire_overrides(svc) -> None:
    app.dependency_overrides[get_agent_service] = lambda: svc


# --------------------------------------------------------------------------- #
# Anonymous / missing credentials
# --------------------------------------------------------------------------- #


class TestAnonymousAccessRejected:
    def test_no_auth_header_returns_401(self, stub_agent_service):
        """Phase 1 hard requirement: no anonymous read of real
        endpoint. The 401 fires *before* any agent lookup so it
        can't be used to time-side-channel which IDs exist."""
        _wire_overrides(stub_agent_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/agents/agent-target/endpoint")

        assert r.status_code == 401, r.text
        # Don't leak existence: 401 must come from the auth gate, not
        # from a downstream "agent not found" path.
        stub_agent_service.get_agent.assert_not_awaited()

    def test_malformed_authorization_header_returns_401(self, stub_agent_service):
        """A non-Bearer ``Authorization`` (e.g. Basic auth, garbled
        prefix) must not silently fall through to anonymous access."""
        _wire_overrides(stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/agent-target/endpoint",
                headers={"Authorization": "Basic dXNlcjpwYXNz"},
            )

        assert r.status_code == 401, r.text
        stub_agent_service.get_agent.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Owner via API key
# --------------------------------------------------------------------------- #


class TestOwnerApiKeyAccess:
    def test_owner_key_for_same_agent_returns_endpoint(self, stub_agent_service):
        """The agent introspecting its own real endpoint stays a
        first-class workflow — that's the legitimate non-internal
        use case the gate must keep working."""
        _wire_overrides(stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/agent-target/endpoint",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        assert r.json() == {
            "agent_id": "agent-target",
            "endpoint": "https://target.example.com/a2a",
        }

    def test_owner_key_for_different_agent_returns_403(self, stub_agent_service):
        """A holder of a *valid* API key must NOT be able to read
        any other agent's endpoint — otherwise the "owner-only"
        promise is just "any-agent". This is the central
        cross-tenant boundary."""
        _wire_overrides(stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/agent-target/endpoint",
                headers={"Authorization": "Bearer other-key"},
            )

        assert r.status_code == 403, r.text
        # Specifically pin "API key does not match agent_id" — same
        # body shape as the heartbeat path, so callers can write a
        # single error handler.
        assert "match" in r.json()["detail"].lower()
        # The 403 fires before the entity lookup — confirm by checking
        # we never reached ``get_agent``.
        stub_agent_service.get_agent.assert_not_awaited()

    def test_invalid_api_key_returns_401(self, stub_agent_service):
        """An unrecognised Bearer key gets 401 (not 403): we want a
        consistent shape with ``verify_agent_api_key`` so an
        attacker can't distinguish "valid-but-wrong-agent" from
        "totally bogus key" via response code."""
        _wire_overrides(stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/agent-target/endpoint",
                headers={"Authorization": "Bearer totally-bogus-key"},
            )

        assert r.status_code == 401, r.text


# --------------------------------------------------------------------------- #
# Internal token
# --------------------------------------------------------------------------- #


class TestInternalTokenAccess:
    def test_valid_internal_token_returns_endpoint(self, stub_agent_service):
        _wire_overrides(stub_agent_service)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.get(
                    "/api/v1/agents/agent-target/endpoint",
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        assert r.status_code == 200, r.text
        assert r.json() == {
            "agent_id": "agent-target",
            "endpoint": "https://target.example.com/a2a",
        }

    def test_wrong_internal_token_fails_closed(self, stub_agent_service):
        """A *present-but-wrong* internal token returns 403 instead
        of falling through to API-key auth. Reasoning: a half-
        correct internal token is far more likely a misconfigured
        ops tool than an attacker who *also* has a valid owner API
        key, and silently routing it through the API-key path would
        mask the misconfig (the operator would see 200s and never
        notice their token rotation didn't propagate)."""
        _wire_overrides(stub_agent_service)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.get(
                    "/api/v1/agents/agent-target/endpoint",
                    headers={
                        "X-Internal-Token": "wrong-token",
                        # Even providing a valid owner key on the
                        # side does not rescue the request — the
                        # internal-token branch fails closed.
                        "Authorization": "Bearer owner-key",
                    },
                )

        assert r.status_code == 403, r.text
        # Don't reach the agent lookup — auth must fail before any
        # downstream service call.
        stub_agent_service.get_agent.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Auth precedes 404 — no enumeration via response code
# --------------------------------------------------------------------------- #


class TestAuthPrecedesNotFound:
    def test_anonymous_request_for_unknown_agent_still_returns_401(
        self, stub_agent_service
    ):
        """Pin auth-precedes-existence: an unauthenticated probe of
        a non-existent agent ID must look identical (401) to a
        probe of a known agent. Otherwise 401 vs 404 leaks the
        agent-ID space.

        Concretely: ``stub_agent_service.get_agent`` would raise
        ``AgentNotFoundException`` for any unknown ID. We assert the
        401 wins, meaning the route never invokes ``get_agent``.
        """
        _wire_overrides(stub_agent_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/agents/agent-does-not-exist/endpoint")

        assert r.status_code == 401, r.text
        stub_agent_service.get_agent.assert_not_awaited()

    def test_internal_token_for_unknown_agent_returns_404(self, stub_agent_service):
        """Once authorized, the existence signal is fine — internal
        callers (or the agent itself) are entitled to know whether
        an ID resolves. The 404 shape matches the pre-fix
        behaviour."""
        _wire_overrides(stub_agent_service)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.get(
                    "/api/v1/agents/agent-does-not-exist/endpoint",
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        assert r.status_code == 404, r.text
