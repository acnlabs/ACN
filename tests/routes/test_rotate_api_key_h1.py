"""Wire-level tests for ``POST /api/v1/agents/{agent_id}/rotate-key``.

H1 (pre-launch audit) gives agents and owners a way to *replace* a
leaked or aging API key without re-registering the agent (which would
otherwise burn its agent_id, reputation, and ERC-8004 binding).

These tests pin the contract:

* Anyone can rotate via the agent's *own* current key — the common
  "scheduled rotation" / "I suspect a leak" workflow.
* An agent cannot rotate *another* agent's key (cross-tenant block).
* The owner can rotate via Auth0 JWT — used when the agent itself
  has lost the key or won't cooperate.
* Anonymous callers, invalid keys, and JWTs from non-owners are
  rejected before any mutation.
* On success the response body returns a fresh ``acn_*`` plaintext
  exactly once and the server stores only the SHA-256 hash. The old
  cached entry is evicted immediately (rate-limit / proxy caches
  served the rotated agent within ``_API_KEY_CACHE_TTL`` of the
  rotation would otherwise let the leaked key linger for up to 60s).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException
from acn.routes.dependencies import (
    _api_key_cache,
    _api_key_cache_by_agent,
    get_agent_service,
)
from acn.services.agent_service import hash_api_key


@pytest.fixture
def stub_agent_service():
    """Service stub backing the rotate-key wire tests.

    State model
    -----------
    * ``agent-target`` (owner ``owner-alice``) holds a "current"
      plaintext key ``acn_target_current``. ``get_agent_by_api_key``
      resolves it to ``agent-target`` so the agent-self auth path
      works.
    * ``agent-other`` (owner ``owner-bob``) holds
      ``acn_other_current`` — used to assert the cross-tenant block.
    * ``rotate_api_key`` mints a deterministic-but-fresh acn_* string
      and overwrites the stored key with its hash. Each invocation
      bumps a counter so two calls produce two different plaintexts
      (matching the real-service invariant covered in the unit tests).
    """
    svc = AsyncMock()

    target = MagicMock()
    target.agent_id = "agent-target"
    target.owner = "owner-alice"
    target.name = "Target"
    target.api_key = hash_api_key("acn_target_current")

    other = MagicMock()
    other.agent_id = "agent-other"
    other.owner = "owner-bob"
    other.name = "Other"
    other.api_key = hash_api_key("acn_other_current")

    agents_by_id = {"agent-target": target, "agent-other": other}

    async def _by_api_key(key: str):
        if key == "acn_target_current":
            return target
        if key == "acn_other_current":
            return other
        return None

    async def _get_agent(agent_id: str):
        try:
            return agents_by_id[agent_id]
        except KeyError as e:
            raise AgentNotFoundException(agent_id) from e

    counter = {"n": 0}

    async def _rotate(agent_id: str):
        agent = await _get_agent(agent_id)  # raises if missing
        counter["n"] += 1
        new_plaintext = f"acn_NEW_KEY_{counter['n']}"
        agent.api_key = hash_api_key(new_plaintext)
        return new_plaintext

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)
    svc.get_agent = AsyncMock(side_effect=_get_agent)
    svc.rotate_api_key = AsyncMock(side_effect=_rotate)
    return svc


def _wire(svc) -> None:
    app.dependency_overrides[get_agent_service] = lambda: svc


# --------------------------------------------------------------------------- #
# Agent-self path (Bearer acn_*)
# --------------------------------------------------------------------------- #


class TestAgentSelfRotation:
    def test_agent_rotates_own_key_returns_fresh_plaintext(
        self, stub_agent_service, monkeypatch
    ):
        """The canonical happy path: an agent presents its current
        key and gets a brand-new key back. The new key must be a
        valid ``acn_*`` string and must differ from anything the
        client sent in the request."""
        # Disable dev-mode so the *real* agent-key dispatcher branch
        # fires; otherwise dev-mode would synthesise a "dev" payload
        # and bypass the agent_id self-check the test pins.
        from acn.config import get_settings

        monkeypatch.setattr(get_settings(), "dev_mode", False)

        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/rotate-key",
                headers={"Authorization": "Bearer acn_target_current"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        assert body["agent_id"] == "agent-target"
        assert body["api_key"].startswith("acn_")
        assert body["api_key"] != "acn_target_current", (
            "rotation must return a *new* key, never echo the caller's "
            "current one back"
        )
        stub_agent_service.rotate_api_key.assert_awaited_once_with("agent-target")

    def test_agent_cannot_rotate_another_agents_key(
        self, stub_agent_service, monkeypatch
    ):
        """The central cross-tenant boundary: a valid agent API key
        only authorises rotating *that agent's own* credential.
        Otherwise any compromised agent key would let the attacker
        invalidate every other agent's credential."""
        from acn.config import get_settings

        monkeypatch.setattr(get_settings(), "dev_mode", False)
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-other/rotate-key",
                # Bearer is agent-target's key — we try to rotate
                # agent-other. Must be rejected.
                headers={"Authorization": "Bearer acn_target_current"},
            )

        assert r.status_code == 403, r.text
        body = r.json()
        assert body["error_code"] == "missing_permission"
        assert body["details"] == {"reason": "agent_can_only_rotate_own_key"}
        # Critically: the rotation must NOT have happened, otherwise
        # the 403 is just a misleading message after the damage was
        # already done.
        stub_agent_service.rotate_api_key.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Owner path (Auth0 JWT)
