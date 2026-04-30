"""Tests for ``/api/v1/communication/manifest/...`` and
``/api/v1/communication/content/{mid}`` — Phase 2 PR #1.

Pins the route-level contract: auth boundaries (owner-only for
manifest queue management, agent API key + owner-derived check for
content fetch), error mapping (404 for cross-tenant probes, never
403, to avoid leaking other agents' queues), and rate-limiting
posture (kill-switch off in tests, sized to mirror /history).

Storage interactions are exercised directly in
``tests/services/test_manifest_service.py``; here we stub the
service to keep these tests focused on the route → service seam.

See docs/features/acn-communication-economic-model.md
"Phase 2 原型 PR 验收清单 — 原型 PR #1" for the assertion list.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.routes.dependencies import (
    _api_key_cache,
    get_agent_service,
    get_manifest_service,
    limiter,
)
from acn.services.manifest_service import ManifestEntry

VALID_INTERNAL_TOKEN = "test-internal-token-min-32-chars-padding"


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
    """Lightweight ManifestService stub.

    Each method returns deterministic data so the test can pin the
    exact route response shape without standing up a real Redis.
    Defaults are "found"/"deleted-true" — individual tests override
    side effects to simulate misses.
    """
    svc = AsyncMock()
    svc.read_since = AsyncMock(
        return_value=[
            ManifestEntry(
                mid="mid-1" + "a" * 26,  # 32 chars to look UUID-like
                sender_id="sender-x",
                summary="hello",
                ts_ms=1_700_000_000_000,
                content_size=42,
            )
        ]
    )
    svc.delete = AsyncMock(return_value=True)
    svc.fetch_content = AsyncMock(return_value={"text": "hi"})
    return svc


@pytest.fixture
def stub_agent_service():
    """Stub AgentService so AgentApiKeyDep + OwnerOrInternalDep work.

    Wires:
      * ``owner-key`` → ``agent-target`` (the manifest queue under
        test).
      * ``other-key`` → ``agent-other`` (used to verify cross-tenant
        404 on content fetch).
    """
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


# --------------------------------------------------------------------------- #
# GET /communication/manifest/{agent_id}
# --------------------------------------------------------------------------- #


class TestListManifest:
    def test_owner_can_list_own_queue(self, stub_manifest_service, stub_agent_service):
        """Happy path: agent-target hits its own manifest with its
        own API key — must succeed and return the stub entry."""
        _wire(stub_manifest_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/communication/manifest/agent-target",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["agent_id"] == "agent-target"
        assert body["count"] == 1
        # Exactly the contract docs/features/...md committed to:
        entry = body["entries"][0]
        assert set(entry.keys()) >= {
            "mid",
            "sender_id",
            "summary",
            "ts",
            "content_size",
        }
        # Service was called with the owner derived from the path.
        stub_manifest_service.read_since.assert_awaited_once()
        kwargs = stub_manifest_service.read_since.await_args.kwargs
        assert kwargs["owner_id"] == "agent-target"

    def test_other_agent_cannot_list_target_queue(
        self, stub_manifest_service, stub_agent_service
    ):
        """OwnerOrInternalDep guarantee: a valid API key for a *different*
        agent must 403 — otherwise any authenticated agent could enumerate
        every other agent's manifest queue, which is exactly the leak the
        owner-scoped dep exists to prevent."""
        _wire(stub_manifest_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/communication/manifest/agent-target",
                headers={"Authorization": "Bearer other-key"},
            )

        assert r.status_code == 403, r.text
        stub_manifest_service.read_since.assert_not_awaited()

    def test_anonymous_returns_401(self, stub_manifest_service, stub_agent_service):
        _wire(stub_manifest_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/communication/manifest/agent-target")

        assert r.status_code == 401, r.text
        stub_manifest_service.read_since.assert_not_awaited()

    def test_internal_token_can_list_any_agent_queue(
        self, stub_manifest_service, stub_agent_service
    ):
        """X-Internal-Token bypass: ops tooling needs to inspect any
        agent's manifest queue for incident response. Must succeed
        without a Bearer header (and without being mistaken for a
        cross-tenant probe)."""
        _wire(stub_manifest_service, stub_agent_service)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.get(
                    "/api/v1/communication/manifest/agent-target",
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        assert r.status_code == 200, r.text
        stub_manifest_service.read_since.assert_awaited_once()


# --------------------------------------------------------------------------- #
# DELETE /communication/manifest/{agent_id}/{mid}
# --------------------------------------------------------------------------- #


class TestDeleteManifestEntry:
    def test_owner_can_delete(self, stub_manifest_service, stub_agent_service):
        _wire(stub_manifest_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/communication/manifest/agent-target/some-mid",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {
            "agent_id": "agent-target",
            "mid": "some-mid",
            "deleted": True,
        }
        stub_manifest_service.delete.assert_awaited_once_with(
            owner_id="agent-target", mid="some-mid"
        )

    def test_unknown_mid_returns_404(self, stub_manifest_service, stub_agent_service):
        """Deleting a non-existent (or already-evicted) entry must
        404. We never reveal "entry exists for another owner" via
        a different code — that would let an attacker probe other
        agents' queues."""
        stub_manifest_service.delete = AsyncMock(return_value=False)
        _wire(stub_manifest_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/communication/manifest/agent-target/missing-mid",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 404, r.text

    def test_other_agent_cannot_delete(
        self, stub_manifest_service, stub_agent_service
    ):
        """Cross-tenant delete must 403 — same guarantee as the list
        endpoint, just on the mutation surface."""
        _wire(stub_manifest_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/communication/manifest/agent-target/some-mid",
                headers={"Authorization": "Bearer other-key"},
            )

        assert r.status_code == 403, r.text
        stub_manifest_service.delete.assert_not_awaited()


# --------------------------------------------------------------------------- #
# GET /communication/content/{mid}
# --------------------------------------------------------------------------- #


class TestFetchManifestContent:
    def test_owner_pulls_content(self, stub_manifest_service, stub_agent_service):
        _wire(stub_manifest_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/communication/content/some-mid",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mid"] == "some-mid"
        assert body["owner_id"] == "agent-target"
        assert body["content"] == {"text": "hi"}
        # Owner-id was derived from the API key, NOT the path —
        # this is the security-critical invariant.
        stub_manifest_service.fetch_content.assert_awaited_once_with(
            owner_id="agent-target", mid="some-mid"
        )

    def test_cross_tenant_returns_404_not_403(
        self, stub_manifest_service, stub_agent_service
    ):
        """Group A #4 / P0-3: when the API-key agent doesn't own
        ``mid`` the route must surface 404 (route layer cannot
        distinguish from "expired"), not 403. A 403 would leak the
        existence of the entry to an attacker probing for other
        agents' content."""
        # Service returns None when called with the (correct, derived)
        # owner_id — simulates "mid belongs to someone else, so
        # fetching as agent-other returns nothing".
        stub_manifest_service.fetch_content = AsyncMock(return_value=None)
        _wire(stub_manifest_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/communication/content/alice-private-mid",
                headers={"Authorization": "Bearer other-key"},
            )

        assert r.status_code == 404, r.text
        # Confirm the service was called with the *caller's* agent_id,
        # not the path/payload.
        stub_manifest_service.fetch_content.assert_awaited_once_with(
            owner_id="agent-other", mid="alice-private-mid"
        )

    def test_invalid_bearer_returns_401(self, stub_manifest_service, stub_agent_service):
        """A Bearer header with an unknown API key must 401. We use
        an invalid key (rather than no header) because
        ``verify_agent_api_key`` declares ``Authorization`` as a
        required header — a missing header surfaces as 422 from
        FastAPI's parameter validator, which is fine but not the
        contract this test pins."""
        _wire(stub_manifest_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/communication/content/some-mid",
                headers={"Authorization": "Bearer not-a-real-key"},
            )

        assert r.status_code == 401, r.text
        stub_manifest_service.fetch_content.assert_not_awaited()
