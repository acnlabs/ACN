"""Unit tests for ``AgentTokenIssuer`` (ADR-0007).

Pure (no app wiring): generate a keypair, mint, verify the round-trip,
and pin the disabled / scope / JWKS-shape invariants.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from acn.services.agent_token_service import AgentTokenIssuer


def _gen_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _issuer(**overrides) -> AgentTokenIssuer:
    params = {
        "private_key_pem": _gen_key_pem(),
        "kid": "acn-agent-key-1",
        "issuer": "https://acn.test",
        "default_audience": "https://api.test",
        "ttl_seconds": 3600,
        "default_scope": "acn:read acn:write store:sell",
    }
    params.update(overrides)
    return AgentTokenIssuer(**params)


def _verify(token: str, jwks: dict, *, audience="https://api.test", issuer="https://acn.test"):
    key = jwks["keys"][0]
    rsa_key = {
        "kty": key["kty"],
        "kid": key["kid"],
        "use": key["use"],
        "n": key["n"],
        "e": key["e"],
    }
    return jwt.decode(token, rsa_key, algorithms=["RS256"], audience=audience, issuer=issuer)


def test_mint_and_verify_roundtrip():
    iss = _issuer()
    assert iss.enabled
    tok = iss.mint("agent-123")
    assert tok["token_type"] == "Bearer"
    assert tok["expires_in"] == 3600
    payload = _verify(tok["access_token"], iss.jwks())
    assert payload["sub"] == "agent-123"
    assert payload["iss"] == "https://acn.test"
    assert payload["aud"] == "https://api.test"
    assert payload["acn_principal"] == "agent"
    assert payload["scope"] == "acn:read acn:write store:sell"


def test_default_scope_excludes_wallet_write():
    # ADR-0007 D3: money-movement-for-others is not granted by default.
    assert "wallet:write" not in _issuer().mint("a")["scope"]


def test_custom_audience_is_honoured():
    tok = _issuer().mint("a", audience="https://other.example")
    assert jwt.get_unverified_claims(tok["access_token"])["aud"] == "https://other.example"


def test_jwks_shape():
    key = _issuer().jwks()["keys"][0]
    assert key["kty"] == "RSA"
    assert key["alg"] == "RS256"
    assert key["use"] == "sig"
    assert key["kid"] == "acn-agent-key-1"
    assert key["n"] and key["e"]


def test_disabled_without_key():
    iss = _issuer(private_key_pem=None)
    assert iss.enabled is False
    assert iss.jwks() == {"keys": []}
    with pytest.raises(RuntimeError):
        iss.mint("a")


def test_invalid_private_key_disables_issuer():
    iss = _issuer(private_key_pem="-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----")
    assert iss.enabled is False
