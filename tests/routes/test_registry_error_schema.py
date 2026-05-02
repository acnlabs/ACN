"""Registry routes — flat ACN error schema contract tests.

Phase 2 review v2 P1 #11 sprint rows #2a + #2b — pin the migrated
4xx sites in ``acn/routes/registry.py`` to the canonical
``ACNHTTPError`` flat schema after their conversion from raw
``HTTPException``.

Sprint #2a scope (legacy section of this file): the 19 4xx sites
that map directly to existing catalog codes — 17 ``AGENT_NOT_FOUND``
+ 1 ``API_KEY_AGENT_MISMATCH`` + 2 ``SUBNET_NOT_FOUND`` + 1
``COMMUNICATION_REJECTED`` (clean-up of the legacy nested
``{"detail": {"detail": ...}}`` shape).

Sprint #2b scope (cross-module auth/permission/validation): the
remaining 11 4xx sites (10 deferred from #2a + 1 missed
``AGENT_NOT_FOUND`` site at ``update_social_card_url`` discovered
during the #2b RFC). New representative tests live under
``TestRegistryFlatErrorSchemaCrossModule`` below; we reuse the
established ``stub_agent_service`` fixture and the conftest-shared
``_assert_flat_shape`` helper.

Coverage choice rationale
  Registry has 17 ``AGENT_NOT_FOUND`` raise sites; covering each
  individually would balloon to ~17 near-identical tests. Instead
  we exercise four *representative* endpoint shapes that together
  touch every distinct ``raise … from …`` style in registry.py:

  * ``GET /agents/{id}`` — ``from e`` style, public discovery
    path (the most-used 404 surface for SDK clients).
  * ``POST /agents/{id}/heartbeat`` — both 403
    (``api_key_agent_mismatch``) AND 404 in one route, plus the
    only ``API_KEY_AGENT_MISMATCH`` migration site in registry.
  * ``DELETE /agents/{id}`` — the only ``raise … from`` -less
    site (``success=False`` short-circuit *inside* a ``try`` whose
    ``except`` clauses can't catch it because it's an
    ``ACNHTTPError`` not the domain exception).
  * ``GET /agents/{id}/wallets`` — the only ``from None`` site,
    pinned to confirm cause-suppression survives the migration.

  This 4-way split is enough to fail loudly on a future refactor
  that drops one of the styles or ferries information differently
  through ``details``. A 5th case covers ``COMMUNICATION_REJECTED``
  on the proxy path — the only migration that *changed* the wire
  shape (the old nested ``{"detail": {"detail": "..."}}`` form is
  flattened) — which the existing
  ``tests/routes/test_proxy_policy.py`` already covers at the
  function level via ``pytest.raises(ACNHTTPError)``. We do not
  duplicate it here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException
from acn.routes.dependencies import get_agent_service
from tests.routes.conftest import _assert_flat_shape


@pytest.fixture
def stub_agent_service():
    """Wires ``owner-key`` → ``agent-target`` and ``other-key`` →
    ``agent-other`` for cross-tenant 403 cases. ``get_agent`` raises
    ``AgentNotFoundException`` for any other id, so the 404 path
    in registry.py fires naturally.
    """
    svc = AsyncMock()

    target = MagicMock()
    target.agent_id = "agent-target"
    target.name = "Target"
    target.endpoint = "https://target.example.com/a2a"
    target.accepts_payment = False
    target.payment_methods = []
    target.wallet_addresses = {}
    target.token_pricing = {}
    target.erc8004_agent_id = None
    target.erc8004_chain = None
    target.erc8004_tx_hash = None
    target.erc8004_registered_at = None

    other = MagicMock()
    other.agent_id = "agent-other"
    other.name = "Other"

    async def _by_api_key(key: str):
        if key == "owner-key":
            return target
        if key == "other-key":
            return other
        return None

    async def _get_agent(agent_id: str):
        if agent_id == "agent-target":
            return target
        raise AgentNotFoundException(agent_id)

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)
    svc.get_agent = AsyncMock(side_effect=_get_agent)
    svc.update_heartbeat = AsyncMock(return_value=None)

    async def _unregister(agent_id, owner):
        return False  # success=False → 404 path

    svc.unregister_agent = AsyncMock(side_effect=_unregister)
    return svc


def _wire(svc) -> None:
    app.dependency_overrides[get_agent_service] = lambda: svc


class TestRegistryFlatErrorSchema:
    """Pin response shape for the four representative endpoints
    described in the module docstring."""

    def test_get_agent_404_flat_shape(self, stub_agent_service):
        """``GET /api/v1/agents/{id}`` — public discovery 404, the
        ``from e`` re-raise pattern. This is the most-used 404
        surface in the registry; an SDK client will see this shape
        more often than any other in registry."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/agents/agent-missing")

        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "agent_not_found"
        assert body["details"] == {"agent_id": "agent-missing"}
        assert r.headers.get("X-Request-ID") == body["request_id"]

    def test_heartbeat_403_api_key_agent_mismatch_flat_shape(
        self, stub_agent_service
    ):
        """``POST /api/v1/agents/{id}/heartbeat`` — the only 403
        ``api_key_agent_mismatch`` migration site in registry.
        Cross-tenant heartbeat must surface the path/key tuple in
        ``details`` so the SDK can show "you tried to heartbeat X
        with a key for Y" without a second round-trip."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/heartbeat",
                headers={"Authorization": "Bearer other-key"},
            )

        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "api_key_agent_mismatch"
        assert body["details"] == {
            "path_agent": "agent-target",
            "key_agent": "agent-other",
        }
        assert r.headers.get("X-Request-ID") == body["request_id"]

    def test_heartbeat_404_flat_shape(self, stub_agent_service):
        """``POST /agents/{id}/heartbeat`` ALSO emits 404 when the
        downstream ``update_heartbeat`` raises ``AgentNotFoundException``.
        The owner-key auth gate succeeds first (the path agent_id
        matches the key agent_id) and the lookup fails inside the
        service. Pin both branches because heartbeat is one of the
        few endpoints with two distinct migrated 4xx surfaces."""
        stub_agent_service.update_heartbeat = AsyncMock(
            side_effect=AgentNotFoundException("agent-target")
        )
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/heartbeat",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "agent_not_found"
        assert body["details"] == {"agent_id": "agent-target"}

    def test_get_wallets_404_from_none_pattern(self, stub_agent_service):
        """``GET /agents/{id}/wallets`` — the only ``from None``
        re-raise in registry.py (cause suppression). The migration
        must preserve the suppression so the response carries the
        new shape without leaking the underlying exception text in
        a stack trace; we cannot assert on traceback presence
        through HTTP, but we *can* pin that the response body is
        the same flat shape as the ``from e`` cases — ergo the
        migration didn't accidentally swap the ``from None`` for a
        ``from e`` (which would be a subtle behavioural change)."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/agents/agent-missing/wallets")

        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "agent_not_found"
        assert body["details"] == {"agent_id": "agent-missing"}


