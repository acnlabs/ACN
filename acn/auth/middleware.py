"""
ACN Auth0 + ACN-agent-JWT Middleware

Standalone JWT verification using python-jose.
Does NOT rely on Backend's auth module or sys.path manipulation.

In production (dev_mode=False), Auth0 configuration is required for human
callers. ACN-issued agent JWTs are verified offline against ACN's own
signing key — no Auth0 dependency for agent-to-ACN traffic.

In development (dev_mode=True), a stub is used for convenience.

JWKS are cached in-memory with a configurable TTL (default 600s) to avoid
hitting Auth0's endpoint on every request.

Protocol dispatch (verify_token)
---------------------------------
1. ``acn_*`` prefix → opaque API-key path (Redis/PG lookup), legacy + mint.
2. Unverified ``iss`` == ACN issuer → ACN-issued RS256 agent JWT path (offline).
3. Everything else → Auth0 human JWT path (network JWKS).

See ADR-0007 D6 (issue #156) for the rationale.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from typing import Any

import httpx
import structlog
from fastapi import Header, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError

from ..core.errors import ACNHTTPError, ErrorCode
from ..monitoring import record_auth_failure

logger = structlog.get_logger()

_bearer_scheme = HTTPBearer(auto_error=False)


def _peer_ip(request: Request | None) -> str | None:
    """Best-effort source IP for audit context.

    Intentionally naive — uses the direct TCP peer rather than
    ``X-Forwarded-For``. The proxy-aware variant lives in
    ``routes.dependencies`` (gated by ``trusted_proxies``) but
    pulling that here would create a routes -> auth import cycle.
    Audit IPs are diagnostic only, never used for security decisions,
    so the simpler accessor is acceptable.
    """
    if request is None:
        return None
    try:
        client = request.client
        return client.host if client else None
    except Exception:  # noqa: BLE001
        return None


def _request_path(request: Request | None) -> tuple[str | None, str | None]:
    """Return ``(path, method)`` from ``request`` defensively."""
    if request is None:
        return None, None
    try:
        return request.url.path, request.method
    except Exception:  # noqa: BLE001
        return None, None

# ---------------------------------------------------------------------------
# JWKS in-memory cache  (avoids a remote HTTP call on every request)
# ---------------------------------------------------------------------------

_JWKS_CACHE_TTL = 600  # seconds

_jwks_cache: dict[str, Any] = {
    "keys": None,
    "domain": None,
    "fetched_at": 0.0,
}

# Prevent concurrent JWKS refreshes (thundering herd) when cache expires
_jwks_lock = asyncio.Lock()


def _get_settings():
    from ..config import get_settings

    return get_settings()


async def _resolve_agent_id_from_api_key(token: str | None) -> str | None:
    """Best-effort: turn an ``acn_*`` API key into its owning agent's UUID.

    Used by the ``dev_mode`` short-circuits below so that downstream ACLs
    (which compare against agent UUIDs — subnet membership, task ownership,
    ...) keep working when callers authenticate with a raw API key instead
    of a JWT. Without this resolver, ``sub`` ends up being the literal
    ``acn_…`` string and every UUID-keyed permission check 403s.

    Lazy-imports the agent service to avoid a routes -> auth circular
    import (see file header for the same constraint on ``_peer_ip``).
    Returns ``None`` on any failure — the caller falls back to the
    pre-existing dev_mode behaviour (raw token as ``sub``).
    """
    if not token or not token.startswith("acn_"):
        return None
    try:
        from ..routes.dependencies import get_agent_service

        agent = await get_agent_service().get_agent_by_api_key(token)
        return agent.agent_id if agent else None
    except Exception:  # noqa: BLE001
        return None


def _load_jwks_from_env() -> dict | None:
    """Load JWKS from AUTH0_JWKS environment variable if present.

    This is the primary mechanism for environments where outbound DNS is
    unreliable (e.g. Railway containers). The env var contains the raw JSON
    string from https://<domain>/.well-known/jwks.json.
    """
    raw = os.environ.get("AUTH0_JWKS")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        logger.warning("auth0_jwks_env_parse_failed")
        return None


async def _get_jwks(domain: str) -> dict:
    """Return JWKS for *domain*, refreshing the cache when stale.

    Priority:
    1. In-memory cache (if valid and not expired)
    2. AUTH0_JWKS environment variable (pre-seeded, no network needed)
    3. Network fetch from Auth0's JWKS endpoint

    Uses an asyncio.Lock to prevent thundering herd on cache expiry.
    """
    now = time.monotonic()
    if (
        _jwks_cache["keys"] is not None
        and _jwks_cache["domain"] == domain
        and now - _jwks_cache["fetched_at"] < _JWKS_CACHE_TTL
    ):
        return _jwks_cache["keys"]

    async with _jwks_lock:
        # Re-check inside the lock: another coroutine may have refreshed already
        now = time.monotonic()
        if (
            _jwks_cache["keys"] is not None
            and _jwks_cache["domain"] == domain
            and now - _jwks_cache["fetched_at"] < _JWKS_CACHE_TTL
        ):
            return _jwks_cache["keys"]

        # Try AUTH0_JWKS env var first (avoids network call entirely)
        jwks = _load_jwks_from_env()
        if jwks:
            _jwks_cache["keys"] = jwks
            _jwks_cache["domain"] = domain
            # Set fetched_at far in the past so network refresh is attempted
            # on the next expiry cycle when the network may be available
            _jwks_cache["fetched_at"] = now
            logger.info("jwks_loaded_from_env", domain=domain)
            # Background: attempt a network refresh without blocking the request
            asyncio.create_task(_refresh_jwks_from_network(domain))
            return jwks

        # No env var — fetch from network
        jwks = await _fetch_jwks_from_network(domain)
        _jwks_cache["keys"] = jwks
        _jwks_cache["domain"] = domain
        _jwks_cache["fetched_at"] = now
        logger.info("jwks_cache_refreshed", domain=domain)
        return jwks


async def _fetch_jwks_from_network(domain: str) -> dict:
    """Fetch JWKS from Auth0's well-known endpoint."""
    # domain may already include the scheme (e.g. "https://tenant.auth0.com")
    bare = domain.removeprefix("https://").removeprefix("http://").rstrip("/")
    jwks_url = f"https://{bare}/.well-known/jwks.json"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(jwks_url)
        resp.raise_for_status()
        return resp.json()


