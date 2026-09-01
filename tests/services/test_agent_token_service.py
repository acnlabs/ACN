"""Unit tests for ``AgentTokenIssuer`` (ADR-0007).

Pure (no app wiring): generate a keypair, mint, verify the round-trip,
and pin the disabled / scope / JWKS-shape invariants.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt
from jose.exceptions import JWTError

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


def test_mint_runtime_command_is_not_an_agent_token():
    iss = _issuer()
    patch = {"preferred_model": "minimax/minimax-m2.5"}
    token = iss.mint_runtime_command("agent-123", patch)
    claims = jwt.get_unverified_claims(token)
    assert claims["sub"] == "acn"
    assert claims["aud"] == "agent-123"
    assert claims["acn_action"] == "runtime"
    assert claims["acn_principal"] == "host"
    assert claims["runtime"] == patch
    assert claims["exp"] - claims["iat"] == 60
    roundtrip = iss.verify_runtime_command(token, agent_id="agent-123", patch=patch)
    assert roundtrip["sub"] == "acn"


def test_agent_jwt_cannot_verify_as_runtime_command():
    iss = _issuer()
    agent_tok = iss.mint("agent-123")["access_token"]
    with pytest.raises(JWTError):
        iss.verify_runtime_command(
            agent_tok,
            agent_id="agent-123",
            patch={"preferred_model": "x"},
        )


def test_runtime_jwt_body_mismatch_is_rejected():
    iss = _issuer()
    token = iss.mint_runtime_command("agent-123", {"preferred_model": "a/b"})
    with pytest.raises(ValueError, match="runtime_jwt_body_mismatch"):
        iss.verify_runtime_command(
            token,
            agent_id="agent-123",
            patch={"preferred_model": "c/d"},
        )
    iss = _issuer(private_key_pem=None)
    assert iss.enabled is False
    assert iss.jwks() == {"keys": []}
    with pytest.raises(RuntimeError):
        iss.mint("a")


def test_invalid_private_key_disables_issuer():
    iss = _issuer(private_key_pem="-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----")
    assert iss.enabled is False


# --- key rotation: overlapping kids (#154) ------------------------------------


def _jwk_by_kid(jwks: dict, kid: str) -> dict:
    key = next(k for k in jwks["keys"] if k["kid"] == kid)
    return {"kty": key["kty"], "kid": key["kid"], "use": key["use"], "n": key["n"], "e": key["e"]}


def test_secondary_key_published_in_jwks_but_not_used_to_mint():
    primary = _gen_key_pem()
    secondary = _gen_key_pem()
    iss = _issuer(
        private_key_pem=primary,
        kid="kid-new",
        secondary_private_key_pem=secondary,
        secondary_kid="kid-old",
    )
    kids = {k["kid"] for k in iss.jwks()["keys"]}
    assert kids == {"kid-new", "kid-old"}
    # mint must use only the primary kid
    tok = iss.mint("agent-1")
    assert jwt.get_unverified_header(tok["access_token"])["kid"] == "kid-new"


def test_old_token_still_verifies_after_promoting_new_primary():
    """Simulate step 2 of rotation: old key demoted to secondary, old token still valid."""
    old = _gen_key_pem()
    new = _gen_key_pem()
    # Before rotation: old is primary.
    before = _issuer(private_key_pem=old, kid="kid-old")
    old_token = before.mint("agent-1")["access_token"]
    # After promotion: new is primary, old is secondary (verification-only).
    after = _issuer(
        private_key_pem=new,
        kid="kid-new",
        secondary_private_key_pem=old,
        secondary_kid="kid-old",
    )
    payload = jwt.decode(
        old_token,
        _jwk_by_kid(after.jwks(), "kid-old"),
        algorithms=["RS256"],
        audience="https://api.test",
        issuer="https://acn.test",
    )
    assert payload["sub"] == "agent-1"


def test_secondary_kid_collision_is_ignored():
    iss = _issuer(
        kid="same-kid",
        secondary_private_key_pem=_gen_key_pem(),
        secondary_kid="same-kid",
    )
    # collision → secondary dropped, only the primary remains
    assert len(iss.jwks()["keys"]) == 1
    assert iss.jwks()["keys"][0]["kid"] == "same-kid"


def test_invalid_secondary_key_does_not_disable_primary():
    iss = _issuer(
        kid="kid-new",
        secondary_private_key_pem="-----BEGIN PRIVATE KEY-----\nbad\n-----END PRIVATE KEY-----",
        secondary_kid="kid-old",
    )
    assert iss.enabled is True
    assert [k["kid"] for k in iss.jwks()["keys"]] == ["kid-new"]
