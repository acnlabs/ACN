"""Production dual-protocol behaviour for ``verify_token`` and
``require_internal_or_permission`` (issue #114 Scope A B1).

Pre-V6 the production branch of ``verify_token`` only accepted JWTs —
agents calling ACL-gated read endpoints (``GET /api/v1/subnets/{id}``,
``DELETE /api/v1/subnets/{id}``, …) with their ``acn_*`` API key were
401'd before reaching the ACL. PR #112 documented an "owner / member /
admin" privacy contract that consequently never fired in production for
any caller except ``acn:admin``.

This file pins the V6 dual-protocol behaviour:

- ``Bearer <jwt>`` → JWT path, payload carries ``"type": "user"``.
- ``Bearer acn_<api_key>`` → API-key path, payload carries
  ``"type": "agent"`` and **no** ``acn:admin`` permission.
- Invalid ``acn_*`` key → 401 with a precise audit reason
  (``api_key_invalid``), not a JWT failure.
- Missing credentials → 401 ``bearer_missing`` (unchanged).

Tests deliberately stay below the FastAPI HTTP layer so they exercise
the dispatch logic directly. ``_verify_jwt`` and
``_resolve_agent_id_from_api_key`` are stubbed; only the prefix-dispatch
+ payload-shape contract is under test here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from acn.auth import middleware as mw
from acn.core.entities import Agent


def _make_agent(agent_id: str = "agent-uuid-prod") -> Agent:
    return Agent(
        agent_id=agent_id,
        owner="user-1",
        name="Prod Agent",
        endpoint="https://agent.example.com",
        description="x",
        tags=[],
        subnet_ids=[],
        metadata={},
    )


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _stub_request() -> MagicMock:
    return MagicMock(name="Request")


@pytest.fixture(autouse=True)
def _force_prod_mode(monkeypatch):
    """Pin production mode regardless of env state — these tests are
    *about* what the production branch does."""
    fake_settings = MagicMock()
    fake_settings.dev_mode = False
    fake_settings.internal_api_token = "x" * 32
    fake_settings.auth0_domain = "test.auth0.com"
    fake_settings.auth0_audience = "test-audience"
    monkeypatch.setattr(mw, "_get_settings", lambda: fake_settings)
    return fake_settings


def _patch_agent_service(monkeypatch, *, returns: Agent | None):
    service = MagicMock()
    service.get_agent_by_api_key = AsyncMock(return_value=returns)

    from acn.routes import dependencies as deps

    monkeypatch.setattr(deps, "get_agent_service", lambda: service)
    return service


def _patch_verify_jwt(monkeypatch, payload: dict | Exception):
    """Stub ``_verify_jwt`` to return ``payload`` (or raise it if it's
    an exception). The real JWKS / Auth0 path is unrelated to the
    dispatch contract under test."""
    if isinstance(payload, Exception):
        async def _raise(*args, **kwargs):
            raise payload
        monkeypatch.setattr(mw, "_verify_jwt", _raise)
    else:
        async def _return(*args, **kwargs):
            return payload
        monkeypatch.setattr(mw, "_verify_jwt", _return)


# ---------------------------------------------------------------------------
# verify_token: API-key path (acn_* prefix)
# ---------------------------------------------------------------------------


class TestVerifyTokenApiKeyPath:
    @pytest.mark.asyncio
    async def test_valid_api_key_returns_agent_payload(self, monkeypatch):
        _patch_agent_service(monkeypatch, returns=_make_agent("agent-prod-1"))

        payload = await mw.verify_token(_stub_request(), _bearer("acn_valid"))

        assert payload["sub"] == "agent-prod-1"
        assert payload["type"] == "agent"

    @pytest.mark.asyncio
    async def test_api_key_payload_excludes_admin_permission(self, monkeypatch):
        """Agents authenticating via API key must NOT receive
        ``acn:admin``. Admin is a user-domain platform permission
        (ops / SRE) that does not flow through the marketplace —
        an agent that 'self-elevates' to admin via its own API key
        would defeat the auth0 / role-based admin model entirely.
        """
        _patch_agent_service(monkeypatch, returns=_make_agent("agent-prod-2"))

        payload = await mw.verify_token(_stub_request(), _bearer("acn_valid"))

        assert "acn:admin" not in payload["permissions"]
        assert set(payload["permissions"]) == {"acn:read", "acn:write"}

    @pytest.mark.asyncio
    async def test_invalid_api_key_raises_401_with_api_key_reason(
        self, monkeypatch
    ):
        """Unknown ``acn_*`` token: must 401 cleanly (not silently fall
        back to JWT, which would emit a misleading
        ``jwt_invalid`` audit row for what is really a stale or
        revoked API key)."""
        _patch_agent_service(monkeypatch, returns=None)

        with pytest.raises(HTTPException) as excinfo:
            await mw.verify_token(_stub_request(), _bearer("acn_revoked"))

        assert excinfo.value.status_code == 401
        assert "Invalid API key" in excinfo.value.detail

    @pytest.mark.asyncio
    async def test_api_key_path_does_not_invoke_jwt_verification(
        self, monkeypatch
    ):
        """Prefix dispatch — ``acn_*`` traffic must not reach
        ``_verify_jwt`` and pollute its audit log with synthetic
        ``jwt_invalid`` rows."""
        _patch_agent_service(monkeypatch, returns=_make_agent("agent-prod-3"))
        jwt_calls: list[str] = []

        async def _spy(*args, **kwargs):
            jwt_calls.append(args[0] if args else "")
            return {"sub": "should-never-be-returned"}

        monkeypatch.setattr(mw, "_verify_jwt", _spy)

        await mw.verify_token(_stub_request(), _bearer("acn_x"))

        assert jwt_calls == [], "API-key path must not call _verify_jwt"


# ---------------------------------------------------------------------------
# verify_token: JWT path (default)
# ---------------------------------------------------------------------------


class TestVerifyTokenJwtPath:
    @pytest.mark.asyncio
    async def test_valid_jwt_returns_user_typed_payload(self, monkeypatch):
        _patch_verify_jwt(
            monkeypatch,
            {"sub": "auth0|user-123", "permissions": ["acn:read"]},
        )

        payload = await mw.verify_token(_stub_request(), _bearer("eyJhbGciOi.fake"))

        assert payload["sub"] == "auth0|user-123"
        assert payload["type"] == "user"
        assert payload["permissions"] == ["acn:read"]

    @pytest.mark.asyncio
    async def test_jwt_path_preserves_existing_type_field(self, monkeypatch):
        """If ``_verify_jwt`` (or some intermediate JWT enricher) ever
        attaches its own ``type`` field, ``verify_token`` must not
        overwrite it — ``setdefault`` is the correct primitive here."""
        _patch_verify_jwt(
            monkeypatch,
            {"sub": "auth0|x", "type": "service", "permissions": []},
        )

        payload = await mw.verify_token(_stub_request(), _bearer("jwt-with-type"))

        assert payload["type"] == "service"

    @pytest.mark.asyncio
    async def test_invalid_jwt_propagates_401(self, monkeypatch):
        """Non-prefixed garbage falls through to the JWT path; failure
        is reported as a JWT error (not an API-key error)."""
        _patch_verify_jwt(
            monkeypatch,
            HTTPException(status_code=401, detail="Invalid token."),
        )

        with pytest.raises(HTTPException) as excinfo:
            await mw.verify_token(_stub_request(), _bearer("garbage"))

        assert excinfo.value.status_code == 401
        assert "Invalid token" in excinfo.value.detail


# ---------------------------------------------------------------------------
# verify_token: missing credentials
# ---------------------------------------------------------------------------


class TestVerifyTokenMissingCredentials:
    @pytest.mark.asyncio
    async def test_no_credentials_raises_401_bearer_missing(self):
        with pytest.raises(HTTPException) as excinfo:
            await mw.verify_token(_stub_request(), None)

        assert excinfo.value.status_code == 401
        assert "Authorization header required" in excinfo.value.detail
        assert excinfo.value.headers == {"WWW-Authenticate": "Bearer"}


# ---------------------------------------------------------------------------
# require_internal_or_permission: production branches
# ---------------------------------------------------------------------------


class TestRequireInternalOrPermissionProduction:
    @pytest.mark.asyncio
    async def test_internal_token_payload_has_internal_type(self, monkeypatch):
        """Internal-token branch returns a synthetic payload — V6 ACL
        code branches on ``payload["type"]`` so it must carry a
        non-user, non-agent discriminator (``"internal"``)."""
        _patch_verify_jwt(monkeypatch, {"sub": "should-not-reach-jwt"})
        checker = mw.require_internal_or_permission("acn:write")

        payload = await checker(_stub_request(), None, "x" * 32)

        assert payload["sub"] == "backend@internal"
        assert payload["type"] == "internal"
        assert "acn:admin" in payload["permissions"]

    @pytest.mark.asyncio
    async def test_jwt_with_permission_passes(self, monkeypatch):
        _patch_verify_jwt(
            monkeypatch,
            {"sub": "auth0|alice", "permissions": ["acn:write"]},
        )
        checker = mw.require_internal_or_permission("acn:write")

        payload = await checker(_stub_request(), _bearer("eyJ.fake"), None)

        assert payload["sub"] == "auth0|alice"
        assert payload["type"] == "user"

    @pytest.mark.asyncio
    async def test_jwt_missing_permission_403s(self, monkeypatch):
        _patch_verify_jwt(
            monkeypatch,
            {"sub": "auth0|bob", "permissions": ["acn:read"]},
        )
        checker = mw.require_internal_or_permission("acn:write")

        with pytest.raises(HTTPException) as excinfo:
            await checker(_stub_request(), _bearer("eyJ.fake"), None)

        assert excinfo.value.status_code == 403

    @pytest.mark.asyncio
    async def test_api_key_propagates_through_verify_token(self, monkeypatch):
        """``require_internal_or_permission`` delegates to
        ``verify_token`` for the JWT/API-key branch — make sure the
        dual-protocol contract carries through (no extra patch
        needed; just confirm the agent payload reaches the
        permission check)."""
        _patch_agent_service(monkeypatch, returns=_make_agent("agent-rip"))
        checker = mw.require_internal_or_permission("acn:write")

        payload = await checker(_stub_request(), _bearer("acn_x"), None)

        assert payload["sub"] == "agent-rip"
        assert payload["type"] == "agent"
        # Agent has acn:write, so the permission check passes.
        assert "acn:write" in payload["permissions"]

    @pytest.mark.asyncio
    async def test_api_key_lacks_admin_403s_when_admin_required(self, monkeypatch):
        """Closes the self-elevation hole: an agent's API key must not
        unlock ``acn:admin``-gated endpoints."""
        _patch_agent_service(monkeypatch, returns=_make_agent("agent-rip-admin"))
        checker = mw.require_internal_or_permission("acn:admin")

        with pytest.raises(HTTPException) as excinfo:
            await checker(_stub_request(), _bearer("acn_x"), None)

        assert excinfo.value.status_code == 403
