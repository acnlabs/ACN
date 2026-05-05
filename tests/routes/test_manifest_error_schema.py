"""Manifest routes — flat ACN error schema contract tests.

Phase 2 review v2 P1 #11 sprint row #8 — pin the 2 4xx sites in
``acn/routes/manifest.py`` to the canonical ``ACNHTTPError`` flat
schema after their migration from raw ``HTTPException``.

This file complements ``tests/routes/test_manifest_routes.py``
(route-level behaviour, auth boundaries, security-critical
``404-not-403`` cross-tenant invariant) by asserting only the
*response shape* — the four-field contract SDK clients depend on:

* ``error_code``  — stable ASCII branch key
* ``message``     — human-readable prose, never to be string-matched
* ``details``     — code-specific structured context
* ``request_id``  — UUID echoed in ``X-Request-ID`` response header

Coverage matrix
---------------
2 4xx sites × 2 distinct error codes:

* ``MANIFEST_ENTRY_NOT_FOUND`` (×1, DELETE) — emits
  ``{agent_id, mid}``. ``agent_id`` is the *path* parameter (the
  owner whose queue is being mutated); the route layer does not
  leak whether ``mid`` exists for some *other* owner.
* ``MANIFEST_CONTENT_NOT_FOUND`` (×1, GET content) — emits
  ``{owner_id, mid}``. The field name is intentionally
  ``owner_id`` (not ``agent_id``) because the GET content route
  has *no* path ``agent_id`` parameter — ``owner_id`` is derived
  from the Bearer API key. Calling it ``agent_id`` would imply the
  caller passed it (they didn't) and risk SDK clients trusting
  the value as path-derived.

Security-critical invariant
---------------------------
Cross-tenant attempts MUST surface the same ``error_code`` /
``details`` shape as legitimate misses. A divergent shape would
reintroduce the existence-leak the route's ``404-not-403`` design
explicitly prevents — see ``test_manifest_routes.py
::test_cross_tenant_returns_404_not_403`` for the underlying
behaviour invariant; this file pins the *body shape* for the same
case.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.routes.dependencies import (
    _api_key_cache,
    get_agent_service,
    get_manifest_service,
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
def stub_manifest_service():
    svc = AsyncMock()
    # Phase 3: DELETE pre-fetches the entry to detect attention_fee
    # escrows that need refunding. ``None`` short-circuits straight
    # to the 404 path the tests below pin — same external surface
    # as a missing entry, no escrow round-trip.
    svc.get_entry = AsyncMock(return_value=None)
    svc.delete = AsyncMock(return_value=False)
    svc.fetch_content = AsyncMock(return_value=None)
    return svc


@pytest.fixture
def stub_agent_service():
    """Resolve ``owner-key`` → ``agent-target`` and
    ``other-key`` → ``agent-other``. Mirrors the fixture in
    ``test_manifest_routes.py`` so the auth flow exercised here
    is the same one the behaviour tests already pin."""
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


def _wire(manifest_svc, agent_svc) -> None:
    app.dependency_overrides[get_manifest_service] = lambda: manifest_svc
    app.dependency_overrides[get_agent_service] = lambda: agent_svc


# ============================================================================
# MANIFEST_ENTRY_NOT_FOUND (1 site — DELETE owner-only)
# ============================================================================


class TestManifestEntryNotFoundFlatShape:
    """DELETE on a missing ``mid`` (or a ``mid`` belonging to a
    different owner — same surface) must emit the flat schema
    with ``{agent_id, mid}``."""

    def test_delete_missing_mid_404_flat_shape(
        self, stub_manifest_service, stub_agent_service
    ):
        _wire(stub_manifest_service, stub_agent_service)
        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/communication/manifest/agent-target/missing-mid",
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 404, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "manifest_entry_not_found"
        assert body["details"] == {
            "agent_id": "agent-target",
            "mid": "missing-mid",
        }
        assert r.headers.get("X-Request-ID") == body["request_id"]


# ============================================================================
# MANIFEST_CONTENT_NOT_FOUND (1 site — GET content, owner-derived)
# ============================================================================


class TestManifestContentNotFoundFlatShape:
    """GET ``/communication/content/{mid}`` against a
    missing/expired/cross-tenant mid emits flat schema with
    ``{owner_id, mid}`` — NOT ``{agent_id, mid}``: this route has
    no path ``agent_id``, the owner is derived from the API key,
    and naming the field ``agent_id`` would mislead SDK clients."""

    def test_legit_miss_404_flat_shape(
        self, stub_manifest_service, stub_agent_service
    ):
        """Owner with a valid key, but the content has expired or
        never existed for them."""
        _wire(stub_manifest_service, stub_agent_service)
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/communication/content/expired-mid",
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 404, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "manifest_content_not_found"
        assert body["details"] == {
            "owner_id": "agent-target",
            "mid": "expired-mid",
        }
        assert r.headers.get("X-Request-ID") == body["request_id"]

    def test_cross_tenant_miss_emits_same_shape_as_legit_miss(
        self, stub_manifest_service, stub_agent_service
    ):
        """Security-critical: a caller probing for *another* agent's
        content surfaces the *exact same* error_code + details shape
        as a legitimate own-content miss. Divergence here would
        reintroduce the existence-leak the 404-not-403 design
        explicitly prevents.

        ``details.owner_id`` is the *probing* caller (agent-other),
        because that's what the route saw when fetching. We
        deliberately do NOT include the real owner of the mid (which
        the caller has no right to know)."""
        _wire(stub_manifest_service, stub_agent_service)
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/communication/content/alice-private-mid",
                headers={"Authorization": "Bearer other-key"},
            )
        assert r.status_code == 404, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "manifest_content_not_found"
        # owner_id is the *caller* (probing agent), not alice.
        # If this assertion ever flips to "alice", the route has
        # started leaking ownership info — that's the exact
        # regression this test exists to catch.
        assert body["details"] == {
            "owner_id": "agent-other",
            "mid": "alice-private-mid",
        }
