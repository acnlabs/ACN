"""Analytics routes — flat ACN error schema contract tests.

Phase 2 review v2 P1 #11 sprint row #9 — pin the 3 4xx sites in
``list_activities`` (``acn/routes/analytics.py``) to the canonical
``ACNHTTPError`` flat schema after their migration from raw
``HTTPException``.

Why only ``list_activities``?
-----------------------------
``analytics.py`` exposes seven endpoints. Six of them
(``/agents``, ``/agents/{id}``, ``/messages``, ``/latency``,
``/subnets``, plus a public branch of ``/activities``) raise
*nothing* on the success path — auth is delegated to the
``InternalTokenDep`` dependency, which lives in
``dependencies.py`` and was migrated in sprint row #10. The
file-local 4xx sites are confined to one route:
``GET /api/v1/analytics/activities`` when called *with* an
``agent_id`` / ``agent_ids`` filter. That route has its own auth
flow (Bearer API key, scoped to the requested agent) and is the
target of this contract file.

Coverage matrix (3 raise sites)
-------------------------------
* ``AUTHENTICATION_REQUIRED`` (×2)

  * ``details.reason = auth_required_for_agent_filter`` —
    ``agent_id`` / ``agent_ids`` filter requested without a
    Bearer header at all (or with a malformed prefix).
  * ``details.reason = invalid_api_key`` — Bearer header
    present, but the key does not resolve to a known agent.
    Same reason value used in the cross-module
    ``AUTHENTICATION_REQUIRED`` catalog (see
    ``acn-error-schema.md`` §2 for the cross-sprint reason
    enum).

* ``API_KEY_AGENT_MISMATCH`` (×1) — Bearer key resolves, but
  the caller is asking for a different agent's activity (or
  one of several comma-separated agents that they don't own).
  ``details = {path_agent, key_agent}`` — the strict
  cross-sprint schema. ``path_agent`` is the *first*
  mismatched id (sorted) when ``agent_ids`` is a multi-id
  filter; we deliberately do NOT echo the entire requested
  set back, so this code stays in the strict schema bucket.

Schema-bucket invariant
-----------------------
``API_KEY_AGENT_MISMATCH`` and ``AUTHENTICATION_REQUIRED``
are *cross-module* error codes whose ``details`` field is
pinned strictly by ``tests/test_error_code_details_consistency.py``.
This file does not introduce any new keys for either code —
it only exercises the existing strict shapes from a new entry
point. Adding a new ``details`` key here would break the
consistency invariant cross-sprint.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.routes.dependencies import (
    _api_key_cache,
    get_activity_service,
    limiter,
)
from tests.routes.conftest import _assert_flat_shape


@pytest.fixture(autouse=True)
def _reset_state():
    limiter.enabled = False
    _api_key_cache.clear()
    yield
    limiter.enabled = True
    _api_key_cache.clear()
    app.dependency_overrides.clear()


@pytest.fixture
def stub_activity_service():
    """Activity service is wired but never reached on the auth-error
    paths exercised here. We still install a stub so that the
    successful-list path (not asserted by this file) remains
    fall-through-safe and we don't accidentally hit the real
    Redis-backed service in a unit test environment."""
    svc = AsyncMock()
    svc.list_activities = AsyncMock(return_value=[])
    return svc


@pytest.fixture
def stub_agent_service():
    """``owner-key`` resolves to ``agent-target``; any other key
    resolves to ``None`` so we can exercise the ``invalid_api_key``
    branch. Mirrors the dependency-injection pattern used by sprint
    rows #6 and #8."""
    svc = AsyncMock()

    target = MagicMock()
    target.agent_id = "agent-target"
    target.name = "Target"
    target.wallet_address = None

    async def _by_api_key(key: str):
        if key == "owner-key":
            return target
        return None

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)
    return svc


def _wire(monkeypatch, activity_svc, agent_svc) -> None:
    """Two distinct injection mechanisms in one helper:

    * ``ActivityService`` is consumed via FastAPI's ``Depends`` machinery
      (``ActivityServiceDep``) so the standard
      ``app.dependency_overrides`` hook works.
    * ``AgentService`` is fetched via a *module-level* call —
      ``analytics.py`` does ``agent_service = get_agent_service()`` inside
      ``list_activities`` rather than declaring it as a parameter
      dependency. ``app.dependency_overrides`` does **not** intercept
      module-level lookups, so we monkey-patch the symbol on the route
      module instead. (This is also why ``get_agent_service`` is no
      longer imported at the top of this file — we patch the
      ``acn.routes.analytics`` re-export, not the original.)
    """
    app.dependency_overrides[get_activity_service] = lambda: activity_svc
    monkeypatch.setattr(
        "acn.routes.analytics.get_agent_service",
        lambda: agent_svc,
    )


# ============================================================================
# AUTHENTICATION_REQUIRED — reason: auth_required_for_agent_filter
# ============================================================================