async def _refresh_jwks_from_network(domain: str) -> None:
    """Background task: silently refresh JWKS cache from network if available.

    Does not raise — failure is expected when network is unavailable.
    """
    try:
        jwks = await _fetch_jwks_from_network(domain)
        async with _jwks_lock:
            _jwks_cache["keys"] = jwks
            _jwks_cache["domain"] = domain
            _jwks_cache["fetched_at"] = time.monotonic()
        logger.info("jwks_background_refresh_succeeded", domain=domain)
    except Exception as e:
        logger.debug("jwks_background_refresh_skipped", domain=domain, error=str(e))


# ---------------------------------------------------------------------------
# ACN self-issued agent JWT verification (ADR-0007 D6, issue #156)
# ---------------------------------------------------------------------------
# ACN publishes its own JWKS at ``/.well-known/jwks.json``. For self-
# verification we derive the public key directly from the private key
# already held in settings — no network call needed. The derived key list
# is cached module-level (process lifetime) and rebuilt only on cold start.
# ---------------------------------------------------------------------------

_acn_agent_jwks: list[dict] = []
_acn_agent_jwks_lock = asyncio.Lock()
_acn_agent_jwks_loaded = False


async def _get_acn_agent_jwks(settings) -> list[dict]:
    """Return ACN's own public JWK list (derived offline from the signing key)."""
    global _acn_agent_jwks, _acn_agent_jwks_loaded
    if _acn_agent_jwks_loaded:
        return _acn_agent_jwks
    async with _acn_agent_jwks_lock:
        if _acn_agent_jwks_loaded:
            return _acn_agent_jwks
        if not settings.agent_jwt_private_key:
            _acn_agent_jwks_loaded = True
            return _acn_agent_jwks
        from ..services.agent_token_service import AgentTokenIssuer

        keys: list[dict] = []
        # Primary signing key.
        try:
            keys.append(
                AgentTokenIssuer._derive_jwk(
                    settings.agent_jwt_private_key.strip(),
                    settings.agent_jwt_kid,
                )
            )
            logger.info("acn_agent_jwks_derived", kid=settings.agent_jwt_kid)
        except Exception as exc:  # noqa: BLE001
            logger.error("acn_agent_jwks_derive_failed", error=str(exc))
        # Secondary verification-only key (rotation window, #154).
        secondary = getattr(settings, "agent_jwt_private_key_secondary", None)
        secondary_kid = getattr(settings, "agent_jwt_kid_secondary", None)
        if secondary and secondary_kid and secondary_kid != settings.agent_jwt_kid:
            try:
                keys.append(AgentTokenIssuer._derive_jwk(secondary.strip(), secondary_kid))
                logger.info("acn_agent_jwks_secondary_derived", kid=secondary_kid)
            except Exception as exc:  # noqa: BLE001
                logger.error("acn_agent_jwks_secondary_derive_failed", error=str(exc))
        _acn_agent_jwks = keys
        _acn_agent_jwks_loaded = True
        return _acn_agent_jwks


