"""
ACN Auth0 Middleware

Standalone Auth0 JWT verification using python-jose.
Does NOT rely on Backend's auth module or sys.path manipulation.

In production (dev_mode=False), Auth0 configuration is required.
In development (dev_mode=True), a stub is used for convenience.

JWKS are cached in-memory with a configurable TTL (default 600s) to avoid
hitting Auth0's endpoint on every request.
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
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unable to find appropriate signing key.",
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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired.",
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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token.",
        ) from e
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
    """FastAPI dependency: verify Bearer token and return JWT payload."""
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
        return {"sub": sub, "permissions": ["acn:read", "acn:write", "acn:admin"]}

    if credentials is None:
        path, method = _request_path(request)
        record_auth_failure(
            reason="bearer_missing",
            source_ip=_peer_ip(request),
            path=path,
            method=method,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return await _verify_jwt(credentials.credentials, request=request)


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
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required permission: {permission}",
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
            return {"sub": sub, "permissions": ["acn:read", "acn:write", "acn:admin"]}

        # Internal token: trusted backend service call. Use constant-time
        # comparison to avoid timing-side-channel leaks of token contents.
        if (
            x_internal_token
            and settings.internal_api_token
            and secrets.compare_digest(x_internal_token, settings.internal_api_token)
        ):
            return {
                "sub": "backend@internal",
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
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required permission: {permission}",
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
]
