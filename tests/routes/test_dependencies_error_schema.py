"""Auth dependencies — flat ACN error schema contract tests.

Phase 2 review v2 P1 #11 sprint row #10 — pin the 8 4xx raise
sites in ``acn/routes/dependencies.py`` to the canonical
``ACNHTTPError`` flat schema. This module is unique among the
migration sprints: it defines no router of its own — its raise
sites surface through *every* router that mounts an auth
dependency. The migration therefore unblocks the long-standing
``[^1]`` footnote caveat (auth-dep 4xx still on legacy shape)
that has been carried in ``acn-error-schema.md`` since the pilot.

Coverage matrix
---------------
8 raise sites × 4 reused ErrorCodes:

* ``AUTHENTICATION_REQUIRED`` (×4) — 401 surfaces:
    - ``_resolve_agent_by_bearer`` (api key not resolvable)
    - ``verify_agent_api_key`` (Authorization not Bearer)
    - ``verify_proxy_caller`` (X-ACN-Authorization not Bearer)
    - ``verify_owner_or_internal`` (no credential at all)
  ``details.reason`` discriminates the four sub-reasons —
  ``invalid_api_key``, ``invalid_authorization_header_format``
  (shared between agent-API and proxy-caller paths because both
  surface the same SDK-actionable failure: "your Bearer token
  prefix is wrong"),
  ``owner_or_internal_credential_required``.

* ``INTERNAL_TOKEN_INVALID`` (×2) — 403 surfaces:
    - ``verify_internal_token`` (internal-only endpoint, wrong token)
    - ``verify_owner_or_internal`` (X-Internal-Token present but wrong;
      priority over Bearer fallback)
  ``details = {}`` (empty) — no diagnostic context to leak; a wrong
  internal token is a misconfiguration the operator sees in audit
  logs, not the caller's display.

* ``API_KEY_AGENT_MISMATCH`` (×1) — 403:
    - ``verify_owner_or_internal`` (Bearer key resolves to a different
      agent than the path agent_id)
  Reuses the cross-module ``{path_agent, key_agent}`` shape established
  in sprint #1 / #2b — same code, identical details schema across
  modules so SDK clients don't need branching per route.

* ``INVALID_REQUEST`` (×1) — 422:
    - ``assert_system_caller`` (``from_agent`` outside the reserved
      ``system:<slug>`` namespace on the internal-channel send)
  Reuses the cross-module ``{field, reason, value}`` envelope from
  sprint #2b. ``reason="system_namespace_required"`` is the new
  enum value; the choice of 422 (vs 400) preserves the pre-migration
  contract — the request *was* understood, it just violated the
  semantic rule ``from_agent ∈ system:*``.

How the tests drive each site
-----------------------------
Auth deps don't have endpoints — they mount onto routes. Each test
therefore picks ONE representative route per dep function and
exercises the failure path through ``TestClient`` so the central
``_acn_http_error_handler`` runs and we observe the actual flat-
schema response body (the same body SDK clients will see in
production).

Route choices:

* ``POST /api/v1/communication/send`` — drives ``AgentApiKeyDep``
  (``verify_agent_api_key`` + ``_resolve_agent_by_bearer``).
* ``POST /api/v1/agents/{agent_id}`` (registry proxy_post) — drives
  ``ProxyCallerDep`` (``verify_proxy_caller``).
* ``GET /api/v1/payments/tasks/{task_id}`` — drives
  ``InternalTokenDep`` (``verify_internal_token``); chosen over an
  analytics endpoint because payments is migrated and we get an
  ``ACN_DEFAULT_RESPONSES`` advertisement bonus on the route itself.
* ``GET /api/v1/communication/manifest/{agent_id}`` — drives
  ``OwnerOrInternalDep`` (``verify_owner_or_internal``); the manifest
  router is migrated as of sprint #8 so the response handler chain
  through the central handler matches every other migrated route.
* ``POST /api/v1/communication/internal/send`` — drives
  ``assert_system_caller`` (the function is a body-validator,
  not a Depends, so the failure is post-auth but pre-handler).

X-Request-ID echo
-----------------
We assert ``r.headers["X-Request-ID"] == body["request_id"]`` on each
test because that's the load-bearing client-side correlation guarantee
(operators triage incidents by request_id; if the header drifts from
the body, postmortems become un-correlatable).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.routes.dependencies import (
    _api_key_cache,
    _api_key_cache_by_agent,
    _cache_agent,
    evict_agent_from_cache,
    get_agent_service,
    get_message_service,
    get_payment_tasks,
    limiter,
)
from tests.routes.conftest import _assert_flat_shape

VALID_INTERNAL_TOKEN = "test-internal-token-min-32-chars-padding"


@pytest.fixture(autouse=True)
def _reset_state():
    limiter.enabled = False
    _api_key_cache.clear()
    _api_key_cache_by_agent.clear()
    yield
    limiter.enabled = True
    _api_key_cache.clear()
    _api_key_cache_by_agent.clear()
    app.dependency_overrides.clear()


@pytest.fixture
def stub_agent_service():
    """Resolve ``owner-key`` → ``agent-target`` and
    ``other-key`` → ``agent-other`` via ``get_agent_by_api_key``.
    Unknown keys resolve to ``None`` so we can exercise the
    ``invalid_api_key`` path."""
    svc = AsyncMock()

    target = MagicMock()
    target.agent_id = "agent-target"
    target.name = "Target"
    target.wallet_address = None

    other = MagicMock()
    other.agent_id = "agent-other"
    other.name = "Other"
    other.wallet_address = None

    async def _by_api_key(key: str):
        if key == "owner-key":
            return target
        if key == "other-key":
            return other
        return None

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)
    return svc


# ============================================================================
# AUTHENTICATION_REQUIRED ×4
# ============================================================================


class TestAuthenticationRequiredFlatShape:
    """All four 401 sites in the dep chain emit the flat schema with
    ``error_code == "authentication_required"`` and a ``reason``
    field discriminating which sub-failure it was."""

    def test_invalid_api_key_via_send(self, stub_agent_service):
        """``_resolve_agent_by_bearer`` returns None for an unknown
        API key → 401 ``authentication_required`` with
        ``reason == "invalid_api_key"``. Driven through
        ``POST /communication/send`` which mounts ``AgentApiKeyDep``."""
        app.dependency_overrides[get_agent_service] = lambda: stub_agent_service
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/communication/send",
                headers={"Authorization": "Bearer not-a-real-key"},
                json={
                    "from_agent": "x",
                    "target_agent": "y",
                    "message": {"role": "user", "parts": []},
                },
            )
        assert r.status_code == 401, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "authentication_required"
        assert body["details"] == {"reason": "invalid_api_key"}
        assert r.headers.get("X-Request-ID") == body["request_id"]

    def test_authorization_not_bearer_via_send(self, stub_agent_service):
        """``verify_agent_api_key`` rejects a non-Bearer Authorization
        header → 401 ``authentication_required`` with
        ``reason == "invalid_authorization_header_format"``."""
        app.dependency_overrides[get_agent_service] = lambda: stub_agent_service
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/communication/send",
                headers={"Authorization": "Basic notbearer"},
                json={
                    "from_agent": "x",
                    "target_agent": "y",
                    "message": {"role": "user", "parts": []},
                },
            )
        assert r.status_code == 401, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "authentication_required"
        assert body["details"] == {"reason": "invalid_authorization_header_format"}
        assert r.headers.get("X-Request-ID") == body["request_id"]

    def test_x_acn_authorization_not_bearer_via_proxy(self, stub_agent_service):
        """``verify_proxy_caller`` rejects a non-Bearer
        X-ACN-Authorization → 401 with the SAME reason as the
        agent-key path. Reusing the reason value across both paths
        is intentional: the caller-actionable failure is identical
        (your Bearer prefix is wrong); discriminating on the header
        name would force SDK clients to maintain a switch over which
        endpoint they hit, which is operationally noisy and adds no
        diagnostic value."""
        app.dependency_overrides[get_agent_service] = lambda: stub_agent_service
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target",
                headers={"X-ACN-Authorization": "Basic notbearer"},
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            )
        assert r.status_code == 401, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "authentication_required"
        assert body["details"] == {"reason": "invalid_authorization_header_format"}
        assert r.headers.get("X-Request-ID") == body["request_id"]

    def test_owner_or_internal_no_credential_via_manifest(self, stub_agent_service):
        """``verify_owner_or_internal`` with NO Authorization and NO
        X-Internal-Token → 401 with
        ``reason == "owner_or_internal_credential_required"``.
        Driven through ``GET /communication/manifest/{agent_id}``."""
        app.dependency_overrides[get_agent_service] = lambda: stub_agent_service
        with TestClient(app) as client:
            r = client.get("/api/v1/communication/manifest/agent-target")
        assert r.status_code == 401, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "authentication_required"
        assert body["details"] == {
            "reason": "owner_or_internal_credential_required"
        }
        assert r.headers.get("X-Request-ID") == body["request_id"]


# ============================================================================
# INTERNAL_TOKEN_INVALID ×2
# ============================================================================


class TestInternalTokenInvalidFlatShape:
    """Both 403 ``internal_token_invalid`` sites — one via the
    pure ``verify_internal_token`` dep, one via the priority-branch
    in ``verify_owner_or_internal`` — emit the flat schema with
    empty ``details``. We deliberately do NOT include the wrong
    token in details (would let a misconfigured ops tool log
    secrets to a less-trusted destination)."""

    def test_pure_internal_dep_wrong_token_via_payment_task(self):
        """``verify_internal_token`` rejects a wrong X-Internal-Token →
        403 ``internal_token_invalid``. Driven through
        ``GET /payments/tasks/{task_id}``."""
        stub_payment_tasks = AsyncMock()
        app.dependency_overrides[get_payment_tasks] = lambda: stub_payment_tasks
        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.get(
                    "/api/v1/payments/tasks/some-task-id",
                    headers={"X-Internal-Token": "wrong-token-padding-min-32-chars"},
                )
        assert r.status_code == 403, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "internal_token_invalid"
        assert body["details"] == {}
        assert r.headers.get("X-Request-ID") == body["request_id"]

    def test_owner_or_internal_priority_wrong_token_via_manifest(
        self, stub_agent_service
    ):
        """``verify_owner_or_internal`` checks X-Internal-Token FIRST;
        a wrong token must fail closed (NOT fall through to Bearer
        auth). The priority order is security-critical: a half-correct
        internal token is much more likely a misconfigured ops tool
        than an attacker who *also* has a valid owner API key, and
        conflating the two would mask the misconfig in audit logs."""
        app.dependency_overrides[get_agent_service] = lambda: stub_agent_service
        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.get(
                    "/api/v1/communication/manifest/agent-target",
                    headers={
                        "X-Internal-Token": "wrong-token-padding-min-32-chars",
                        "Authorization": "Bearer owner-key",
                    },
                )
        assert r.status_code == 403, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "internal_token_invalid"
        assert body["details"] == {}


# ============================================================================
# API_KEY_AGENT_MISMATCH ×1
# ============================================================================


class TestApiKeyAgentMismatchFlatShape:
    """The single 403 ``api_key_agent_mismatch`` site in
    ``verify_owner_or_internal`` reuses the cross-module shape
    established in sprint #1 (``{path_agent, key_agent}``). This
    means the SAME error_code now surfaces from at least 4
    different module call paths (allowlist, registry, follows,
    manifest, communication, payments, AND now the auth-dep
    layer itself) with a single canonical details schema —
    exactly the SDK-side simplification the cross-module RFC
    promised."""

    def test_owner_key_for_different_agent_via_manifest(self, stub_agent_service):
        """``verify_owner_or_internal``: ``other-key`` resolves to
        ``agent-other`` but the path is ``agent-target`` → 403
        ``api_key_agent_mismatch`` with
        ``details = {path_agent, key_agent}``."""
        app.dependency_overrides[get_agent_service] = lambda: stub_agent_service
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/communication/manifest/agent-target",
                headers={"Authorization": "Bearer other-key"},
            )
        assert r.status_code == 403, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "api_key_agent_mismatch"
        assert body["details"] == {
            "path_agent": "agent-target",
            "key_agent": "agent-other",
        }
        assert r.headers.get("X-Request-ID") == body["request_id"]


# ============================================================================
# INVALID_REQUEST ×1
# ============================================================================


class TestInvalidRequestSystemNamespaceFlatShape:
    """``assert_system_caller`` rejects ``from_agent`` outside the
    reserved ``system:<slug>`` namespace with a 422
    ``invalid_request`` carrying ``reason="system_namespace_required"``.
    422 (vs 400) preserves the pre-migration contract — the request
    *was* understood, it just violated a semantic rule.

    Why we include the offending value in ``details``: the
    validator's ``value`` field is caller-supplied so the caller
    already has it; echoing it back closes the diagnostic loop
    (without the echo, an SDK that mangles ``from_agent`` between
    its own pydantic layer and the wire would have to log+correlate
    request bodies to debug). This mirrors the pattern used by the
    tasks ``list_tasks`` invalid-status site — see
    ``acn-error-schema.md`` §2 cross-module subsection."""

    def test_non_system_from_agent_via_internal_send(self):
        stub_message = AsyncMock()
        stub_message.send_message = AsyncMock(
            return_value={"message_id": "m-1", "status": "sent"}
        )
        app.dependency_overrides[get_message_service] = lambda: stub_message
        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/internal/send",
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                    json={
                        "from_agent": "not-a-system-caller",
                        "target_agent": "agent-x",
                        "message": {"role": "user", "parts": []},
                    },
                )
        assert r.status_code == 422, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "invalid_request"
        assert body["details"] == {
            "field": "from_agent",
            "reason": "system_namespace_required",
            "value": "not-a-system-caller",
        }
        assert r.headers.get("X-Request-ID") == body["request_id"]
        stub_message.send_message.assert_not_called()


# ============================================================================
# M3: evict_agent_from_cache — immediate revocation (security)
# ============================================================================


class TestEvictAgentFromCache:
    """``evict_agent_from_cache`` must atomically remove both the primary
    ``_api_key_cache`` entry AND the reverse index entry so that a revoked
    agent's credentials cannot be replayed for up to the remaining TTL.
    """

    def test_evict_removes_primary_and_reverse_index(self):
        _cache_agent("raw-key-abc", "agent-123", "Alice", wallet_address=None)
        assert "agent-123" in _api_key_cache_by_agent
        assert len(_api_key_cache) == 1

        evict_agent_from_cache("agent-123")

        assert "agent-123" not in _api_key_cache_by_agent
        assert len(_api_key_cache) == 0

    def test_evict_unknown_agent_is_noop(self):
        evict_agent_from_cache("nonexistent-agent")
        assert len(_api_key_cache) == 0
        assert len(_api_key_cache_by_agent) == 0

    def test_evict_only_targets_named_agent(self):
        _cache_agent("key-a", "agent-a", "AgentA")
        _cache_agent("key-b", "agent-b", "AgentB")
        assert len(_api_key_cache) == 2

        evict_agent_from_cache("agent-a")

        assert "agent-a" not in _api_key_cache_by_agent
        assert "agent-b" in _api_key_cache_by_agent
        assert len(_api_key_cache) == 1

    def test_cache_agent_updates_reverse_index(self):
        _cache_agent("key-1", "agent-x", "X")
        assert _api_key_cache_by_agent.get("agent-x") is not None

    def test_key_rotation_removes_old_entry(self):
        """Re-caching the same agent_id with a new key must drop the old entry."""
        _cache_agent("old-key", "agent-rotate", "Rot")
        assert len(_api_key_cache) == 1

        _cache_agent("new-key", "agent-rotate", "Rot")

        # Only the new entry should survive
        assert len(_api_key_cache) == 1
        assert _api_key_cache_by_agent.get("agent-rotate") is not None
