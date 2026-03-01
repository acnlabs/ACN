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
import time
from typing import Any

import httpx
import structlog
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError

logger = structlog.get_logger()

_bearer_scheme = HTTPBearer(auto_error=False)

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


async def _get_jwks(domain: str) -> dict:
    """Return JWKS for *domain*, refreshing the cache when stale.

    Uses an asyncio.Lock to ensure only one coroutine performs the network
    fetch when the cache expires (prevents thundering herd).
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

        jwks_url = f"https://{domain}/.well-known/jwks.json"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(jwks_url)
            resp.raise_for_status()
            jwks = resp.json()

        _jwks_cache["keys"] = jwks
        _jwks_cache["domain"] = domain
        _jwks_cache["fetched_at"] = now
        logger.info("jwks_cache_refreshed", domain=domain)
        return jwks


# ---------------------------------------------------------------------------
# Core JWT verification
# ---------------------------------------------------------------------------


async def _verify_jwt(token: str) -> dict:
    """Verify an Auth0 JWT and return its payload."""
    settings = _get_settings()

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
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unable to find appropriate signing key.",
            )

        payload = jwt.decode(
            token,
            rsa_key,
            algorithms=["RS256"],
            audience=settings.auth0_audience,
            issuer=f"https://{settings.auth0_domain}/",
        )
        return payload

    # ExpiredSignatureError must be caught before JWTError (it's a subclass)
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired.",
        ) from None
    except JWTError as e:
        logger.warning("jwt_verification_failed", error=str(e))
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
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> dict:
    """FastAPI dependency: verify Bearer token and return JWT payload."""
    settings = _get_settings()

    if settings.dev_mode and credentials is None:
        return {"sub": "dev@clients", "permissions": ["acn:read", "acn:write", "acn:admin"]}

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return await _verify_jwt(credentials.credentials)


def require_permission(permission: str):
    """FastAPI dependency factory: verify token and check for a specific permission."""

    async def permission_checker(
        credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
    ) -> dict:
        payload = await verify_token(credentials)
        permissions: list[str] = payload.get("permissions", [])
        if permission not in permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required permission: {permission}",
            )
        return payload

    return permission_checker


async def get_subject(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> str:
    """FastAPI dependency: return the 'sub' claim from the JWT."""
    settings = _get_settings()

    if settings.dev_mode and credentials is None:
        return "dev@clients"

    payload = await verify_token(credentials)
    return payload.get("sub", "unknown")


__all__ = [
    "verify_token",
    "require_permission",
    "get_subject",
]