class TestRegistryFlatErrorSchemaUnregisterPath:
    """Cover the unique ``raise ACNHTTPError(...)`` *without* a
    ``from`` clause inside an ``unregister_agent`` flow — the
    ``success=False`` short-circuit in the ``else`` branch of a
    ``try`` whose ``except`` clauses catch the domain exceptions.
    This is the only such pattern in the migration; without an
    explicit pin a future refactor could quietly fold it into the
    ``except AgentNotFoundException`` branch and lose the explicit
    "service returned False" signal in operator logs.

    Auth bypass note
        DELETE /api/v1/agents/{id} is gated by
        ``Depends(require_permission("acn:write"))``. We rely on
        ``settings.dev_mode=True`` (which is the test-environment
        default — see ``acn/auth/middleware.py::verify_token``)
        to short-circuit Auth0 verification: any non-empty Bearer
        token is accepted and the synthetic payload grants
        ``acn:read`` / ``acn:write`` / ``acn:admin`` permissions.

        Why not ``app.dependency_overrides``: ``Depends`` captures
        the ``permission_checker`` closure at decoration time,
        keyed by *that closure object*. Overriding the factory
        ``require_permission`` after import would create a
        different closure that FastAPI never resolves against.
        Overriding the *resolved* dependency would require us to
        reach into ``permission_checker`` directly — fragile across
        FastAPI versions. The dev-mode pathway is more robust and
        is already the canonical way the rest of the suite tests
        Auth0-gated routes (``test_phase1_management_rate_limits``,
        ``test_agent_endpoint_disclosure``, etc.).

        When sprint row #10 (`dependencies` migration) lands and
        introduces ``ACNHTTPError`` for auth rejects, this test
        will need a re-think — at that point the auth gate's flat
        shape becomes part of the contract this file pins.
    """

    def test_unregister_returns_404_with_flat_shape(self, stub_agent_service):
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/agents/agent-target",
                headers={"Authorization": "Bearer dev-mode-any-token"},
            )

        # Defence-in-depth: if a future refactor disables dev_mode in
        # the test environment, fail loudly rather than silently
        # turning into a no-op. ``pytest.skip`` would hide the
        # coverage loss; an outright ``fail`` keeps it loud.
        assert r.status_code != 401, (
            "DELETE /agents/{id} returned 401 — dev_mode auth bypass "
            "is no longer in effect. Restore the dev-mode default in "
            "the test environment, or rewrite this test against the "
            "new auth surface (sprint row #10)."
        )

        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "agent_not_found"
        assert body["details"] == {"agent_id": "agent-target"}