# --------------------------------------------------------------------------- #


class TestOwnerJwtRotation:
    def test_owner_rotates_via_jwt(self, stub_agent_service, monkeypatch):
        """The recovery flow: agent has lost its key, owner steps in
        via Auth0 to force-rotate. The owner's ``sub`` must match
        the agent's ``owner`` field."""
        from acn.config import get_settings

        async def _fake_verify_token(*args, **kwargs):
            return {"sub": "owner-alice", "permissions": ["acn:write"]}

        monkeypatch.setattr(get_settings(), "dev_mode", False)
        monkeypatch.setattr("acn.routes.registry.verify_token", _fake_verify_token)
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/rotate-key",
                headers={"Authorization": "Bearer some-auth0-jwt"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        assert body["agent_id"] == "agent-target"
        assert body["api_key"].startswith("acn_")

    def test_owner_jwt_without_acn_write_still_rotates(
        self, stub_agent_service, monkeypatch
    ):
        """Labs SPA owners typically have openid/profile/email only —
        recovery must not require Auth0 RBAC ``acn:write``."""
        from acn.config import get_settings

        async def _fake_verify_token(*args, **kwargs):
            return {"sub": "owner-alice", "permissions": []}

        monkeypatch.setattr(get_settings(), "dev_mode", False)
        monkeypatch.setattr("acn.routes.registry.verify_token", _fake_verify_token)
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/rotate-key",
                headers={"Authorization": "Bearer some-auth0-jwt"},
            )

        assert r.status_code == 200, r.text
        assert r.json()["api_key"].startswith("acn_")

    def test_non_owner_jwt_rejected_with_ownership_mismatch(
        self, stub_agent_service, monkeypatch
    ):
        """A valid human JWT is *not* a free pass — it must be the
        *specific* owner of the target agent. Otherwise any logged-in
        Auth0 user could rotate every agent's key on the platform."""
        from acn.config import get_settings

        async def _fake_verify_token(*args, **kwargs):
            # owner-bob owns agent-other, NOT agent-target
            return {"sub": "owner-bob", "permissions": ["acn:write"]}

        monkeypatch.setattr(get_settings(), "dev_mode", False)
        monkeypatch.setattr("acn.routes.registry.verify_token", _fake_verify_token)
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/rotate-key",
                headers={"Authorization": "Bearer some-auth0-jwt"},
            )

        assert r.status_code == 403, r.text
        body = r.json()
        assert body["error_code"] == "ownership_mismatch"
        assert body["details"] == {
            "agent_id": "agent-target",
            "reason": "not_agent_owner",
        }
        stub_agent_service.rotate_api_key.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Anonymous / missing / unknown agent
# --------------------------------------------------------------------------- #