class TestAuthRequiredForAgentFilterFlatShape:
    """Filtering by ``agent_id`` / ``agent_ids`` without any
    ``Authorization: Bearer …`` header (or with a non-Bearer
    prefix) MUST emit the flat ACN schema with
    ``details.reason = auth_required_for_agent_filter``.

    The reason value is *new in sprint #9* and reuses the
    cross-module ``AUTHENTICATION_REQUIRED`` code rather than
    minting a new code, because the failure mode (caller has no
    valid Bearer token) is the same in semantic terms; only the
    *trigger* (filter parameter present) differs from the
    invalid-key branch."""

    def test_missing_authorization_emits_flat_schema(
        self, monkeypatch, stub_activity_service, stub_agent_service
    ):
        _wire(monkeypatch, stub_activity_service, stub_agent_service)
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/analytics/activities",
                params={"agent_id": "agent-target"},
            )
        assert r.status_code == 401, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "authentication_required"
        assert body["details"] == {
            "reason": "auth_required_for_agent_filter",
        }
        assert r.headers.get("X-Request-ID") == body["request_id"]

    def test_non_bearer_authorization_emits_flat_schema(
        self, monkeypatch, stub_activity_service, stub_agent_service
    ):
        """A header that exists but doesn't start with ``Bearer ``
        should hit the *same* branch as no header at all — both
        paths fail the ``startswith("Bearer ")`` guard. Pinning
        the shape on this branch too prevents a future refactor
        from accidentally splitting the two cases into divergent
        codes / details shapes."""
        _wire(monkeypatch, stub_activity_service, stub_agent_service)
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/analytics/activities",
                params={"agent_ids": "agent-target,agent-other"},
                headers={"Authorization": "Basic deadbeef"},
            )
        assert r.status_code == 401, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "authentication_required"
        assert body["details"] == {
            "reason": "auth_required_for_agent_filter",
        }


# ============================================================================
# AUTHENTICATION_REQUIRED — reason: invalid_api_key
# ============================================================================


class TestInvalidApiKeyFlatShape:
    """Bearer header present, syntactically valid, but the key
    does not resolve to any agent (rotated / expired / never
    issued / typo). ``details.reason = invalid_api_key`` —
    same value used by ``dependencies.py`` (sprint #10) so the
    cross-module reason vocabulary stays consistent."""

    def test_unknown_api_key_emits_flat_schema(
        self, monkeypatch, stub_activity_service, stub_agent_service
    ):
        _wire(monkeypatch, stub_activity_service, stub_agent_service)
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/analytics/activities",
                params={"agent_id": "agent-target"},
                headers={"Authorization": "Bearer not-a-real-key"},
            )
        assert r.status_code == 401, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "authentication_required"
        assert body["details"] == {"reason": "invalid_api_key"}
        assert r.headers.get("X-Request-ID") == body["request_id"]


# ============================================================================
# API_KEY_AGENT_MISMATCH — strict {path_agent, key_agent} schema
# ============================================================================


class TestApiKeyAgentMismatchFlatShape:
    """Caller has a valid key for one agent but is asking for
    activity belonging to a *different* agent. Strict
    ``{path_agent, key_agent}`` schema — same shape used by
    sprint rows #6 (follows) and #10 (dependencies).

    For ``agent_ids=`` (comma-separated) we surface only the
    *first* mismatched id (sorted) so the body stays in the
    strict schema bucket. The test asserts both the
    single-``agent_id`` and the multi-``agent_ids`` paths emit
    the same shape; if a future refactor decides to echo the
    full mismatch list, it must also flip this code into the
    ``union`` bucket in ``test_error_code_details_consistency.py``
    — which is exactly the regression this test pins against."""

    def test_single_agent_id_mismatch_emits_flat_schema(
        self, monkeypatch, stub_activity_service, stub_agent_service
    ):
        _wire(monkeypatch, stub_activity_service, stub_agent_service)
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/analytics/activities",
                params={"agent_id": "agent-other"},
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 403, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "api_key_agent_mismatch"
        assert body["details"] == {
            "path_agent": "agent-other",
            "key_agent": "agent-target",
        }
        assert r.headers.get("X-Request-ID") == body["request_id"]

    def test_multi_agent_ids_mismatch_surfaces_first_sorted(
        self, monkeypatch, stub_activity_service, stub_agent_service
    ):
        """``agent_ids = b-other, a-stranger`` — neither belongs to
        ``agent-target``. The route surfaces only the *first
        sorted* mismatched id (``a-stranger``) to keep the strict
        schema; ``b-other`` is suppressed from the body."""
        _wire(monkeypatch, stub_activity_service, stub_agent_service)
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/analytics/activities",
                params={"agent_ids": "b-other,a-stranger"},
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 403, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "api_key_agent_mismatch"
        assert body["details"] == {
            "path_agent": "a-stranger",
            "key_agent": "agent-target",
        }
