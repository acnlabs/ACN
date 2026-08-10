"""Tests for ``/api/v1/agents/{id}/allowlist/...`` (Phase 2 PR #2).

Pins the route-level contract:

* Owner-only auth on POST / DELETE / GET (cross-tenant returns 403).
* 200 + ``changed=true`` on first POST; 200 + ``changed=false`` on
  re-POST (idempotent).
* 200 + ``changed=true/false`` on DELETE depending on prior state.
* 404 when ``target_id`` doesn't exist.
* 400 when owner tries to allowlist itself.
* 429 when capacity exceeded.
* GET returns the canonical owner-only listing.

Mirrors the manifest routes test layout: AllowlistService is stubbed
so this suite focuses on the route → service seam.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException
from acn.core.interfaces import AllowlistEntry
from acn.routes.dependencies import (
    _api_key_cache,
    get_agent_service,
    get_allowlist_service,
    limiter,
)
from acn.services import (
    AllowlistCapacityExceededError,
    SelfAllowlistError,
)

VALID_INTERNAL_TOKEN = "test-internal-token-for-allowlist"


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
    """Wires ``owner-key`` → ``agent-target`` and ``other-key`` →
    ``agent-other`` so we can verify cross-tenant 403."""
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


@pytest.fixture
def stub_allowlist_service():
    svc = AsyncMock()
    # Sensible defaults — individual tests override.
    svc.add = AsyncMock(return_value=True)
    svc.remove = AsyncMock(return_value=True)
    svc.list_targets = AsyncMock(return_value=[])
    svc.count = AsyncMock(return_value=0)
    return svc


def _wire(allowlist_svc, agent_svc) -> None:
    app.dependency_overrides[get_allowlist_service] = lambda: allowlist_svc
    app.dependency_overrides[get_agent_service] = lambda: agent_svc


# ---------------------------------------------------------------------------
# POST /allowlist/{target_id}
# ---------------------------------------------------------------------------


class TestAddAllowlist:
    def test_owner_can_add_target(self, stub_allowlist_service, stub_agent_service):
        _wire(stub_allowlist_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/allowlist/alice",
                headers={"Authorization": "Bearer owner-key"},
                json={"reason": "trusted partner"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {
            "owner_id": "agent-target",
            "target_id": "alice",
            "allowlisted": True,
            "changed": True,
        }
        stub_allowlist_service.add.assert_awaited_once()
        kwargs = stub_allowlist_service.add.await_args.kwargs
        assert kwargs["owner_id"] == "agent-target"
        assert kwargs["target_id"] == "alice"
        assert kwargs["reason"] == "trusted partner"

    def test_repeat_add_is_idempotent(
        self, stub_allowlist_service, stub_agent_service
    ):
        """Re-adding existing target → 200 with ``changed=false``,
        not 409. Lets clients write retry-safe code without first
        checking state."""
        stub_allowlist_service.add = AsyncMock(return_value=False)
        _wire(stub_allowlist_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/allowlist/alice",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200
        assert r.json()["changed"] is False
        assert r.json()["allowlisted"] is True

    def test_post_without_body_works(
        self, stub_allowlist_service, stub_agent_service
    ):
        """Reason is optional — naked POST must succeed."""
        _wire(stub_allowlist_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/allowlist/alice",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200
        kwargs = stub_allowlist_service.add.await_args.kwargs
        assert kwargs["reason"] is None

    def test_other_agent_cannot_modify_target_allowlist(
        self, stub_allowlist_service, stub_agent_service
    ):
        """Cross-tenant write attempt must 403 — the recipient's
        allowlist is private and the only writer is the recipient."""
        _wire(stub_allowlist_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/allowlist/alice",
                headers={"Authorization": "Bearer other-key"},
            )

        assert r.status_code == 403
        stub_allowlist_service.add.assert_not_awaited()

    def test_anonymous_returns_401(self, stub_allowlist_service, stub_agent_service):
        _wire(stub_allowlist_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/allowlist/alice",
                headers={"Authorization": "Bearer not-a-real-key"},
            )

        assert r.status_code == 401
        stub_allowlist_service.add.assert_not_awaited()

    def test_self_allowlist_returns_400(
        self, stub_allowlist_service, stub_agent_service
    ):
        stub_allowlist_service.add = AsyncMock(
            side_effect=SelfAllowlistError("self")
        )
        _wire(stub_allowlist_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/allowlist/agent-target",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 400

    def test_unknown_target_returns_404(
        self, stub_allowlist_service, stub_agent_service
    ):
        stub_allowlist_service.add = AsyncMock(
            side_effect=AgentNotFoundException("Agent not found")
        )
        _wire(stub_allowlist_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/allowlist/ghost",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 404

    def test_capacity_exceeded_returns_429(
        self, stub_allowlist_service, stub_agent_service
    ):
        """Capacity limit must surface as 429 (matches the pattern
        FollowService established for ``FollowLimitExceededError``)."""
        stub_allowlist_service.add = AsyncMock(
            side_effect=AllowlistCapacityExceededError("capacity reached")
        )
        _wire(stub_allowlist_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/allowlist/alice",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 429

    def test_long_reason_rejected_by_pydantic(
        self, stub_allowlist_service, stub_agent_service
    ):
        """Body validation: > 200 char reason returns 422 (Pydantic),
        even before reaching the service layer."""
        _wire(stub_allowlist_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/allowlist/alice",
                headers={"Authorization": "Bearer owner-key"},
                json={"reason": "x" * 500},
            )

        assert r.status_code == 422
        stub_allowlist_service.add.assert_not_awaited()


# ---------------------------------------------------------------------------
# DELETE /allowlist/{target_id}
# ---------------------------------------------------------------------------


class TestRemoveAllowlist:
    def test_owner_can_remove_target(
        self, stub_allowlist_service, stub_agent_service
    ):
        _wire(stub_allowlist_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/agents/agent-target/allowlist/alice",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200
        body = r.json()
        assert body == {
            "owner_id": "agent-target",
            "target_id": "alice",
            "allowlisted": False,
            "changed": True,
        }

    def test_idempotent_repeat_delete_returns_200_changed_false(
        self, stub_allowlist_service, stub_agent_service
    ):
        stub_allowlist_service.remove = AsyncMock(return_value=False)
        _wire(stub_allowlist_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/agents/agent-target/allowlist/alice",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200
        assert r.json()["changed"] is False

    def test_other_agent_cannot_remove(
        self, stub_allowlist_service, stub_agent_service
    ):
        _wire(stub_allowlist_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/agents/agent-target/allowlist/alice",
                headers={"Authorization": "Bearer other-key"},
            )

        assert r.status_code == 403
        stub_allowlist_service.remove.assert_not_awaited()


# ---------------------------------------------------------------------------
# GET /allowlist (owner-only listing)
# ---------------------------------------------------------------------------


class TestListAllowlist:
    def test_owner_can_list_own(
        self, stub_allowlist_service, stub_agent_service
    ):
        stub_allowlist_service.list_targets = AsyncMock(
            return_value=[
                AllowlistEntry(
                    target_id="alice",
                    created_at=datetime(2026, 4, 30, tzinfo=UTC),
                    reason="trusted",
                ),
                AllowlistEntry(
                    target_id="bob",
                    created_at=datetime(2026, 4, 29, tzinfo=UTC),
                    reason=None,
                ),
            ]
        )
        stub_allowlist_service.count = AsyncMock(return_value=2)
        _wire(stub_allowlist_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/agent-target/allowlist",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200
        body = r.json()
        assert body["owner_id"] == "agent-target"
        assert body["total"] == 2
        assert len(body["entries"]) == 2
        assert body["entries"][0] == {
            "target_id": "alice",
            "created_at": "2026-04-30T00:00:00+00:00",
            "reason": "trusted",
        }
        assert body["entries"][1]["reason"] is None

    def test_other_agent_cannot_list(
        self, stub_allowlist_service, stub_agent_service
    ):
        """Privacy gate: allowlist content is owner-only — leaks
        relationship signals if other agents could read it."""
        _wire(stub_allowlist_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/agent-target/allowlist",
                headers={"Authorization": "Bearer other-key"},
            )

        assert r.status_code == 403

    def test_pagination_passes_through(
        self, stub_allowlist_service, stub_agent_service
    ):
        _wire(stub_allowlist_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/agent-target/allowlist?limit=10&offset=5",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200
        kwargs = stub_allowlist_service.list_targets.await_args.kwargs
        assert kwargs["limit"] == 10
        assert kwargs["offset"] == 5

    def test_internal_token_can_list(
        self, stub_allowlist_service, stub_agent_service
    ):
        """Chat Gateway proxies with X-Internal-Token after human owner assert."""
        stub_allowlist_service.list_targets = AsyncMock(return_value=[])
        stub_allowlist_service.count = AsyncMock(return_value=0)
        _wire(stub_allowlist_service, stub_agent_service)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.get(
                    "/api/v1/agents/agent-target/allowlist",
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        assert r.status_code == 200, r.text
        stub_allowlist_service.list_targets.assert_awaited_once()


# ---------------------------------------------------------------------------
# Service-disabled path — PR #2 v3 review P1-A3
# ---------------------------------------------------------------------------
#
# When PostgreSQL is missing, ``api.py`` lifespan logs
# ``allowlist_service_disabled`` and leaves the global ``None``. The
# dependency must surface that as HTTP 503 + Retry-After (not the
# previous 500 from ``RuntimeError``) so clients distinguish "feature
# not configured" from "transient server crash".


class TestAllowlistServiceDisabled:
    """503 surface when the service was never wired into the lifespan.

    Note on response shape: the global ``_http_exception_handler``
    (acn/api.py) intentionally scrubs 5xx response bodies to a
    generic ``internal_server_error`` envelope so internal config
    state isn't leaked over the wire. The detailed
    "AllowlistService is unavailable" message lives in the structured
    log emitted alongside (operators see it via observability stack,
    clients don't). This is the same pattern every 5xx in this app
    follows; here we just verify the status code + ``Retry-After``
    header indicate "this is a known disabled state, not a crash".
    """

    def test_post_returns_503_with_retry_after(self, stub_agent_service):
        from acn.routes import dependencies as deps

        # Force the wiring back to the "disabled" state used during a
        # PG-less startup. The autouse ``_reset_state`` fixture clears
        # dependency_overrides, but the module-global ``_allowlist_service``
        # must also be None for ``get_allowlist_service`` to take the 503
        # branch instead of returning a leftover instance.
        prior = deps._allowlist_service
        deps._allowlist_service = None

        # Don't override get_allowlist_service — we want the real
        # dependency to raise.
        app.dependency_overrides[get_agent_service] = lambda: stub_agent_service

        try:
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/agents/agent-target/allowlist/agent-other",
                    headers={"Authorization": "Bearer owner-key"},
                )
        finally:
            deps._allowlist_service = prior

        # Status 503 is the contract: differentiates "feature
        # configured-disabled" from "transient server crash" (500)
        # in standard nginx / Datadog / cloudwatch alerting rules.
        assert r.status_code == 503
        # Retry-After=300 (5 min) is the only header we expose
        # through the 5xx scrubber — explicit hint that this is
        # *not* a transient server fault.
        assert r.headers.get("Retry-After") == "300"
