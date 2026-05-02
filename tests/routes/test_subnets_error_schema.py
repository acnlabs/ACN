"""Subnets routes — flat ACN error schema contract tests.

Phase 2 review v2 P1 #11 sprint row #3 — pin the migrated 4xx
sites in ``acn/routes/subnets.py`` to the canonical
``ACNHTTPError`` flat schema after their conversion from raw
``HTTPException``.

#3 scope (this file): the 13 4xx sites that map directly to
existing catalog codes — 7 ``SUBNET_NOT_FOUND`` + 3
``AGENT_NOT_FOUND`` + 3 ``API_KEY_AGENT_MISMATCH``.

Out of scope (deferred to later sprints, see BACKLOG)
  * 6 4xx sites that need new catalog codes — 2 auth (401), 3
    permission (403), 1 ``ValueError`` (400 on ``create_subnet``).
  * 1 4xx site (``delete_subnet`` ``success=False`` short-circuit
    *inside* ``try``) that today is already silently rewritten to
    500 by the catch-all ``except Exception`` — this is a
    pre-existing bug, fixing it requires the ``except ACNHTTPError:
    raise`` defence ticket and is intentionally not in #3 scope.
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
