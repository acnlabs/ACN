"""Canonical agent identity helpers (ARD-aligned).

Single source of truth for the ARD ``urn:air:`` discovery identifier
(https://agenticresourcediscovery.org/spec/ §4.2.1) so the ARD adapter
(``routes/ard.py``) and ACN's own registry responses (``routes/registry.py``)
never derive it two different ways.

The URN is a *discovery handle*, intentionally decoupled from ACN's
internal ``agent_id`` (which stays the system-of-record primary key) and
from any cryptographic identity. It is fully derivable from
``(publisher_domain, agent_id)`` and is reversible via :func:`parse_agent_urn`
so a client holding the URN can recover the ACN ``agent_id`` without a
lookup table.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .config import get_settings

# urn:air:<publisher-FQDN>:agent:<agent-id>
# publisher is a bare FQDN (no colons); agent-id is the trailing remainder.
_AGENT_URN_RE = re.compile(r"^urn:air:(?P<publisher>[^:]+):agent:(?P<agent_id>.+)$")


def resolve_publisher_domain() -> str:
    """Return the verifiable FQDN used as the ARD URN authority anchor.

    Prefers the explicit ``ard_publisher_domain`` setting; otherwise
    derives the host from ``gateway_base_url`` so a standard deployment
    is spec-compliant without extra configuration (ARD §4.2.1 requires a
    bare FQDN — no scheme, no path).
    """
    settings = get_settings()
    configured = (settings.ard_publisher_domain or "").strip()
    if configured:
        return configured
    host = urlparse(settings.gateway_base_url).hostname
    return host or "acn.local"


def build_agent_urn(agent_id: str, *, publisher: str | None = None) -> str:
    """Build the ARD discovery URN for an ACN agent.

    ``publisher`` defaults to :func:`resolve_publisher_domain` so callers
    that don't already have it resolved get the deployment default.
    """
    pub = publisher or resolve_publisher_domain()
    return f"urn:air:{pub}:agent:{agent_id}"


def parse_agent_urn(urn: str) -> tuple[str, str] | None:
    """Reverse of :func:`build_agent_urn`.

    Returns ``(publisher, agent_id)`` for a well-formed agent URN, or
    ``None`` when the string is not an ``urn:air:<publisher>:agent:<id>``.
    """
    if not isinstance(urn, str):
        return None
    match = _AGENT_URN_RE.match(urn.strip())
    if not match:
        return None
    return match.group("publisher"), match.group("agent_id")
