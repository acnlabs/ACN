"""C1b security tests: SSRF / DNS-rebinding three-piece-set.

What this pins down
-------------------
The pre-launch C1b finding was that ACN's reverse proxy
(``/api/v1/agents/{agent_id}``) blindly forwards to whatever URL an agent
registered as its endpoint. The fix has three layers:

1. ``validate_endpoint_url`` — synchronous, runs at registration time and
   rejects IP-literal URLs that point at private/reserved/link-local
   ranges (``127.0.0.1``, ``169.254.169.254``, ``10.x``, …).
2. ``safe_resolve_target`` — async, runs before each outbound request.
   Re-resolves the hostname and rejects if *any* answer is in a blocked
   range, defeating "register public hostname now, repoint DNS to
   ``127.0.0.1`` later" rebinding.
3. ``follow_redirects=False`` on every outbound httpx client — covered by
   inspection in ``test_follow_redirects_is_disabled`` rather than a live
   network test.

These tests are pure unit tests; they patch ``getaddrinfo`` so we never
actually go to a resolver.
"""

from __future__ import annotations

import ipaddress
import socket
from unittest.mock import patch

import pytest

from acn.security import (
    SSRFViolation,
    safe_resolve_target,
    validate_endpoint_url,
)

# ─────────────────────────────────────────────
# 1. validate_endpoint_url — registration-time
# ─────────────────────────────────────────────


class TestValidateEndpointUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",  # cloud metadata
            "http://127.0.0.1/admin",
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://172.16.0.1/",
            "http://[::1]/",
            "http://[fc00::1]/",
            "http://0.0.0.0/",
        ],
    )
    def test_blocks_private_ip_literals(self, url: str) -> None:
        with pytest.raises(SSRFViolation):
            validate_endpoint_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://api.example.com/a2a",
            "https://203.0.113.5/foo",  # TEST-NET-3 — actually blocked
        ],
    )
    def test_hostnames_pass_through_to_runtime(self, url: str) -> None:
        # Hostnames are not resolved here. Only literal-IP URLs in private
        # ranges should fail. TEST-NET-3 is reserved so it must fail.
        if url.startswith("https://203.0.113"):
            with pytest.raises(SSRFViolation):
                validate_endpoint_url(url)
        else:
            validate_endpoint_url(url)

    def test_blocks_non_http_scheme(self) -> None:
        for bad in ("file:///etc/passwd", "ftp://example.com", "gopher://x"):
            with pytest.raises(SSRFViolation):
                validate_endpoint_url(bad)

    def test_blocks_embedded_credentials(self) -> None:
        with pytest.raises(SSRFViolation):
            validate_endpoint_url("http://user:pass@example.com/")

    def test_allow_loopback_only_when_explicitly_enabled(self) -> None:
        # Default: loopback is blocked
        with pytest.raises(SSRFViolation):
            validate_endpoint_url("http://127.0.0.1:9000/")
        # Dev mode opt-in: loopback OK, but other private ranges still blocked
        validate_endpoint_url("http://127.0.0.1:9000/", allow_loopback=True)
        with pytest.raises(SSRFViolation):
            validate_endpoint_url("http://10.0.0.1/", allow_loopback=True)


# ─────────────────────────────────────────────
# 2. safe_resolve_target — runtime DNS rebinding
# ─────────────────────────────────────────────


def _addrinfo(*ips: str) -> list:
    out = []
    for ip in ips:
        try:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        except Exception:
            family = socket.AF_INET
        out.append((family, socket.SOCK_STREAM, 0, "", (ip, 0)))
    return out


class TestSafeResolveTarget:
    @pytest.mark.asyncio
    async def test_passes_when_all_ips_public(self) -> None:
        async def fake_resolve(host, *args, **kwargs):
            return _addrinfo("203.0.114.1")  # not in TEST-NET-3 range

        # 203.0.114.1 is just outside 203.0.113.0/24, so it's public.
        # Sanity: ensure ipaddress agrees
        assert not ipaddress.ip_address("203.0.114.1").is_private
        with patch("acn.security.ssrf.asyncio.get_running_loop") as gloop:

            class L:
                async def getaddrinfo(self, *a, **kw):
                    return _addrinfo("8.8.8.8")

            gloop.return_value = L()
            host, ip = await safe_resolve_target("https://api.example.com/a2a")
            assert host == "api.example.com"
            assert ip == "8.8.8.8"

    @pytest.mark.asyncio
    async def test_blocks_if_any_resolved_ip_is_private(self) -> None:
        """The classic DNS-rebinding payload: one public + one private
        in the answer set. Fail closed even though one IP looks fine."""

        class L:
            async def getaddrinfo(self, *a, **kw):
                return _addrinfo("8.8.8.8", "127.0.0.1")

        with patch("acn.security.ssrf.asyncio.get_running_loop", return_value=L()):
            with pytest.raises(SSRFViolation):
                await safe_resolve_target("https://api.example.com/a2a")

    @pytest.mark.asyncio
    async def test_blocks_metadata_endpoint(self) -> None:
        class L:
            async def getaddrinfo(self, *a, **kw):
                return _addrinfo("169.254.169.254")

        with patch("acn.security.ssrf.asyncio.get_running_loop", return_value=L()):
            with pytest.raises(SSRFViolation):
                await safe_resolve_target("https://metadata.example.com/")

    @pytest.mark.asyncio
    async def test_dns_failure_is_ssrf_violation_not_silent_pass(self) -> None:
        class L:
            async def getaddrinfo(self, *a, **kw):
                raise socket.gaierror(8, "nodename")

        with patch("acn.security.ssrf.asyncio.get_running_loop", return_value=L()):
            with pytest.raises(SSRFViolation):
                await safe_resolve_target("https://nope.invalid/")

    @pytest.mark.asyncio
    async def test_ip_literal_in_url_uses_synchronous_check(self) -> None:
        # Should not even call getaddrinfo for an IP literal.
        called = {"yes": False}

        class L:
            async def getaddrinfo(self, *a, **kw):
                called["yes"] = True
                return _addrinfo("8.8.8.8")

        with patch("acn.security.ssrf.asyncio.get_running_loop", return_value=L()):
            with pytest.raises(SSRFViolation):
                await safe_resolve_target("http://10.0.0.5/")
        assert called["yes"] is False


# ─────────────────────────────────────────────
# 3. follow_redirects=False on outbound clients
# ─────────────────────────────────────────────


class TestFollowRedirectsDisabled:
    """A 3xx response could otherwise escape the SSRF guard by sending
    httpx to an internal IP after we already validated the registered URL.
    We grep the source so the constant cannot regress silently."""

    def test_proxy_route_disables_redirects(self) -> None:
        from pathlib import Path

        src = Path("acn/routes/registry.py").read_text()
        assert "follow_redirects=False" in src, (
            "_proxy_to_agent must construct httpx.AsyncClient with "
            "follow_redirects=False; otherwise a 3xx Location header "
            "could redirect the SSRF check."
        )

    def test_message_router_disables_redirects(self) -> None:
        from pathlib import Path

        src = Path("acn/infrastructure/messaging/message_router.py").read_text()
        assert "follow_redirects=False" in src, (
            "MessageRouter._get_client must build httpx.AsyncClient with "
            "follow_redirects=False."
        )
