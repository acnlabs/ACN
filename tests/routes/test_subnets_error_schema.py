"""Subnets routes — flat ACN error schema contract tests.

Phase 2 review v2 P1 #11 sprint rows #3 + #3-followup — pin the
migrated 4xx sites in ``acn/routes/subnets.py`` to the canonical
``ACNHTTPError`` flat schema after their conversion from raw
``HTTPException``.

Sprint #3 scope (legacy): the 13 4xx sites that map directly to
existing catalog codes — 7 ``SUBNET_NOT_FOUND`` + 3
``AGENT_NOT_FOUND`` + 3 ``API_KEY_AGENT_MISMATCH``.

Sprint #3-followup scope (cross-module catalog from sprint #2b):
the 6 deferred 4xx sites picking up new catalog codes —
``INVALID_REQUEST`` (1× create_subnet ``ValueError``),
``AUTHENTICATION_REQUIRED`` (2× owner-filter + private-subnet auth
gates), ``OWNERSHIP_MISMATCH`` (2× cross-tenant list_subnets +
delete_subnet PermissionError), ``NOT_SUBNET_MEMBER`` (1× private
subnet member list).

Out of scope (still deferred)
  * 1 4xx site (``delete_subnet`` ``success=False`` short-circuit
    *inside* ``try``) — pre-existing latent bug silently rewritten
    to 500 by the catch-all ``except Exception``. Fixing it
    requires the ``except ACNHTTPError: raise`` cross-module
    defence P3 ticket and is intentionally not in #3-followup
    scope.
  * 8 5xx sites — kept on raw ``HTTPException`` per the
    sanitisation contract documented in ``acn.core.errors``.

Coverage choice rationale
  Subnets has 7 ``SUBNET_NOT_FOUND`` migration sites; covering each
  individually would balloon to redundant near-identical tests.
  Following the row #2a template we exercise five *representative*
  endpoint shapes that together touch every distinct ``raise … from
  …`` style and every migrated ``ErrorCode``:

  * ``GET /subnets/{subnet_id}`` — the simplest ``except … from e``
    re-raise, the public discovery 404 surface for SDK clients.
  * ``GET /subnets/{subnet_id}/agents`` — same domain exception
    re-raise but with an *early-out* ``try`` block that wraps a
    single ``await``; the rest of the handler runs at top-level.
    Pins that the migration didn't accidentally widen the ``try``.
  * ``POST /subnets/{agent_id}/subnets/{subnet_id}`` (403 path) —
    the ``if agent_info[...] != agent_id: raise`` shape (raise at
    function top-level, not in any ``try``). One of three identical
    ``API_KEY_AGENT_MISMATCH`` sites; representative coverage.
  * ``POST /subnets/{agent_id}/subnets/{subnet_id}`` (404 subnet) —
    the *first* ``try`` block of a multi-try handler raises
    ``ACNHTTPError`` from ``SubnetNotFoundException`` before the
    second ``try`` runs. Pins that mid-handler ``ACNHTTPError``
    propagates without being swallowed by a *later* catch-all.
  * ``POST /subnets/{agent_id}/subnets/{subnet_id}`` (404 agent) —
    the *last* ``try`` block reraises ``ACNHTTPError`` from
    ``AgentNotFoundException``; the surrounding ``except Exception``
    catch-all is the one that *would* swallow the new exception
    if it were placed in the ``try`` body rather than the ``except``
    branch. This is the highest-risk migration point and warrants
    an explicit shape pin.

  Together: 3 distinct ``ErrorCode`` values × 4 distinct
  raise-shape patterns. Each test also re-asserts the flat schema
  invariant via ``_assert_flat_shape`` so a future regression that
  re-introduces ``HTTPException(detail=...)`` fails loudly with a
  ``"detail" not in body`` assertion error pointing at the legacy
  field.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException, SubnetNotFoundException
from acn.routes.dependencies import (
    get_agent_service,
    get_subnet_service,
)
from tests.routes.conftest import _assert_flat_shape


@pytest.fixture
def stub_agent_service():
    """Wires ``owner-key`` → ``agent-target`` and ``other-key`` →
    ``agent-other`` for the ``api_key_agent_mismatch`` 403 test.
    ``get_agent`` raises ``AgentNotFoundException`` for any other
    id so the 404 path in subnets.py fires naturally.

    The 403 cross-tenant case requires *two* distinct agent
    identities — one whose Bearer key the test client presents
    (``other-key``) and one whose ``agent_id`` is in the URL
    (``agent-target``). Without two identities the auth gate would
    coincidentally pass and the test would never reach the
    ``api_key_agent_mismatch`` branch.
    """
    svc = AsyncMock()

    target = MagicMock()
    target.agent_id = "agent-target"
    target.name = "Target"
    target.subnet_ids = ["subnet-1"]

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
    svc.search_agents = AsyncMock(return_value=[])
    svc.join_subnet = AsyncMock(return_value=None)
    svc.leave_subnet = AsyncMock(return_value=None)
    return svc


@pytest.fixture
def stub_subnet_service():
    """``get_subnet`` raises ``SubnetNotFoundException`` for any
    id ≠ ``subnet-1``; ``add_member`` / ``remove_member`` are
    awaitable no-ops so the join-success path can be exercised
    without standing up a real repository.
    """
    svc = AsyncMock()

    target_subnet = MagicMock()
    target_subnet.subnet_id = "subnet-1"
    target_subnet.name = "test"
    target_subnet.owner = "user-1"
    target_subnet.is_private = False
    target_subnet.is_public = True
    target_subnet.member_count = 0
    target_subnet.description = None
    target_subnet.security_config = {}
    target_subnet.created_at = "2025-01-01T00:00:00Z"
    target_subnet.metadata = {}

    async def _get_subnet(subnet_id: str):
        if subnet_id == "subnet-1":
            return target_subnet
        raise SubnetNotFoundException(subnet_id)

    svc.get_subnet = AsyncMock(side_effect=_get_subnet)
    svc.add_member = AsyncMock(return_value=None)
    svc.remove_member = AsyncMock(return_value=None)
    return svc


def _wire(agent_svc, subnet_svc) -> None:
    app.dependency_overrides[get_agent_service] = lambda: agent_svc
    app.dependency_overrides[get_subnet_service] = lambda: subnet_svc


class TestSubnetsFlatErrorSchema:
    """Pin response shape for the five representative endpoints
    described in the module docstring."""

    def test_get_subnet_404_flat_shape(self, stub_agent_service, stub_subnet_service):
        """``GET /api/v1/subnets/{id}`` — public discovery 404, the
        ``except SubnetNotFoundException as e: raise … from e``
        pattern. Most-used 404 surface for SDK clients consuming
        the subnet catalog."""
        _wire(stub_agent_service, stub_subnet_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/subnets/subnet-missing")

        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "subnet_not_found"
        assert body["details"] == {"subnet_id": "subnet-missing"}
        assert r.headers.get("X-Request-ID") == body["request_id"]

    def test_get_subnet_agents_404_early_out(
        self, stub_agent_service, stub_subnet_service
    ):
        """``GET /api/v1/subnets/{id}/agents`` — the migrated raise
        sits in the *first* ``try`` of a two-try handler. Pins that
        the migration didn't accidentally widen the ``try`` to
        include the privacy-check / member-fetch bodies (which
        would change failure modes for valid subnets)."""
        _wire(stub_agent_service, stub_subnet_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/subnets/subnet-missing/agents")

        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "subnet_not_found"
        assert body["details"] == {"subnet_id": "subnet-missing"}

    def test_join_subnet_403_api_key_agent_mismatch(
        self, stub_agent_service, stub_subnet_service
    ):
        """``POST /api/v1/subnets/{path_agent}/subnets/{subnet_id}``
        — the ``if agent_info[...] != agent_id: raise`` top-level
        check. Cross-tenant joins must surface the path/key tuple
        in ``details`` so the SDK can show "you tried to join with
        a key for X, the path is Y" without a second round-trip.
        Representative for the three identical
        ``API_KEY_AGENT_MISMATCH`` migration sites in subnets.py
        (join, leave, get_agent_subnets)."""
        _wire(stub_agent_service, stub_subnet_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets/agent-target/subnets/subnet-1",
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

    def test_join_subnet_404_subnet_first_try_block(
        self, stub_agent_service, stub_subnet_service
    ):
        """``POST /subnets/{agent}/subnets/{missing_subnet}`` —
        first-of-two ``try`` block raises ``ACNHTTPError`` from
        ``SubnetNotFoundException``. Pins that mid-handler
        ``ACNHTTPError`` propagates without being intercepted by
        the *second* ``try``'s catch-all ``except Exception``."""
        _wire(stub_agent_service, stub_subnet_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets/agent-target/subnets/subnet-missing",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "subnet_not_found"
        assert body["details"] == {"subnet_id": "subnet-missing"}

    def test_join_subnet_404_agent_inside_catchall_try(
        self, stub_agent_service, stub_subnet_service
    ):
        """``POST /subnets/{agent}/subnets/{subnet_id}`` —
        ``join_subnet`` raises ``AgentNotFoundException`` *inside*
        a ``try`` whose final clause is a catch-all
        ``except Exception``. The ``ACNHTTPError`` is raised from
        the ``except AgentNotFoundException`` branch, NOT from
        inside the ``try`` body — so the catch-all does not see
        it. This is the highest-risk migration site in subnets.py
        because ``ACNHTTPError`` is *not* an ``HTTPException``
        subclass (intentionally — see ``acn.core.errors`` docstring),
        meaning the existing ``except HTTPException: raise``
        defence pattern would not protect it; the migration is
        safe only because the raise sits in an ``except`` branch,
        not the ``try`` body."""
        stub_agent_service.join_subnet = AsyncMock(
            side_effect=AgentNotFoundException("agent-target")
        )
        _wire(stub_agent_service, stub_subnet_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets/agent-target/subnets/subnet-1",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "agent_not_found"
        assert body["details"] == {"agent_id": "agent-target"}


class TestSubnetsFlatErrorSchemaCrossModule:
    """Sprint #3-followup — pin the 6 cross-module ErrorCode raises.

    Coverage choice rationale
        Six raise sites spanning four cross-module ErrorCode members
        (``AUTHENTICATION_REQUIRED`` × 2, ``OWNERSHIP_MISMATCH`` × 2,
        ``NOT_SUBNET_MEMBER`` × 1, ``INVALID_REQUEST`` × 1). One
        representative test per site so each site's
        ``details.reason`` and field set is pinned independently —
        unlike registry's `replace_all=true` cohorts, the subnets
        sites differ in payload shape (``subnet_id`` vs
        ``requested_owner`` vs free-form ``reason``) so per-site
        pinning is more useful here.

    list_subnets safety net
        The owner-filter / cross-tenant gates at the top of
        ``list_subnets`` raise ``ACNHTTPError`` from inside a
        ``try`` body whose surrounding ``except Exception`` would
        silently rewrite the 401/403 to 500 if not for the new
        ``except ACNHTTPError: raise`` defence layer added in this
        sprint. The two list_subnets tests below double as
        regression pins for that defence — if a future refactor
        drops the new ``except ACNHTTPError: raise`` line, the
        tests will start seeing 500 responses with
        ``error_code: internal_server_error`` and fail loudly.
    """

    def test_create_subnet_value_error_returns_invalid_request(
        self, stub_agent_service, stub_subnet_service
    ):
        """``POST /api/v1/subnets`` with a body that the service
        rejects with ``ValueError`` — pins ``INVALID_REQUEST``.

        Auth bypass note
            ``create_subnet`` is gated by
            ``require_internal_or_permission("acn:write")``; the
            JWT path uses dev-mode bypass like
            ``test_unregister_returns_404_with_flat_shape`` in
            registry. See that test's docstring for rationale.
        """
        stub_subnet_service.create_subnet = AsyncMock(
            side_effect=ValueError("subnet name already taken")
        )
        _wire(stub_agent_service, stub_subnet_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets",
                headers={"Authorization": "Bearer dev-mode-any-token"},
                json={
                    "subnet_id": "test-subnet",
                    "name": "Test Subnet",
                },
            )

        assert r.status_code == 400
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "invalid_request"
        assert body["details"] == {"reason": "subnet name already taken"}

    def test_list_subnets_owner_filter_no_auth_returns_authentication_required(
        self, stub_agent_service, stub_subnet_service
    ):
        """``GET /api/v1/subnets?owner=...`` without an
        ``Authorization`` header — pins ``AUTHENTICATION_REQUIRED``
        with the owner-filter reason."""
        _wire(stub_agent_service, stub_subnet_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/subnets", params={"owner": "user-1"})

        assert r.status_code == 401
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "authentication_required"
        assert body["details"] == {"reason": "owner_filter_requires_auth"}
        assert r.headers.get("WWW-Authenticate") == "Bearer"

    def test_list_subnets_cross_tenant_returns_ownership_mismatch(
        self, stub_agent_service, stub_subnet_service, monkeypatch
    ):
        """``GET /api/v1/subnets?owner=user-2`` with a non-admin
        token whose ``sub`` is ``user-1`` — pins
        ``OWNERSHIP_MISMATCH``.

        Why monkeypatch instead of dependency_overrides
            ``list_subnets`` calls ``verify_token`` *directly*
            (``payload = await verify_token(request, credentials)``)
            rather than via ``Depends(...)`` — so
            ``app.dependency_overrides[verify_token] = stub`` would
            NOT intercept the call. We monkeypatch the route
            module's reference instead, which IS what gets called.

            Dev-mode bypass would also satisfy the gate (its
            synthetic payload includes ``acn:admin``), so we
            replace the function entirely with one returning a
            non-admin payload to exercise the cross-tenant 403
            branch deterministically.
        """

        async def _fake_verify_token(*args, **kwargs):
            return {"sub": "user-1", "permissions": ["acn:read"]}

        monkeypatch.setattr(
            "acn.routes.subnets.verify_token", _fake_verify_token
        )
        _wire(stub_agent_service, stub_subnet_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/subnets",
                params={"owner": "user-2"},
                headers={"Authorization": "Bearer non-admin-token"},
            )

        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "ownership_mismatch"
        assert body["details"] == {
            "requested_owner": "user-2",
            "token_owner": "user-1",
        }

    def test_get_subnet_agents_private_no_auth_returns_authentication_required(
        self, stub_agent_service, stub_subnet_service
    ):
        """``GET /api/v1/subnets/{id}/agents`` against a private
        subnet without auth — pins ``AUTHENTICATION_REQUIRED`` with
        the ``private_subnet`` reason and ``subnet_id`` in details.

        Distinct from the owner-filter ``AUTHENTICATION_REQUIRED``
        test above: same ErrorCode but the SDK can branch on
        ``details.reason`` to give a more specific UX (e.g. "join
        this subnet" vs "log in to filter by owner").
        """
        # Override stub to mark the subnet as private so the auth
        # check fires.
        stub_subnet_service.get_subnet.side_effect = None
        target_subnet = MagicMock()
        target_subnet.subnet_id = "subnet-private"
        target_subnet.owner = "user-1"
        target_subnet.is_private = True
        stub_subnet_service.get_subnet.return_value = target_subnet

        _wire(stub_agent_service, stub_subnet_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/subnets/subnet-private/agents")

        assert r.status_code == 401
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "authentication_required"
        assert body["details"] == {
            "subnet_id": "subnet-private",
            "reason": "private_subnet",
        }

    def test_get_subnet_agents_private_cross_tenant_returns_not_subnet_member(
        self, stub_agent_service, stub_subnet_service, monkeypatch
    ):
        """``GET /api/v1/subnets/{id}/agents`` against a private
        subnet with a non-owner non-admin token — pins
        ``NOT_SUBNET_MEMBER``. Same monkeypatch pattern as the
        cross-tenant list_subnets test above (``verify_token`` is
        called directly, not via Depends)."""
        stub_subnet_service.get_subnet.side_effect = None
        target_subnet = MagicMock()
        target_subnet.subnet_id = "subnet-private"
        target_subnet.owner = "user-1"
        target_subnet.is_private = True
        stub_subnet_service.get_subnet.return_value = target_subnet

        async def _fake_verify_token(*args, **kwargs):
            return {"sub": "user-2", "permissions": ["acn:read"]}

        monkeypatch.setattr(
            "acn.routes.subnets.verify_token", _fake_verify_token
        )
        _wire(stub_agent_service, stub_subnet_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/subnets/subnet-private/agents",
                headers={"Authorization": "Bearer non-member-token"},
            )

        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "not_subnet_member"
        assert body["details"] == {
            "subnet_id": "subnet-private",
            "agent_id": "user-2",
        }

    def test_delete_subnet_permission_error_returns_ownership_mismatch(
        self, stub_agent_service, stub_subnet_service
    ):
        """``DELETE /api/v1/subnets/{id}`` when the service raises
        ``PermissionError`` — pins ``OWNERSHIP_MISMATCH`` with
        ``subnet_id`` and the underlying reason in ``details``.

        Auth bypass note
            ``delete_subnet`` is gated by ``require_permission("acn:write")``;
            dev-mode bypass like the registry tests.
        """
        stub_subnet_service.delete_subnet = AsyncMock(
            side_effect=PermissionError("Only the subnet owner can delete it.")
        )
        _wire(stub_agent_service, stub_subnet_service)

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/subnets/subnet-1",
                headers={"Authorization": "Bearer dev-mode-any-token"},
            )

        assert r.status_code != 401, (
            "DELETE /subnets/{id} returned 401 — dev_mode auth bypass "
            "is no longer in effect."
        )
        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "ownership_mismatch"
        assert body["details"] == {
            "subnet_id": "subnet-1",
            "reason": "Only the subnet owner can delete it.",
        }


class TestSubnetsCatchAllDefence:
    """P3 cross-module catch-all defence — sweep that adds
    ``except ACNHTTPError: raise`` + ``except HTTPException: raise``
    to every catch-all ``except Exception`` block in subnets/registry/
    tasks routes.

    These tests pin two contracts:

    1. **Latent-bug fix proof**: ``delete_subnet``'s ``else: raise
       HTTPException(404, "Subnet not found")`` short-circuit (in-try)
       was previously rewritten to 500 by the catch-all. The new
       ``except HTTPException: raise`` defence layer makes the 404
       propagate correctly. Pre-defence this test would have asserted
       500; post-defence it asserts 404.

    2. **Forward-looking ACNHTTPError propagation**: any future
       refactor that moves an ``ACNHTTPError`` raise *into* a try
       body (intentionally or not) MUST still see the 4xx propagate.
       We simulate this by mocking the service layer to raise an
       ``ACNHTTPError`` from inside the ``try``, asserting the catch-
       all does NOT swallow it.

    These tests are forward-looking by design — none of the current
    ``ACNHTTPError`` raises live inside any try body in subnets.py
    (they live in either ``except`` clauses or pre-try gates). Pinning
    the defence here means future schema migrations cannot
    accidentally regress the contract.
    """

    def test_delete_subnet_returns_none_propagates_404(
        self, stub_agent_service, stub_subnet_service
    ):
        """When ``subnet_service.delete_subnet`` returns falsy (no
        exception raised), the route's ``else: raise HTTPException(
        404, "Subnet not found")`` short-circuit must produce a 404
        — NOT a 500. Before the catch-all defence sweep, this
        in-try ``HTTPException`` was silently swallowed and rewritten
        to 500 by ``except Exception``."""
        stub_subnet_service.delete_subnet = AsyncMock(return_value=False)
        _wire(stub_agent_service, stub_subnet_service)

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/subnets/subnet-1",
                headers={"Authorization": "Bearer dev-mode-any-token"},
            )

        assert r.status_code == 404, (
            f"delete_subnet None-return path should propagate 404, got "
            f"{r.status_code}: {r.text}. If this is 500, the "
            f"``except HTTPException: raise`` defence in delete_subnet "
            f"is missing or out of order."
        )
        assert r.json() == {"detail": "Subnet not found"}, (
            "Latent-bug fix preserves the legacy ``HTTPException`` shape "
            "for this site (it was deliberately NOT migrated to "
            "``ACNHTTPError`` in sprint #3-followup — the defence sweep "
            "fixes the silent rewrite without changing the wire shape "
            "for this site)."
        )

    def test_create_subnet_inner_acnhttperror_propagates(
        self, stub_agent_service, stub_subnet_service
    ):
        """Forward-looking contract test: if an ``ACNHTTPError`` ever
        gets raised inside a ``try`` body in subnets.py, the catch-all
        ``except Exception`` MUST NOT swallow it. We simulate this by
        making ``subnet_service.create_subnet`` raise an
        ``ACNHTTPError`` directly (bypassing the route's normal
        ``ValueError → INVALID_REQUEST`` mapping). Without
        ``except ACNHTTPError: raise``, this would land as a sanitised
        500."""
        from acn.core.errors import ACNHTTPError, ErrorCode

        stub_subnet_service.create_subnet = AsyncMock(
            side_effect=ACNHTTPError(
                ErrorCode.INVALID_REQUEST,
                400,
                message="Synthetic in-try raise for defence test.",
                details={"reason": "defence_contract_pin"},
            )
        )
        _wire(stub_agent_service, stub_subnet_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/subnets/",
                json={"subnet_id": "sn-x", "name": "x", "owner": "o"},
                headers={"Authorization": "Bearer dev-mode-any-token"},
            )

        assert r.status_code == 400, (
            f"In-try ACNHTTPError must propagate as its declared status "
            f"({400}), got {r.status_code}: {r.text}. If this is 500, "
            f"``except ACNHTTPError: raise`` is missing in create_subnet."
        )
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "invalid_request"
        assert body["details"] == {"reason": "defence_contract_pin"}