def _get_acn_effective_issuer(settings) -> str | None:
    """Return the effective ACN agent-JWT issuer (the ``iss`` claim value).

    Mirrors ``AgentTokenIssuer.__init__`` which falls back to
    ``gateway_base_url`` when ``agent_jwt_issuer`` is unset.
    """
    iss = settings.agent_jwt_issuer
    if iss:
        return iss.rstrip("/")
    gw = getattr(settings, "gateway_base_url", None)
    return gw.rstrip("/") if gw else None


async def _verify_acn_agent_jwt(
    token: str,
    settings,
    request: Request | None = None,
) -> dict:
    """Verify an ACN-issued RS256 agent JWT and return a normalised payload.

    On success returns ``{"sub": agent_id, "type": "agent", "permissions": [...],
    "acn_principal": "agent"}`` so downstream ACLs can branch on the same
    schema as every other auth path.

    Raises ``ACNHTTPError(401)`` on any verification failure. The caller is
    responsible for routing only tokens whose unverified ``iss`` matches the
    ACN issuer; this function does not re-check routing, it only verifies.
    """
    src_ip = _peer_ip(request)
    path, method = _request_path(request)

    issuer = _get_acn_effective_issuer(settings)
    if not issuer:
        raise ACNHTTPError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            401,
            message="ACN agent JWT issuer not configured.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    jwks = await _get_acn_agent_jwks(settings)
    if not jwks:
        raise ACNHTTPError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            401,
            message="ACN agent JWT signing key not configured.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        # Strict kid match. The old behaviour fell back to jwks[0] on a
        # mismatch, which during a rotation window could verify a token
        # against the wrong key's slot; with overlapping kids now properly
        # published (#154) we require an exact match and reject otherwise.
        rsa_key = next((k for k in jwks if k.get("kid") == kid), None)
        if rsa_key is None:
            logger.warning("acn_agent_jwt_unknown_kid", kid=kid)
            record_auth_failure(
                reason="acn_agent_jwt_unknown_kid",
                source_ip=src_ip,
                path=path,
                method=method,
                extra={"kid": str(kid)},
            )
            raise ACNHTTPError(
                ErrorCode.AUTHENTICATION_REQUIRED,
                401,
                message="Unknown token signing key.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        payload = jwt.decode(
            token,
            rsa_key,
            algorithms=["RS256"],
            audience=settings.agent_jwt_audience,
            issuer=issuer,
        )
    except ExpiredSignatureError:
        record_auth_failure(
            reason="acn_agent_jwt_expired",
            source_ip=src_ip,
            path=path,
            method=method,
        )
        raise ACNHTTPError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            401,
            message="Token has expired.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except JWTError as exc:
        logger.warning("acn_agent_jwt_verification_failed", error=str(exc))
        record_auth_failure(
            reason="acn_agent_jwt_invalid",
            source_ip=src_ip,
            path=path,
            method=method,
            extra={"jwt_error": type(exc).__name__},
        )
        raise ACNHTTPError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            401,
            message="Invalid token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    agent_id = payload.get("sub")
    if not agent_id:
        raise ACNHTTPError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            401,
            message="ACN agent JWT missing sub claim.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    logger.debug("acn_agent_jwt_verified", agent_id=agent_id)
    return {
        "sub": agent_id,
        "type": "agent",
        # Agent JWTs don't carry acn:admin — same restriction as API keys.
        "permissions": ["acn:read", "acn:write"],
        "acn_principal": "agent",
    }


