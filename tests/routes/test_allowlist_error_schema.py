"""Allowlist routes — flat ACN error schema contract tests.

Phase 2 review v2 P1 #11 sprint row #1 — pin the four 4xx sites in
``acn/routes/allowlist.py`` to the canonical ``ACNHTTPError`` flat
schema after their migration from raw ``HTTPException``.

The behavioural assertions (status codes, idempotency, owner-only
access) live in ``test_allowlist_routes.py``; this file complements
those by asserting only the *response shape* — the four fields that
SDK clients depend on:

* ``error_code``    — stable ASCII contract (the only field SDK
                      clients should branch on)
* ``message``       — human-readable prose; not stable but always
                      present
* ``details``       — code-specific structured context with the
                      keys documented in
                      ``docs/features/acn-error-schema.md`` §2
* ``request_id``    — UUID echoed in the ``X-Request-ID`` response
                      header

We also assert ``"detail"`` is **absent** from migrated responses —
its presence would indicate a leak of legacy ``HTTPException``
shape (e.g. via a dependency-layer reject) and SDK clients have an
explicit branch that would mis-route those.

Why a separate file?
  Test parametrisation here covers four error codes whose only
  shared fixture surface is the stub services from
  ``test_allowlist_routes.py``; reusing that file would hide the
  schema invariant under a forest of behavioural tests. A small
  dedicated file keeps the schema contract greppable for SDK
  reviewers and obvious to a future migration sprint contributor
  asking "what shape did allowlist promise?".
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException
from acn.routes.dependencies import (
    get_agent_service,
    get_allowlist_service,
)
from acn.services import AllowlistCapacityExceededError, SelfAllowlistError
from acn.services.allowlist_service import MAX_ALLOWLIST_SIZE
from tests.routes.conftest import _assert_flat_shape


@pytest.fixture
def stub_agent_service():
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
    svc.add = AsyncMock(return_value=True)
    svc.remove = AsyncMock(return_value=True)
    svc.list_targets = AsyncMock(return_value=[])
    svc.count = AsyncMock(return_value=0)
    return svc


def _wire(allowlist_svc, agent_svc) -> None:
    app.dependency_overrides[get_allowlist_service] = lambda: allowlist_svc
    app.dependency_overrides[get_agent_service] = lambda: agent_svc


class TestAllowlistFlatErrorSchema:
    """Pin the response shape of all four migrated 4xx sites in
    ``acn/routes/allowlist.py`` (sprint row #1)."""

    def test_403_api_key_agent_mismatch_flat_shape(
        self, stub_allowlist_service, stub_agent_service
    ):
        """Cross-tenant write attempt → 403 with
        ``error_code=api_key_agent_mismatch`` and the documented
        ``{path_agent, key_agent}`` details."""
        _wire(stub_allowlist_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/allowlist/alice",
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

    def test_400_self_allowlist_forbidden_flat_shape(
        self, stub_allowlist_service, stub_agent_service
    ):
        """Owner attempting to allowlist itself → 400 with
        ``error_code=self_allowlist_forbidden`` and ``{owner_id}``."""
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
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "self_allowlist_forbidden"
        assert body["details"] == {"owner_id": "agent-target"}
        assert r.headers.get("X-Request-ID") == body["request_id"]

    def test_404_agent_not_found_flat_shape(
        self, stub_allowlist_service, stub_agent_service
    ):
        """Unknown ``target_id`` → 404 with
        ``error_code=agent_not_found`` and the *target* in
        ``details.agent_id`` (not the owner — the missing entity is
        the target)."""
        stub_allowlist_service.add = AsyncMock(
            side_effect=AgentNotFoundException("Agent ghost not found")
        )
        _wire(stub_allowlist_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/allowlist/ghost",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "agent_not_found"
        assert body["details"] == {"agent_id": "ghost"}
        assert r.headers.get("X-Request-ID") == body["request_id"]

    def test_429_allowlist_capacity_exceeded_flat_shape(
        self, stub_allowlist_service, stub_agent_service
    ):
        """Capacity reached → 429 with
        ``error_code=allowlist_capacity_exceeded`` and
        ``{owner_id, max_size}``. ``max_size`` is the documented
        contract knob — clients can pre-flight from this on retry."""
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
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "allowlist_capacity_exceeded"
        assert body["details"] == {
            "owner_id": "agent-target",
            "max_size": MAX_ALLOWLIST_SIZE,
        }
        assert r.headers.get("X-Request-ID") == body["request_id"]


class TestAllowlistFlatErrorSchemaCoverage:
    """A narrow but high-signal cross-route check: GET and DELETE
    also pass through ``_ensure_owner`` and therefore must emit the
    same 403 ``api_key_agent_mismatch`` flat shape on cross-tenant
    access. The POST case is covered above; these add the missing
    two route methods so a future refactor touching ``_ensure_owner``
    can't quietly drop the schema for one verb."""

    def test_delete_403_emits_flat_shape(
        self, stub_allowlist_service, stub_agent_service
    ):
        _wire(stub_allowlist_service, stub_agent_service)
        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/agents/agent-target/allowlist/alice",
                headers={"Authorization": "Bearer other-key"},
            )
        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "api_key_agent_mismatch"

    def test_get_403_emits_flat_shape(
        self, stub_allowlist_service, stub_agent_service
    ):
        _wire(stub_allowlist_service, stub_agent_service)
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/agent-target/allowlist",
                headers={"Authorization": "Bearer other-key"},
            )
        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "api_key_agent_mismatch"