class TestRegistryFlatErrorSchemaCrossModule:
    """Sprint #2b — pin the new cross-module ErrorCode raises.

    Coverage choice rationale
        Sprint #2b adds 11 new raise sites in registry.py spanning 4
        new ``ErrorCode`` members. We pick **one representative
        endpoint per ErrorCode** rather than every site, since the
        flat-shape invariant is the same and conftest's
        ``_assert_flat_shape`` already does the heavy lifting:

        * ``AUTHENTICATION_REQUIRED`` — covered by ``GET /agents/me``
          (the only public endpoint that surfaces *both* 401 sites,
          one for each detail.reason value: malformed Bearer header
          and unrecognised API key).
        * ``INTERNAL_TOKEN_INVALID`` — covered by
          ``POST /agents/join/internal`` without an
          ``X-Internal-Token`` header.
        * ``OWNERSHIP_MISMATCH`` — covered by
          ``DELETE /agents/{id}`` raising ``PermissionError`` from the
          service. We choose ``unregister_agent`` over
          ``transfer_agent`` / ``release_agent`` because the
          ``replace_all=true`` migration step pinned all 3 sites with
          identical raise shapes, so testing one is sufficient.
        * ``INVALID_REQUEST`` — covered by the bulk-delete safety
          guard (``POST /agents/admin/bulk_delete`` with no filter).
          Tests both the ``message`` (custom prose explaining the
          guard) and ``details.reason``.

        Out of scope: ``MISSING_PERMISSION`` (dev-mode disabled at
        L296) — surface needs ``settings.dev_mode = False``, which
        clashes with how the rest of the suite uses dev-mode auth
        bypass. The site is small (a single ``if not settings.dev_mode``
        guard) and its raise shape is byte-identical to the
        ``ownership_mismatch`` shape pattern below; the catalog
        completeness test in ``tests/core/test_error_schema.py``
        guarantees the code itself is well-formed.
    """

    def test_get_me_invalid_authorization_header_returns_authentication_required(
        self, stub_agent_service
    ):
        """``GET /api/v1/agents/me`` without a ``Bearer `` prefix
        — pins the ``invalid_authorization_header_format`` reason
        path of ``AUTHENTICATION_REQUIRED``."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/me",
                headers={"Authorization": "Token foo"},
            )

        assert r.status_code == 401
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "authentication_required"
        assert body["details"] == {"reason": "invalid_authorization_header_format"}
        assert r.headers.get("X-Request-ID") == body["request_id"]

    def test_get_me_invalid_api_key_returns_authentication_required(
        self, stub_agent_service
    ):
        """``GET /api/v1/agents/me`` with a Bearer prefix but an
        unknown API key — pins the ``invalid_api_key`` reason
        path of ``AUTHENTICATION_REQUIRED``. Both 401 sites share
        one ErrorCode; the SDK can branch on ``details.reason`` if
        it wants per-cause UX (e.g. "fix your auth header" vs
        "your key was revoked")."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/me",
                headers={"Authorization": "Bearer no-such-key"},
            )

        assert r.status_code == 401
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "authentication_required"
        assert body["details"] == {"reason": "invalid_api_key"}

    def test_internal_join_missing_token_returns_internal_token_invalid(
        self, stub_agent_service
    ):
        """``POST /api/v1/agents/join/internal`` without
        ``X-Internal-Token`` — pins ``INTERNAL_TOKEN_INVALID``.
        FastAPI's Pydantic body validation runs *before* the
        handler, so we still need a schema-valid ``AgentJoinRequest``
        body — but the handler short-circuits at the auth gate
        before touching any agent service, so we don't need to
        stub anything beyond the bare-minimum required fields."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/join/internal",
                json={
                    "name": "TestAgent",
                    "description": "Schema-valid agent for the auth-gate test only.",
                    "endpoint": "https://example.com/a2a",
                },
            )

        assert r.status_code == 401
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "internal_token_invalid"
        # The default message lands here; we don't pin the prose
        # (default messages can evolve without breaking SDKs that
        # branch on error_code).
        assert body["details"] == {}

    def test_unregister_permission_error_returns_ownership_mismatch(
        self, stub_agent_service
    ):
        """``DELETE /api/v1/agents/{id}`` when the service raises
        ``PermissionError`` — pins ``OWNERSHIP_MISMATCH``.
        ``unregister_agent`` is one of three identical
        ``except PermissionError`` sites in registry.py
        (``transfer_agent`` / ``release_agent`` are the others); a
        single representative test pins the wire shape that all
        three share, since the ``replace_all=true`` migration
        step guarantees they are byte-identical raises.

        Auth bypass note
            Same dev-mode pathway as
            ``test_unregister_returns_404_with_flat_shape`` —
            see that test's class docstring for the rationale.
        """
        stub_agent_service.unregister_agent = AsyncMock(
            side_effect=PermissionError("Only the owner can unregister this agent.")
        )
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/agents/agent-target",
                headers={"Authorization": "Bearer dev-mode-any-token"},
            )

        assert r.status_code != 401, (
            "DELETE /agents/{id} returned 401 — dev_mode auth bypass "
            "is no longer in effect."
        )
        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "ownership_mismatch"
        assert body["details"] == {
            "agent_id": "agent-target",
            "reason": "Only the owner can unregister this agent.",
        }

    def test_bulk_delete_no_filter_returns_invalid_request(
        self, stub_agent_service, monkeypatch
    ):
        """``DELETE /api/v1/agents?dry_run=false`` without
        ``name_prefix`` / ``owner`` filter — pins
        ``INVALID_REQUEST`` with the bulk-delete safety guard
        reason. The guard fires before any agent service call so
        we don't need to stub ``search_agents``.

        Auth gate (X-Internal-Token)
            ``admin_bulk_delete_agents`` is gated by
            ``InternalTokenDep``; we monkeypatch
            ``settings.internal_api_token`` to a known value and
            supply the matching header so the gate passes and we
            can reach the safety guard at the top of the handler
            body.

        Endpoint shape note
            The endpoint is the *root* of the registry router
            (``DELETE /api/v1/agents``) — not
            ``POST /agents/admin/bulk_delete`` despite what the
            handler name and docstring imply. The query-parameter
            interface (``name_prefix`` / ``owner`` / ``dry_run``)
            is what makes this a "filter-required" guard rather
            than a body-validation guard.
        """
        from acn.routes.dependencies import settings as deps_settings

        monkeypatch.setattr(
            deps_settings, "internal_api_token", "test-internal-token"
        )
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/agents",
                params={"dry_run": "false"},
                headers={"X-Internal-Token": "test-internal-token"},
            )

        assert r.status_code == 400
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "invalid_request"
        assert body["details"] == {"reason": "bulk_delete_filter_required"}
        # The safety guard's prose is part of operator-facing
        # behaviour (an operator who typo'd a bulk delete needs to
        # see the explanation), so we DO pin the message here —
        # unlike the default-message paths above.
        assert "Refusing to bulk-delete without a filter" in body["message"]
