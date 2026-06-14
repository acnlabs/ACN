"""Pluggable human OIDC issuer registry (``HUMAN_OIDC_PROVIDERS_JSON``).

ACN's human-JWT path historically pinned a single Auth0 issuer. The registry
lets a region (e.g. China) verify human users against a domestic, self-issued
OIDC IdP routed by the token's ``iss`` claim — while Auth0 keeps its dedicated,
unchanged code path.

These tests sign real RS256 tokens with an ephemeral keypair and verify them
through ``_verify_jwt`` (the registry fast path) without any network: the
provider is configured with a *pre-seeded* JWKS so ``_get_provider_jwks``
returns it directly.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from acn.auth import middleware as mw
from acn.core.errors import ACNHTTPError
from acn.services.agent_token_service import AgentTokenIssuer

_ISS = "https://api.acnlabs.cn/u"
_AUD = "https://api.acnlabs.cn"
_KID = "cn-user-key-1"


def _make_keypair() -> str:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


_PRIV_PEM = _make_keypair()
_JWKS = {"keys": [AgentTokenIssuer._derive_jwk(_PRIV_PEM, _KID)]}


def _providers_json(**overrides) -> str:
    entry = {
        "name": "cn-wechat",
        "issuer": _ISS,
        "audience": _AUD,
        "jwks": json.dumps(_JWKS),
        "sub_prefix": "wechat|",
    }
    entry.update(overrides)
    return json.dumps([entry])


def _sign(
    *,
    sub: str = "wechat|openid-abc",
    iss: str = _ISS,
    aud: str = _AUD,
    kid: str = _KID,
    exp_offset: int = 300,
    key_pem: str = _PRIV_PEM,
) -> str:
    now = int(time.time())
    claims = {"iss": iss, "sub": sub, "aud": aud, "iat": now, "nbf": now, "exp": now + exp_offset}
    return jwt.encode(claims, key_pem, algorithm="RS256", headers={"kid": kid})


@pytest.fixture(autouse=True)
def _clear_caches():
    mw._human_providers_cache.clear()
    mw._oidc_jwks_caches.clear()
    mw._oidc_jwks_locks.clear()
    yield
    mw._human_providers_cache.clear()
    mw._oidc_jwks_caches.clear()
    mw._oidc_jwks_locks.clear()


@pytest.fixture
def _settings(monkeypatch):
    s = MagicMock()
    s.dev_mode = False
    # Auth0 remains configured (global default) — proves the registry is purely
    # additive and does not require dropping Auth0.
    s.auth0_domain = "https://tenant.auth0.com"
    s.auth0_audience = "https://api.agentplanet.org"
    s.human_oidc_providers_json = _providers_json()
    monkeypatch.setattr(mw, "_get_settings", lambda: s)
    return s


@pytest.mark.asyncio
async def test_registry_provider_token_verified_as_user(_settings):
    payload = await mw._verify_jwt(_sign(), request=MagicMock())
    assert payload["sub"] == "wechat|openid-abc"
    assert payload["type"] == "user"


@pytest.mark.asyncio
async def test_wrong_audience_rejected(_settings):
    with pytest.raises(ACNHTTPError) as exc:
        await mw._verify_jwt(_sign(aud="https://evil"), request=MagicMock())
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_expired_token_rejected(_settings):
    with pytest.raises(ACNHTTPError) as exc:
        await mw._verify_jwt(_sign(exp_offset=-10), request=MagicMock())
    assert exc.value.status_code == 401
    assert "expired" in exc.value.message.lower()


@pytest.mark.asyncio
async def test_unknown_kid_rejected(_settings):
    with pytest.raises(ACNHTTPError) as exc:
        await mw._verify_jwt(_sign(kid="no-such-kid"), request=MagicMock())
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_sub_prefix_mismatch_rejected(_settings):
    """A registry provider pinned to ``wechat|`` must reject a token whose sub
    pretends to be in another provider's namespace (e.g. ``auth0|``)."""
    with pytest.raises(ACNHTTPError) as exc:
        await mw._verify_jwt(_sign(sub="auth0|spoofed"), request=MagicMock())
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_foreign_key_signature_rejected(_settings):
    """Token claiming the registry issuer/kid but signed by a different key
    (forgery) must fail signature verification."""
    other_pem = _make_keypair()
    with pytest.raises(ACNHTTPError) as exc:
        await mw._verify_jwt(_sign(key_pem=other_pem), request=MagicMock())
    assert exc.value.status_code == 401


def test_resolver_matches_issuer_trailing_slash_tolerant(_settings):
    assert mw._resolve_human_provider(_settings, _ISS) is not None
    assert mw._resolve_human_provider(_settings, _ISS + "/") is not None
    assert mw._resolve_human_provider(_settings, "https://api.agentplanet.org") is None
    assert mw._resolve_human_provider(_settings, None) is None


def test_malformed_registry_json_does_not_crash():
    assert mw._parse_human_providers("not json{") == []
    assert mw._parse_human_providers(None) == []
    # one bad entry skipped, one good entry kept
    raw = json.dumps(
        [
            {"name": "bad-missing-aud", "issuer": "https://x"},
            {"name": "ok", "issuer": "https://y", "audience": "a", "jwks_url": "https://y/jwks"},
        ]
    )
    parsed = mw._parse_human_providers(raw)
    assert [p.name for p in parsed] == ["ok"]
