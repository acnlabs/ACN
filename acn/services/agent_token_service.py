"""Agent JWT issuance (ADR-0007).

ACN is the agent identity authority. This service turns an agent's
long-lived ``acn_*`` API key (the client credential) into a short-lived,
standards-compliant RS256 JWT that resource servers verify *offline*
against the JWKS published at ``/.well-known/jwks.json``.

Design notes
============
- **Self-contained identity.** The token carries ``sub = agent_id`` and
  ``scope`` as signed claims, so resource servers read the agent identity
  straight from the verified JWT — no shared mapping table, no callback
  to ACN per request (contrast the legacy ``agent_auth0_credentials``
  lookup the AgentPlanet backend used to do on every call).
- **Public key derived from the private key.** JWKS ``n``/``e`` are
  computed from the PEM private key at construction time, so operators
  only set one secret (``AGENT_JWT_PRIVATE_KEY``).
- **Disabled-by-default.** With no private key configured the issuer is
  inert: ``enabled`` is False, ``jwks()`` returns an empty key set, and
  the route layer surfaces 503. This keeps self-hosted ACN bootable
  without standing up issuance.
"""

from __future__ import annotations

import base64
import time
import uuid

import structlog  # type: ignore[import-untyped]
from jose import jwt

logger = structlog.get_logger()


def _b64url_uint(val: int) -> str:
    """Base64url-encode a big-endian unsigned int with no padding (RFC 7518)."""
    raw = val.to_bytes((val.bit_length() + 7) // 8 or 1, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class AgentTokenIssuer:
    """Mints and publishes keys for ACN-issued agent JWTs.

    Construct once (e.g. cached per process). Safe to construct with a
    ``None`` private key — the resulting instance is ``enabled == False``
    and refuses to mint.
    """

    def __init__(
        self,
        *,
        private_key_pem: str | None,
        kid: str,
        issuer: str,
        default_audience: str,
        ttl_seconds: int,
        default_scope: str,
    ) -> None:
        self._kid = kid
        self._issuer = issuer.rstrip("/")
        self._default_audience = default_audience
        self._ttl = ttl_seconds
        self._default_scope = default_scope
        self._private_key_pem = private_key_pem.strip() if private_key_pem else None
        self._jwks_keys: list[dict] = []

        if self._private_key_pem:
            try:
                self._jwks_keys = [self._derive_jwk(self._private_key_pem, kid)]
            except Exception as exc:  # noqa: BLE001
                logger.error("agent_jwt_private_key_invalid", error=str(exc))
                self._private_key_pem = None

    @property
    def enabled(self) -> bool:
        return self._private_key_pem is not None

    @property
    def issuer(self) -> str:
        return self._issuer

    @staticmethod
    def _derive_jwk(private_key_pem: str, kid: str) -> dict:
        """Build the public JWK (RSA) from a PEM private key."""
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
        from cryptography.hazmat.primitives.serialization import (
            load_pem_private_key,
        )

        priv = load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
        if not isinstance(priv, RSAPrivateKey):
            # RS256 signing requires an RSA key; reject anything else loudly
            # so a misconfigured PEM disables the issuer instead of 500ing.
            raise ValueError("AGENT_JWT_PRIVATE_KEY must be an RSA private key")
        pub_numbers = priv.public_key().public_numbers()
        return {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": kid,
            "n": _b64url_uint(pub_numbers.n),
            "e": _b64url_uint(pub_numbers.e),
        }

    def jwks(self) -> dict:
        """Return the JSON Web Key Set for the public verification key(s)."""
        return {"keys": list(self._jwks_keys)}

    def mint(
        self,
        agent_id: str,
        *,
        audience: str | None = None,
        scope: str | None = None,
    ) -> dict:
        """Issue a short-lived JWT for ``agent_id``.

        Returns an OAuth2-style token response dict. Raises
        ``RuntimeError`` if the issuer is disabled (no signing key).
        """
        if not self.enabled:
            raise RuntimeError("Agent JWT issuer is not configured (no signing key)")

        now = int(time.time())
        aud = audience or self._default_audience
        scp = scope if scope is not None else self._default_scope
        claims = {
            "iss": self._issuer,
            "sub": agent_id,
            "aud": aud,
            "scope": scp,
            "iat": now,
            "nbf": now,
            "exp": now + self._ttl,
            "jti": str(uuid.uuid4()),
            # Marks the principal class so resource servers can branch
            # agent-direct vs human without sniffing the sub format.
            "acn_principal": "agent",
        }
        token = jwt.encode(
            claims,
            self._private_key_pem,
            algorithm="RS256",
            headers={"kid": self._kid},
        )
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": self._ttl,
            "scope": scp,
        }
