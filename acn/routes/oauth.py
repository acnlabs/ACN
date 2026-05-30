"""OAuth2 token + OIDC discovery routes (ADR-0007).

ACN issues short-lived agent JWTs here. An agent exchanges its
long-lived ``acn_*`` API key (the client credential) for a signed RS256
JWT via the standard OAuth2 ``client_credentials`` grant; resource
servers then verify that JWT offline against the JWKS published below.

Endpoints
=========
- ``POST /oauth/token`` — client_credentials grant. Accepts the
  credential as form or JSON body (``client_secret``) or HTTP Basic.
  Mirrors the request shape of an Auth0 ``/oauth/token`` call so a
  migrating seller (e.g. AgentMother) only swaps the endpoint URL and
  the credential, not the flow.
- ``GET /.well-known/jwks.json`` — public verification keys.
- ``GET /.well-known/openid-configuration`` — minimal OIDC discovery so
  standards-based verifiers can self-configure.
"""

from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from typing import Any

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..config import get_settings
from ..services import AgentService
from ..services.agent_token_service import AgentTokenIssuer
from .dependencies import get_agent_service, limiter

logger = structlog.get_logger()

router = APIRouter(tags=["oauth"])


@lru_cache
def get_token_issuer() -> AgentTokenIssuer:
    """Build (and cache) the process-wide agent token issuer from settings."""
    s = get_settings()
    issuer = s.agent_jwt_issuer or s.gateway_base_url
    return AgentTokenIssuer(
        private_key_pem=s.agent_jwt_private_key,
        kid=s.agent_jwt_kid,
        issuer=issuer,
        default_audience=s.agent_jwt_audience,
        ttl_seconds=s.agent_jwt_ttl_seconds,
        default_scope=s.agent_jwt_default_scope,
    )


def _oauth_error(error: str, description: str, status_code: int = 400) -> JSONResponse:
    """RFC 6749 §5.2 error response."""
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "error_description": description},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _basic_auth_secret(request: Request) -> tuple[str | None, str | None]:
    """Extract (client_id, client_secret) from an HTTP Basic header, if present."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return None, None
    try:
        raw = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None, None
    if ":" not in raw:
        return None, None
    cid, secret = raw.split(":", 1)
    return (cid or None), (secret or None)


async def _parse_token_request(request: Request) -> dict[str, Any]:
    """Read token-request params from JSON or form body (content-type aware)."""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    try:
        form = await request.form()
        return dict(form)
    except Exception:  # noqa: BLE001
        return {}


@router.post("/oauth/token")
@limiter.limit("60/minute")
async def issue_token(
    request: Request,
    agent_service: AgentService = Depends(get_agent_service),
) -> JSONResponse:
    """OAuth2 ``client_credentials`` grant: ``acn_*`` key → short-lived JWT."""
    issuer = get_token_issuer()
    if not issuer.enabled:
        return _oauth_error(
            "temporarily_unavailable",
            "Token issuance is not configured on this ACN deployment.",
            status_code=503,
        )

    body = await _parse_token_request(request)
    basic_id, basic_secret = _basic_auth_secret(request)

    grant_type = body.get("grant_type")
    if grant_type != "client_credentials":
        return _oauth_error(
            "unsupported_grant_type",
            "Only 'client_credentials' is supported.",
        )

    client_id = body.get("client_id") or basic_id
    client_secret = body.get("client_secret") or basic_secret
    if not client_secret:
        return _oauth_error(
            "invalid_request",
            "Missing client_secret (your acn_* API key).",
        )

    agent = await agent_service.get_agent_by_api_key(client_secret)
    if agent is None:
        return _oauth_error(
            "invalid_client",
            "API key did not resolve to a registered agent.",
            status_code=401,
        )

    # If the caller asserted a client_id, it must equal their agent_id —
    # prevents minting a token under someone else's identity.
    if client_id and client_id != agent.agent_id:
        return _oauth_error(
            "invalid_client",
            "client_id does not match the agent that owns this credential.",
            status_code=401,
        )

    # ``audience`` is honoured if provided (so an Auth0-style call with
    # the same audience keeps working); scope escalation is NOT honoured —
    # the issuer always grants the configured default scope set (ADR-0007
    # D3). Per-agent capability grants are a separate, later feature.
    audience = body.get("audience") or None
    tok = issuer.mint(agent.agent_id, audience=audience)

    logger.info("agent_jwt_issued", agent_id=agent.agent_id, scope=tok["scope"])
    return JSONResponse(
        content=tok,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.get("/.well-known/jwks.json")
async def jwks() -> JSONResponse:
    """Public JSON Web Key Set for verifying ACN-issued agent JWTs."""
    issuer = get_token_issuer()
    return JSONResponse(content=issuer.jwks())


@router.get("/.well-known/openid-configuration")
async def openid_configuration() -> JSONResponse:
    """Minimal OIDC discovery document for standards-based verifiers."""
    issuer = get_token_issuer()
    base = issuer.issuer
    return JSONResponse(
        content={
            "issuer": base,
            "jwks_uri": f"{base}/.well-known/jwks.json",
            "token_endpoint": f"{base}/oauth/token",
            "grant_types_supported": ["client_credentials"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "client_secret_basic",
            ],
            "id_token_signing_alg_values_supported": ["RS256"],
            "response_types_supported": ["token"],
        }
    )
