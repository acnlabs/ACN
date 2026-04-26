"""SSRF / DNS-rebinding defences.

Why this module exists
----------------------
ACN exposes a reverse proxy at ``/api/v1/agents/{agent_id}`` that forwards
arbitrary HTTP traffic to whatever URL the agent registered as its
``endpoint``. Without controls, anyone who can register an agent can ask
ACN to issue requests to internal services (cloud metadata at
``169.254.169.254``, ``localhost`` admin panels, the Kubernetes API on
``10.x.x.x``, …) — classic Server-Side Request Forgery.

We layer two checks so a single bypass cannot defeat the defence:

1. ``validate_endpoint_url`` — synchronous, no DNS. Run at *registration*
   time so an obviously-malicious URL (private IP literal, file:// scheme,
   credentials embedded) is rejected immediately.
2. ``safe_resolve_target`` — async, performs DNS resolution. Run before
   each *outbound* request so an endpoint that resolved to a public IP at
   registration time but now points to ``127.0.0.1`` is also blocked
   (mitigates the "register first, rebind later" attack).

There is still a TOCTOU window between the DNS check and the actual
connect. Defeating it requires IP-pinning at the socket layer (e.g.
custom ``httpx`` transport), which is a worthwhile follow-up but out of
scope for the pre-launch P0 fix. The TOCTOU window is narrow because
each check uses ``getaddrinfo`` immediately before the request; an
attacker would need to flip DNS in single-millisecond windows AND know
the precise moment ACN dispatches a forwarded request.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

# IPv4 ranges that are never a legitimate destination from a public service.
# Curated from RFC 1918, IANA special-purpose registries, and historic SSRF
# write-ups. Unicast cloud metadata (e.g. EC2 169.254.169.254) is covered
# by the 169.254.0.0/16 link-local block.
_BLOCKED_CIDRS_V4 = (
    "0.0.0.0/8",  # this network
    "10.0.0.0/8",  # private
    "100.64.0.0/10",  # CGNAT
    "127.0.0.0/8",  # loopback
    "169.254.0.0/16",  # link-local + EC2 metadata
    "172.16.0.0/12",  # private
    "192.0.0.0/24",  # IETF protocol assignments
    "192.0.2.0/24",  # TEST-NET-1
    "192.168.0.0/16",  # private
    "198.18.0.0/15",  # benchmarking
    "198.51.100.0/24",  # TEST-NET-2
    "203.0.113.0/24",  # TEST-NET-3
    "224.0.0.0/4",  # multicast
    "240.0.0.0/4",  # reserved
    "255.255.255.255/32",  # broadcast
)

_BLOCKED_CIDRS_V6 = (
    "::/128",  # unspecified
    "::1/128",  # loopback
    "::ffff:0:0/96",  # IPv4-mapped (could escape v4 checks if not blocked)
    "64:ff9b::/96",  # NAT64 (typically maps to private v4)
    "fc00::/7",  # unique-local (private)
    "fe80::/10",  # link-local
    "ff00::/8",  # multicast
    "2001:db8::/32",  # documentation
    "100::/64",  # discard prefix
)

_ALLOWED_SCHEMES = {"http", "https"}


class SSRFViolation(ValueError):
    """Raised when an endpoint URL is rejected by the SSRF guard."""


def _is_blocked_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_loopback: bool
) -> bool:
    """Whether ``ip`` falls in one of the blocked CIDR ranges.

    When ``allow_loopback`` is true, loopback addresses (127.0.0.0/8, ::1)
    are accepted — used for local development. Other private/reserved
    ranges are still blocked because they are never legitimate even in
    dev (they would mean the dev box can attack its own private network).
    """
    if ip.is_unspecified:
        return True
    if allow_loopback and ip.is_loopback:
        return False
    nets = _BLOCKED_CIDRS_V4 if ip.version == 4 else _BLOCKED_CIDRS_V6
    return any(ip in ipaddress.ip_network(n) for n in nets)


def _parse_url_or_raise(url: str) -> tuple[str, int | None, str]:
    """Parse and validate URL syntax. Returns (hostname, port, scheme).

    Raises ``SSRFViolation`` for unsupported schemes, embedded credentials,
    or missing host.
    """
    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise SSRFViolation(f"Malformed URL {url!r}: {e}") from e

    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SSRFViolation(
            f"Disallowed URL scheme {scheme!r}; only http/https are accepted."
        )
    if parsed.username or parsed.password:
        raise SSRFViolation("URL must not contain embedded credentials.")
    host = parsed.hostname
    if not host:
        raise SSRFViolation("URL must have a host.")
    try:
        port = parsed.port
    except ValueError as e:
        raise SSRFViolation(f"Invalid port in URL {url!r}: {e}") from e
    return host, port, scheme


def validate_endpoint_url(url: str, *, allow_loopback: bool = False) -> None:
    """Synchronous syntactic + IP-literal validation. No DNS lookup.

    Use this at endpoint *registration* time so URLs like
    ``http://169.254.169.254/`` or ``http://10.0.0.1/`` are rejected
    immediately, before they are ever dispatched. Hostnames that point
    to private IPs via DNS will pass this check — they are caught by
    ``safe_resolve_target`` at request dispatch time instead.

    Raises:
        SSRFViolation: if the URL is malformed, uses a non-http(s) scheme,
            embeds credentials, or is an IP literal in a blocked range.
    """
    host, _port, _scheme = _parse_url_or_raise(url)
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return  # hostname — defer to runtime DNS check
    if _is_blocked_ip(ip, allow_loopback=allow_loopback):
        raise SSRFViolation(
            f"Endpoint host {host!r} is in a blocked address range. "
            "ACN refuses to forward requests to private/reserved IPs."
        )


async def safe_resolve_target(
    url: str, *, allow_loopback: bool = False
) -> tuple[str, str]:
    """Resolve the URL's host and verify *every* answer is a public IP.

    Returns a ``(hostname, ip)`` tuple — the first usable resolved IP is
    returned so callers can pin the connection to it if they want
    stricter DNS-rebinding protection (TOCTOU mitigation).

    The check is fail-closed: even if only ONE answer points to a
    private range, the whole resolution is rejected. This defeats
    multi-A-record DNS-rebinding tricks where the attacker mixes a
    public address (to pass any one-shot check) with a private one
    (which the OS may pick on the next connection).

    Raises:
        SSRFViolation: on syntactic failure, bad scheme, blocked IP
            literal, DNS failure, or any resolved IP in a blocked range.
    """
    host, _port, _scheme = _parse_url_or_raise(url)

    # Direct IP literal — same logic as the sync validator.
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None

    if ip is not None:
        if _is_blocked_ip(ip, allow_loopback=allow_loopback):
            raise SSRFViolation(
                f"URL host {host!r} is in a blocked range."
            )
        return host, str(ip)

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(
            host, None, type=socket.SOCK_STREAM
        )
    except socket.gaierror as e:
        raise SSRFViolation(
            f"DNS resolution failed for {host!r}: {e}"
        ) from e

    first_ip: str | None = None
    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0]
        try:
            resolved = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_blocked_ip(resolved, allow_loopback=allow_loopback):
            raise SSRFViolation(
                f"Hostname {host!r} resolves to blocked address {addr!r}; "
                "refusing to dispatch outbound request."
            )
        if first_ip is None:
            first_ip = addr

    if first_ip is None:
        raise SSRFViolation(f"No usable IP returned for {host!r}.")
    return host, first_ip