# ---------------------------------------------------------------------------
# Core JWT verification
# ---------------------------------------------------------------------------


async def _verify_jwt(token: str, request: Request | None = None) -> dict:
    """Verify an Auth0 JWT and return its payload."""
    settings = _get_settings()
    src_ip = _peer_ip(request)
    path, method = _request_path(request)

    if not settings.auth0_domain or not settings.auth0_audience:
        if settings.dev_mode:
            logger.warning(
                "auth0_not_configured_dev_mode",
                message="Auth0 not configured, using dev stub",
            )
            return {"sub": "dev@clients", "permissions": []}
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth0 is not configured. Set AUTH0_DOMAIN and AUTH0_AUDIENCE.",
        )

    try:
        jwks = await _get_jwks(settings.auth0_domain)

        unverified_header = jwt.get_unverified_header(token)
        rsa_key = {}
        for key in jwks.get("keys", []):
            if key.get("kid") == unverified_header.get("kid"):
                rsa_key = {
                    "kty": key["kty"],
                    "kid": key["kid"],
                    "use": key["use"],
                    "n": key["n"],
                    "e": key["e"],
                }
                break

        if not rsa_key:
            # Key not found — cache may be stale; invalidate and retry once
            async with _jwks_lock:
                _jwks_cache["keys"] = None
            jwks = await _get_jwks(settings.auth0_domain)
            for key in jwks.get("keys", []):
                if key.get("kid") == unverified_header.get("kid"):
                    rsa_key = {
                        "kty": key["kty"],
                        "kid": key["kid"],
                        "use": key["use"],
                        "n": key["n"],
                        "e": key["e"],
                    }
                    break

        if not rsa_key:
            record_auth_failure(
                reason="jwt_signing_key_not_found",
                source_ip=src_ip,
                path=path,
                method=method,
            )
            raise ACNHTTPError(
                ErrorCode.AUTHENTICATION_REQUIRED,
                401,
                message="Unable to find appropriate signing key.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        bare_domain = settings.auth0_domain.removeprefix("https://").removeprefix("http://").rstrip("/")
        payload = jwt.decode(
            token,
            rsa_key,
            algorithms=["RS256"],
            audience=settings.auth0_audience,
            issuer=f"https://{bare_domain}/",
        )
        return payload

    # ExpiredSignatureError must be caught before JWTError (it's a subclass)
    except ExpiredSignatureError:
        record_auth_failure(
            reason="jwt_expired",
            source_ip=src_ip,
            path=path,
            method=method,
        )
        raise ACNHTTPError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            401,
            message="Token has expired.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except JWTError as e:
        logger.warning("jwt_verification_failed", error=str(e))
        record_auth_failure(
            reason="jwt_invalid",
            source_ip=src_ip,
            path=path,
            method=method,
            extra={"jwt_error": type(e).__name__},
        )
        raise ACNHTTPError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            401,
            message="Invalid token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
    except ACNHTTPError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error("auth_unexpected_error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication service error.",
        ) from e


# ---------------------------------------------------------------------------
# FastAPI dependency functions (public API, matches previous interface)
# ---------------------------------------------------------------------------


async def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> dict:
    """FastAPI dependency: verify Bearer token and return a payload.

    Production traffic is dual-protocol:

    - ``Authorization: Bearer <jwt>`` — auth0-issued user JWT, verified
      against the JWKS, returns ``{"sub": user_sub, "type": "user", ...}``.
    - ``Authorization: Bearer acn_<api_key>`` — agent API key, resolved
      to the owning agent's UUID, returns
      ``{"sub": agent_id, "type": "agent", "permissions": ["acn:read", "acn:write"]}``.

    The ``acn_`` prefix is the protocol discriminator: prefix-dispatch
    keeps API-key traffic from polluting JWT failure audit logs and
    avoids paying a Redis lookup on every JWT request. API-key callers
    do **not** receive ``acn:admin`` — that permission is auth0 / user-
    domain only by design (issue #114 §3.2 admin row).

    See ADR-0006 (issue #114) for the V6 ACL contract this enables.
    """
    settings = _get_settings()

    if settings.dev_mode:
        # In dev mode: accept any credential (API keys, stub tokens, etc.)
        # as a convenience shortcut — no Auth0 verification. If the bearer
        # is an actual ``acn_*`` agent API key, resolve it to that agent's
        # UUID so downstream ACLs (subnet membership, task ownership, ...)
        # keep working; otherwise fall back to the raw token string.
        resolved = await _resolve_agent_id_from_api_key(
            credentials.credentials if credentials is not None else None
        )
        if resolved is not None:
            return {
                "sub": resolved,
                "type": "agent",
                "permissions": ["acn:read", "acn:write", "acn:admin"],
            }
        sub = credentials.credentials if credentials is not None else "dev@clients"
        return {
            "sub": sub,
            "type": "user",
            "permissions": ["acn:read", "acn:write", "acn:admin"],
        }

    if credentials is None:
        path, method = _request_path(request)
        record_auth_failure(
            reason="bearer_missing",
            source_ip=_peer_ip(request),
            path=path,
            method=method,
        )
        raise ACNHTTPError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            401,
            message="Authorization header required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # Production triple-protocol dispatch:
    #   1. ``acn_*`` prefix       → opaque API-key (Redis/PG lookup)
    #   2. iss == ACN issuer      → ACN-issued RS256 agent JWT (offline verify)
    #   3. everything else        → Auth0 human JWT
    #
    # Prefix / iss routing (rather than try-and-fallback) keeps each
    # protocol's traffic in its own failure-audit bucket, making on-call
    # alert queries precise. See ADR-0007 D6, issue #156.
    if token.startswith("acn_"):
        resolved = await _resolve_agent_id_from_api_key(token)
        if resolved is not None:
            return {
                "sub": resolved,
                "type": "agent",
                # API key callers never receive ``acn:admin``. Admin is
                # an auth0 / user-domain permission (ops, SRE) that
                # transcends the marketplace. Agents requiring elevated
                # ops access must go through a human admin's JWT.
                "permissions": ["acn:read", "acn:write"],
            }
        path, method = _request_path(request)
        record_auth_failure(
            reason="api_key_invalid",
            source_ip=_peer_ip(request),
            path=path,
            method=method,
        )
        raise ACNHTTPError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            401,
            message="Invalid API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ACN-issued agent JWT: peek at the unverified ``iss`` claim (fast,
    # no crypto yet). If it matches ACN's own issuer, verify offline
    # against ACN's signing key — no Auth0 dependency for agent callers.
    try:
        _unverified_claims = jwt.get_unverified_claims(token)
        _token_iss = (_unverified_claims.get("iss") or "").rstrip("/")
        _acn_iss = _get_acn_effective_issuer(settings) or ""
        if _token_iss and _acn_iss and _token_iss == _acn_iss:
            return await _verify_acn_agent_jwt(token, settings, request=request)
    except (ACNHTTPError, HTTPException):
        raise
    except Exception:  # noqa: BLE001
        # Unverified decode failed (malformed base64, etc.) — fall through
        # to the Auth0 path which will surface a clean "Invalid token" error.
        pass

    payload = await _verify_jwt(token, request=request)
    # Standardise payload schema: every successful auth returns ``type``
    # so downstream ACL code can branch user-bridge vs agent-direct
    # without re-sniffing the token format.
    payload.setdefault("type", "user")
    return payload


def require_permission(permission: str):
    """FastAPI dependency factory: verify token and check for a specific permission."""

    async def permission_checker(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
    ) -> dict:
        payload = await verify_token(request, credentials)
        permissions: list[str] = payload.get("permissions", [])
        if permission not in permissions:
            path, method = _request_path(request)
            # ``sub`` is the *actor* (Auth0 user / client) — not a target.
            # We pass it as ``actor_id`` so analyst queries by target_id stay
            # clean (target_id is reserved for the asset being protected).
            record_auth_failure(
                reason="permission_denied",
                source_ip=_peer_ip(request),
                actor_id=payload.get("sub"),
                path=path,
                method=method,
                extra={"permission": permission},
            )
            raise ACNHTTPError(
                ErrorCode.MISSING_PERMISSION,
                403,
                message=f"Missing required permission: {permission}",
            )
        return payload

    return permission_checker


def require_internal_or_permission(permission: str):
    """FastAPI dependency factory: accept either a valid internal token (for
    Backend-to-ACN service calls) or a JWT with the required permission.

    When authenticated via internal token the payload is synthetic — it grants
    all permissions and sets sub to 'backend@internal'.  The actual creator
    identity should be passed via X-Creator-Id / X-Creator-Name / X-Creator-Type
    request headers and read by the endpoint handler.
    """
    async def checker(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
        x_internal_token: str | None = Header(default=None),
    ) -> dict:
        settings = _get_settings()

        # Dev mode: accept anything — but resolve ``acn_*`` API keys to the
        # owning agent's UUID so UUID-keyed ACLs still work (see
        # ``_resolve_agent_id_from_api_key`` for the why).
        if settings.dev_mode:
            resolved = await _resolve_agent_id_from_api_key(
                credentials.credentials if credentials is not None else None
            )
            if resolved is not None:
                return {
                    "sub": resolved,
                    "type": "agent",
                    "permissions": ["acn:read", "acn:write", "acn:admin"],
                }
            sub = credentials.credentials if credentials is not None else "dev@clients"
            return {
                "sub": sub,
                "type": "user",
                "permissions": ["acn:read", "acn:write", "acn:admin"],
            }

        # Internal token: trusted backend service call. Use constant-time
        # comparison to avoid timing-side-channel leaks of token contents.
        if (
            x_internal_token
            and settings.internal_api_token
            and secrets.compare_digest(x_internal_token, settings.internal_api_token)
        ):
            return {
                "sub": "backend@internal",
                "type": "internal",
                "permissions": ["acn:read", "acn:write", "acn:admin"],
            }

        # Otherwise require standard JWT + permission
        payload = await verify_token(request, credentials)
        permissions: list[str] = payload.get("permissions", [])
        if permission not in permissions:
            path, method = _request_path(request)
            record_auth_failure(
                reason="permission_denied",
                source_ip=_peer_ip(request),
                actor_id=payload.get("sub"),
                path=path,
                method=method,
                extra={"permission": permission},
            )
            raise ACNHTTPError(
                ErrorCode.MISSING_PERMISSION,
                403,
                message=f"Missing required permission: {permission}",
            )
        return payload

    return checker


async def get_subject(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> str:
    """FastAPI dependency: return the 'sub' claim from the JWT."""
    settings = _get_settings()

    if settings.dev_mode:
        if credentials is None:
            return "dev@clients"
        # Resolve ``acn_*`` API keys to the owning agent's UUID so callers
        # that key off ``get_subject`` (e.g. ownership checks) line up with
        # the agent identity rather than the raw token value.
        resolved = await _resolve_agent_id_from_api_key(credentials.credentials)
        return resolved if resolved is not None else credentials.credentials

    payload = await verify_token(request, credentials)
    return payload.get("sub", "unknown")


__all__ = [
    "verify_token",
    "require_permission",
    "require_internal_or_permission",
    "get_subject",
    # Exported for use by the A2A agent_lookup binding (api.py).
    "_get_acn_effective_issuer",
    "_get_acn_agent_jwks",
    "_verify_acn_agent_jwt",
]
