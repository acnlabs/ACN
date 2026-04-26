"""Startup TLS configuration check (security audit M13).

Why this exists
---------------
ACN ships several URLs that face *external* parties:

* ``gateway_base_url`` — embedded in agent-card responses, claim links,
  referral URLs, and webhook callback hints. The browser / agent on the
  other end will use whatever scheme we hand them. Plain HTTP here means
  "we told the world to talk to us in cleartext".
* ``frontend_base_url`` — same story, used for human-facing claim links.
* ``webhook_url`` / ``billing_webhook_url`` — outgoing callbacks where
  ACN posts agent / payment events. Plain HTTP here means HMAC-signed
  payloads are still sniffable in transit, and any session cookie / API
  key the receiving end attaches to its acknowledgement is plaintext.
* ``backend_url`` — service-to-service calls to AgentPlanet Backend
  carrying internal tokens. Plain HTTP between separate hosts is a leak
  of the X-Internal-Token header.

We do NOT hard-fail on plain HTTP. ACN frequently runs behind a
TLS-terminating reverse proxy (Railway, Nginx, Cloudflare, GKE Gateway)
that talks plain HTTP to the upstream — refusing to start in that
configuration would be wrong. We also can't easily distinguish
"in-cluster service mesh" from "across the public internet". So:

* Hard rule: in dev mode, no warning at all.
* Soft rule: in production, log a structured warning per offending URL
  so it shows up in alerting dashboards. Operators acknowledge the
  warning explicitly (e.g. by setting ``http://internal-backend.svc``
  on a deliberate basis) and we keep the noise tractable.

Tested in ``tests/test_tls_check.py``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

# URLs that, when set, MUST be HTTPS in production (dev_mode=False).
# Pairs of (settings-attribute-name, human description). Description is
# included in the structured log so operators don't have to grep code.
_EXTERNAL_HTTPS_REQUIRED: tuple[tuple[str, str], ...] = (
    ("gateway_base_url", "external base URL embedded in agent-card responses"),
    ("frontend_base_url", "human-facing claim / referral links"),
    ("webhook_url", "outbound webhook target"),
    ("billing_webhook_url", "outbound billing webhook target"),
    ("backend_url", "service-to-service backend (carries internal token)"),
)


def _is_plain_http(url: str) -> bool:
    """Return True iff ``url`` is set, well-formed, and uses ``http`` (not https).

    We only care about ``http://`` schemes. ``ws://``, ``wss://``,
    ``file://``, etc. are out of scope — those don't appear in the
    audited setting names. An empty/None URL returns False (not unsafe,
    just absent).
    """
    if not url:
        return False
    try:
        scheme = urlparse(url).scheme
    except Exception:
        return False
    return scheme.lower() == "http"


def _is_loopback(url: str) -> bool:
    """Return True if the URL points at a loopback interface.

    Plain HTTP to ``localhost`` / ``127.0.0.1`` is fine even in production
    — typical for in-pod sidecars or single-host deployments. We don't
    warn on it because it's a deliberate, common, and safe pattern.
    """
    if not url:
        return False
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    return host in {"localhost", "127.0.0.1", "::1"}


def check_tls_config(settings: Any, logger: Any) -> list[str]:
    """Scan ``settings`` and log warnings for production URLs on plain HTTP.

    Returns the list of attribute names that triggered a warning so
    callers (and tests) can assert on the result without having to
    inspect log output. The log itself is the operator-facing channel —
    the return value is purely for programmatic verification.

    No-op when ``settings.dev_mode`` is true: dev rigs commonly use
    ``http://localhost:*`` everywhere and warning would be noise.
    """
    if getattr(settings, "dev_mode", False):
        return []

    flagged: list[str] = []
    for attr, description in _EXTERNAL_HTTPS_REQUIRED:
        url = getattr(settings, attr, None)
        if not _is_plain_http(url):
            continue
        if _is_loopback(url):
            # Loopback in production usually means an in-host sidecar.
            # Acceptable, no warning.
            continue
        # Use a stable structured log key (``tls_plaintext_url``) so
        # operators can write a single alert rule that fires on any
        # mis-configured URL.
        logger.warning(
            "tls_plaintext_url",
            setting=attr,
            description=description,
            url=url,
            advice=(
                "URL is plain HTTP outside loopback. In production this "
                "leaks transit data. Either point it at HTTPS or terminate "
                "TLS at a reverse proxy and update this setting to the "
                "external HTTPS URL."
            ),
        )
        flagged.append(attr)
    return flagged
