"""Tests for the OAuth2 token + JWKS routes (ADR-0007).

Contract pinned here (route → issuer/service seam):

* **client_credentials** — a valid ``acn_*`` key (``client_secret``)
  resolves to its agent and mints a verifiable RS256 JWT whose ``sub``
  is that agent's id.
* **invalid_client** — an unknown key, or a ``client_id`` that does not
  match the credential's owner, fails 401 before a token is minted.
* **unsupported_grant_type** — only ``client_credentials`` is accepted.
* **JWKS** — the public key set is served and matches the signing kid.
* **temporarily_unavailable** — with no signing key the endpoint is 503.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jose import jwt

from acn.api import app
from acn.routes import oauth
from acn.routes.dependencies import get_agent_service, limiter
from acn.services.agent_token_service import AgentTokenIssuer


def _gen_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture(autouse=True)
def _reset_state():
    limiter.enabled = False
    yield
    limiter.enabled = True
    app.dependency_overrides.clear()


@pytest.fixture
def issuer() -> AgentTokenIssuer:
    return AgentTokenIssuer(
        private_key_pem=_gen_key_pem(),
        kid="acn-agent-key-1",
        issuer="https://acn.test",
        default_audience="https://api.test",
        ttl_seconds=3600,
        default_scope="acn:read acn:write store:sell",
    )


@pytest.fixture
def client(issuer, monkeypatch) -> TestClient:
    monkeypatch.setattr(oauth, "get_token_issuer", lambda: issuer)

    svc = AsyncMock()
    agent = MagicMock()
    agent.agent_id = "agent-xyz"

    async def _by_key(key: str):
        return agent if key == "acn_validkey" else None

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_key)
    app.dependency_overrides[get_agent_service] = lambda: svc
    return TestClient(app)


def _verify(token: str, issuer: AgentTokenIssuer) -> dict:
    key = issuer.jwks()["keys"][0]
    rsa_key = {k: key[k] for k in ("kty", "kid", "use", "n", "e")}
    return jwt.decode(
        token, rsa_key, algorithms=["RS256"],
        audience="https://api.test", issuer="https://acn.test",
    )


def test_token_success(client, issuer):
    r = client.post(
        "/oauth/token",
        json={"grant_type": "client_credentials", "client_secret": "acn_validkey"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "Bearer"
    payload = _verify(body["access_token"], issuer)
    assert payload["sub"] == "agent-xyz"
    assert payload["acn_principal"] == "agent"


def test_token_success_with_matching_client_id(client):
    r = client.post(
        "/oauth/token",
        json={
            "grant_type": "client_credentials",
            "client_id": "agent-xyz",
            "client_secret": "acn_validkey",
        },
    )
    assert r.status_code == 200


def test_token_invalid_key(client):
    r = client.post(
        "/oauth/token",
        json={"grant_type": "client_credentials", "client_secret": "acn_wrong"},
    )
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"


def test_token_client_id_mismatch(client):
    r = client.post(
        "/oauth/token",
        json={
            "grant_type": "client_credentials",
            "client_id": "someone-else",
            "client_secret": "acn_validkey",
        },
    )
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"


def test_token_missing_secret(client):
    r = client.post("/oauth/token", json={"grant_type": "client_credentials"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_unsupported_grant_type(client):
    r = client.post(
        "/oauth/token",
        json={"grant_type": "password", "client_secret": "acn_validkey"},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_grant_type"


def test_basic_auth_credentials(client):
    import base64

    basic = base64.b64encode(b"agent-xyz:acn_validkey").decode()
    r = client.post(
        "/oauth/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {basic}"},
    )
    assert r.status_code == 200


def test_jwks_endpoint(client, issuer):
    r = client.get("/.well-known/jwks.json")
    assert r.status_code == 200
    keys = r.json()["keys"]
    assert len(keys) == 1
    assert keys[0]["kid"] == "acn-agent-key-1"


def test_disabled_issuer_returns_503(client, monkeypatch):
    disabled = AgentTokenIssuer(
        private_key_pem=None,
        kid="acn-agent-key-1",
        issuer="https://acn.test",
        default_audience="https://api.test",
        ttl_seconds=3600,
        default_scope="acn:read",
    )
    monkeypatch.setattr(oauth, "get_token_issuer", lambda: disabled)
    r = client.post(
        "/oauth/token",
        json={"grant_type": "client_credentials", "client_secret": "acn_validkey"},
    )
    assert r.status_code == 503
    assert r.json()["error"] == "temporarily_unavailable"
