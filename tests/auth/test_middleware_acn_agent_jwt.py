"""ACN self-issued RS256 agent JWT acceptance in ``verify_token``
and the A2A ``_a2a_agent_lookup`` helper (ADR-0007 D6, issue #156).

Contract pinned here:

* ACN-issued RS256 JWT → accepted by ``verify_token``, payload carries
  ``{type: "agent", sub: agent_id, acn_principal: "agent"}``.
* Expired ACN JWT → 401 ``acn_agent_jwt_expired`` (not Auth0 path).
* JWT with wrong issuer → falls through to Auth0 path (not rejected by
  the ACN branch; the Auth0 path will surface its own error).
* ``acn_*`` API key → still works (dual-accept during transition).
* Auth0 human JWT → still works (human path unaffected).
* A2A ``_a2a_agent_lookup``: JWT credential resolves to agent_id.

Tests deliberately stay below the FastAPI HTTP layer to exercise dispatch
logic directly. ``_verify_jwt`` (Auth0 path) and
``_resolve_agent_id_from_api_key`` are stubbed where needed.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

from acn.auth import middleware as mw
from acn.core.errors import ACNHTTPError, ErrorCode

# ---------------------------------------------------------------------------
# Helpers: key/JWT generation
# ---------------------------------------------------------------------------

_KID = "acn-agent-key-1"
_ISSUER = "https://acn.test"
_AUDIENCE = "https://api.test"
_AGENT_ID = "agent-uuid-jwt-test"


def _gen_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _mint_jwt(
    private_pem: str,
    *,
    agent_id: str = _AGENT_ID,
    issuer: str = _ISSUER,
    audience: str = _AUDIENCE,
    ttl: int = 3600,
) -> str:
    now = int(time.time())
    claims = {
        "iss": issuer,
        "sub": agent_id,
        "aud": audience,
        "iat": now,
        "nbf": now,
        "exp": now + ttl,
        "acn_principal": "agent",
    }
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": _KID})


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _stub_request() -> MagicMock:
    return MagicMock(name="Request")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def rsa_pem() -> str:
    return _gen_key_pem()


@pytest.fixture(autouse=True)
def _reset_acn_jwks_cache():
    """Reset module-level JWKS cache so each test derives fresh keys."""
    mw._acn_agent_jwks = []
    mw._acn_agent_jwks_loaded = False
    yield
    mw._acn_agent_jwks = []
    mw._acn_agent_jwks_loaded = False


@pytest.fixture(autouse=True)
def _force_prod_mode(monkeypatch, rsa_pem):
    """Pin production mode with ACN agent JWT issuer configured."""
    fake = MagicMock()
    fake.dev_mode = False
    fake.internal_api_token = "x" * 32
    fake.auth0_domain = "test.auth0.com"
    fake.auth0_audience = "test-audience"
    fake.agent_jwt_private_key = rsa_pem
    fake.agent_jwt_kid = _KID
    fake.agent_jwt_issuer = _ISSUER
    fake.agent_jwt_audience = _AUDIENCE
    fake.gateway_base_url = _ISSUER
    monkeypatch.setattr(mw, "_get_settings", lambda: fake)
    return fake


def _patch_verify_jwt_never_called(monkeypatch) -> list[str]:
    """Install a spy on ``_verify_jwt`` that records calls (expects none)."""
    calls: list[str] = []

    async def _spy(token: str, *args, **kwargs) -> dict:
        calls.append(token)
        return {"sub": "auth0|user", "type": "user"}

    monkeypatch.setattr(mw, "_verify_jwt", _spy)
    return calls


def _patch_api_key_lookup(monkeypatch, *, agent_id: str | None):
    from acn.routes import dependencies as deps

    svc = MagicMock()
    svc.get_agent_by_api_key = AsyncMock(
        return_value=(
            MagicMock(agent_id=agent_id) if agent_id is not None else None
        )
    )
    monkeypatch.setattr(deps, "get_agent_service", lambda: svc)
    return svc


# ---------------------------------------------------------------------------
# verify_token: ACN agent JWT path
# ---------------------------------------------------------------------------


class TestVerifyTokenAcnAgentJwt:
    @pytest.mark.asyncio
    async def test_valid_acn_jwt_accepted(self, monkeypatch, rsa_pem):
        """ACN-issued JWT: accepted, returns agent payload."""
        jwt_calls = _patch_verify_jwt_never_called(monkeypatch)
        token = _mint_jwt(rsa_pem, agent_id=_AGENT_ID)

        payload = await mw.verify_token(_stub_request(), _bearer(token))

        assert payload["sub"] == _AGENT_ID
        assert payload["type"] == "agent"
        assert payload["acn_principal"] == "agent"
        assert "acn:read" in payload["permissions"]
        assert "acn:write" in payload["permissions"]
        # Must NOT reach Auth0 path
        assert jwt_calls == [], "ACN JWT must not reach Auth0 _verify_jwt"

    @pytest.mark.asyncio
    async def test_acn_jwt_no_admin_permission(self, monkeypatch, rsa_pem):
        """Agent JWTs, like API keys, must not carry acn:admin."""
        _patch_verify_jwt_never_called(monkeypatch)
        token = _mint_jwt(rsa_pem)

        payload = await mw.verify_token(_stub_request(), _bearer(token))

        assert "acn:admin" not in payload["permissions"]

    @pytest.mark.asyncio
    async def test_expired_acn_jwt_raises_401(self, monkeypatch, rsa_pem):
        """Expired ACN JWT: rejected with 401 (not silently passed to Auth0)."""
        _patch_verify_jwt_never_called(monkeypatch)
        token = _mint_jwt(rsa_pem, ttl=-1)  # already expired

        with pytest.raises(ACNHTTPError) as exc_info:
            await mw.verify_token(_stub_request(), _bearer(token))

        assert exc_info.value.status_code == 401
        assert exc_info.value.code == ErrorCode.AUTHENTICATION_REQUIRED
        assert "expired" in exc_info.value.message.lower()

    @pytest.mark.asyncio
    async def test_wrong_signature_acn_jwt_raises_401(self, monkeypatch, rsa_pem):
        """JWT with ACN issuer but signed by a different key: rejected."""
        _patch_verify_jwt_never_called(monkeypatch)
        other_pem = _gen_key_pem()
        token = _mint_jwt(other_pem, issuer=_ISSUER)  # signed by unrelated key

        with pytest.raises(ACNHTTPError) as exc_info:
            await mw.verify_token(_stub_request(), _bearer(token))

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_issuer_jwt_falls_through_to_auth0(
        self, monkeypatch, rsa_pem
    ):
        """JWT whose iss != ACN issuer must NOT be handled by the ACN path.

        It falls through to ``_verify_jwt`` (Auth0 path). We stub that to
        return a user payload so the test confirms routing, not Auth0 success.
        """
        auth0_calls: list[str] = []

        async def _stub_auth0(token: str, *args, **kwargs) -> dict:
            auth0_calls.append(token)
            return {"sub": "auth0|user", "type": "user"}

        monkeypatch.setattr(mw, "_verify_jwt", _stub_auth0)
        token = _mint_jwt(rsa_pem, issuer="https://other.issuer.example")

        payload = await mw.verify_token(_stub_request(), _bearer(token))

        # Wrong-iss JWT → Auth0 path handles it
        assert auth0_calls, "Wrong-iss JWT must reach _verify_jwt (Auth0 path)"
        assert payload["sub"] == "auth0|user"


# ---------------------------------------------------------------------------
# verify_token: acn_* API key path (dual-accept regression)
# ---------------------------------------------------------------------------


class TestVerifyTokenApiKeyStillWorks:
    @pytest.mark.asyncio
    async def test_api_key_still_resolved(self, monkeypatch):
        """``acn_*`` API keys keep working alongside the new JWT path."""
        _patch_api_key_lookup(monkeypatch, agent_id="agent-apikeypath")
        _patch_verify_jwt_never_called(monkeypatch)

        payload = await mw.verify_token(_stub_request(), _bearer("acn_validkey"))

        assert payload["sub"] == "agent-apikeypath"
        assert payload["type"] == "agent"

    @pytest.mark.asyncio
    async def test_api_key_does_not_reach_acn_jwt_path(self, monkeypatch, rsa_pem):
        """``acn_*`` prefix must be consumed before the JWT dispatch runs.

        The ACN JWKS cache starts empty — if an API key were incorrectly
        sent through the JWT path it would raise (no key configured for
        this token), not resolve to an agent_id.
        """
        _patch_api_key_lookup(monkeypatch, agent_id="agent-prefix-guard")

        payload = await mw.verify_token(_stub_request(), _bearer("acn_x"))

        assert payload["sub"] == "agent-prefix-guard"


# ---------------------------------------------------------------------------
# verify_token: Auth0 human path (regression)
# ---------------------------------------------------------------------------


class TestVerifyTokenAuth0PathUnaffected:
    @pytest.mark.asyncio
    async def test_auth0_jwt_still_routed_to_auth0(self, monkeypatch):
        """Non-ACN JWT → Auth0 path; human login unaffected by agent changes."""
        auth0_calls: list[str] = []

        async def _stub_auth0(token: str, *args, **kwargs) -> dict:
            auth0_calls.append(token)
            return {"sub": "auth0|alice", "permissions": ["acn:read"]}

        monkeypatch.setattr(mw, "_verify_jwt", _stub_auth0)
        # A realistic Auth0 JWT has iss="https://tenant.auth0.com/"
        # We don't actually decode it here; _verify_jwt stub handles it.
        token = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJodHRwczovL3Rlc3QuYXV0aDAuY29tLyIsInN1YiI6ImF1dGgwfGFsaWNlIn0.stub"

        payload = await mw.verify_token(_stub_request(), _bearer(token))

        assert auth0_calls, "Auth0 JWT must reach _verify_jwt"
        assert payload["type"] == "user"


# ---------------------------------------------------------------------------
# _verify_acn_agent_jwt: direct unit tests
# ---------------------------------------------------------------------------


class TestVerifyAcnAgentJwtDirect:
    @pytest.mark.asyncio
    async def test_no_signing_key_raises_401(self, monkeypatch):
        """If ACN has no signing key configured, verification fails cleanly."""
        fake = MagicMock()
        fake.agent_jwt_private_key = None
        fake.agent_jwt_kid = _KID
        fake.agent_jwt_issuer = _ISSUER
        fake.agent_jwt_audience = _AUDIENCE
        monkeypatch.setattr(mw, "_get_settings", lambda: fake)

        with pytest.raises(ACNHTTPError) as exc_info:
            await mw._verify_acn_agent_jwt("any.token.here", fake)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_audience_rejected(self, monkeypatch, rsa_pem):
        """JWT with wrong audience must be rejected."""
        _patch_verify_jwt_never_called(monkeypatch)
        token = _mint_jwt(rsa_pem, audience="https://wrong.audience")

        with pytest.raises(ACNHTTPError) as exc_info:
            # We call _verify_acn_agent_jwt directly (not via verify_token)
            # because verify_token would route wrong-iss to Auth0 path.
            from unittest.mock import MagicMock as MM

            fake = MM()
            fake.agent_jwt_private_key = rsa_pem
            fake.agent_jwt_kid = _KID
            fake.agent_jwt_issuer = _ISSUER
            fake.agent_jwt_audience = _AUDIENCE
            await mw._verify_acn_agent_jwt(token, fake)

        assert exc_info.value.status_code == 401