class TestRotationAuthFailures:
    def test_no_auth_header_returns_401(self, stub_agent_service, monkeypatch):
        """Anonymous rotation is never allowed — the entire endpoint
        is a credential mutator and would be useless if unauthed
        callers could hit it."""
        from acn.config import get_settings

        async def _fake_verify_token(*args, **kwargs):
            from acn.core.errors import ACNHTTPError, ErrorCode

            raise ACNHTTPError(ErrorCode.AUTHENTICATION_REQUIRED, 401, details={})

        monkeypatch.setattr(get_settings(), "dev_mode", False)
        monkeypatch.setattr("acn.routes.registry.verify_token", _fake_verify_token)
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.post("/api/v1/agents/agent-target/rotate-key")

        assert r.status_code == 401, r.text
        stub_agent_service.rotate_api_key.assert_not_awaited()

    def test_invalid_agent_key_returns_401(self, stub_agent_service, monkeypatch):
        """A Bearer that *looks* like an agent key (``acn_*``) but
        resolves to no agent must return 401, not 403 — same shape
        as every other ``invalid_agent_api_key`` failure across the
        codebase so a single client error handler covers everything."""
        from acn.config import get_settings

        monkeypatch.setattr(get_settings(), "dev_mode", False)
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/rotate-key",
                headers={"Authorization": "Bearer acn_does_not_exist"},
            )

        assert r.status_code == 401, r.text
        body = r.json()
        assert body["error_code"] == "authentication_required"
        assert body["details"] == {"reason": "invalid_agent_api_key"}
        stub_agent_service.rotate_api_key.assert_not_awaited()

    def test_unknown_agent_returns_404(self, stub_agent_service, monkeypatch):
        """Hitting an agent_id that doesn't exist returns 404 — but
        ONLY after auth has passed (a valid key authorising a real
        agent). Otherwise the 404 would let an attacker enumerate
        existing agent_ids by comparing 404 vs 403.
        """
        from acn.config import get_settings

        async def _fake_verify_token(*args, **kwargs):
            return {"sub": "owner-alice", "permissions": ["acn:write"]}

        monkeypatch.setattr(get_settings(), "dev_mode", False)
        monkeypatch.setattr("acn.routes.registry.verify_token", _fake_verify_token)
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-ghost/rotate-key",
                headers={"Authorization": "Bearer some-auth0-jwt"},
            )

        assert r.status_code == 404, r.text
        body = r.json()
        assert body["error_code"] == "agent_not_found"
        assert body["details"] == {"agent_id": "agent-ghost"}


# --------------------------------------------------------------------------- #
# Auth cache invalidation
# --------------------------------------------------------------------------- #


class TestRotationInvalidatesAuthCache:
    def test_cached_old_key_evicted_after_rotation(
        self, stub_agent_service, monkeypatch
    ):
        """After a successful rotation the OLD key must immediately
        stop authenticating — even though the in-memory
        ``_api_key_cache`` would otherwise serve a 60-second-stale
        entry pointing at the same agent.

        We prime the cache exactly the way ``_cache_agent`` does, and
        then assert that after rotation the cache no longer contains
        the entry. ``_api_key_cache_by_agent`` is the reverse index
        the M3 fix relies on for O(1) eviction.
        """
        from acn.config import get_settings
        from acn.routes.dependencies import _cache_agent

        monkeypatch.setattr(get_settings(), "dev_mode", False)
        _wire(stub_agent_service)

        _cache_agent(
            api_key="acn_target_current",
            agent_id="agent-target",
            name="Target",
            wallet_address=None,
        )
        assert hash_api_key("acn_target_current") in _api_key_cache
        assert "agent-target" in _api_key_cache_by_agent

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/agents/agent-target/rotate-key",
                headers={"Authorization": "Bearer acn_target_current"},
            )
        assert r.status_code == 200, r.text

        # The reverse index entry must be gone — that's the M3
        # invariant the route relies on. Without this, a leaked key
        # remains usable up to ``_API_KEY_CACHE_TTL`` past the
        # rotation.
        assert "agent-target" not in _api_key_cache_by_agent
        assert hash_api_key("acn_target_current") not in _api_key_cache
