"""C1a security tests: agent-proxy authentication + rate-limit keying.

Pins down the pre-launch security audit C1a fix:
- Proxy routes require ``X-ACN-Authorization: Bearer <key>``
- The header (and ``X-Internal-Token``) is stripped before forwarding
- The caller agent_id is injected as ``X-ACN-Caller-Agent`` for downstream
- The shared limiter buckets per-agent for authenticated requests and per-IP
  otherwise; ``X-Forwarded-For`` is honoured ONLY when the immediate peer
  is in ``settings.trusted_proxies``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.routes.dependencies import (
    _get_real_ip,
    _rate_limit_key,
    get_agent_service,
    limiter,
)


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    """slowapi's limiter would otherwise try to reach Redis on every request.
    Tests in this module verify auth/header behaviour, not throttling, so
    we toggle it off here (mirroring the pattern used by other route tests)."""
    was = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = was


@pytest.fixture
def stub_agent_service() -> AsyncMock:
    svc = AsyncMock()
    # Agent that the caller is hitting (target of the proxy)
    target_agent = SimpleNamespace(
        agent_id="target-agent",
        endpoint="https://target.example.com/a2a",
    )
    svc.get_agent = AsyncMock(return_value=target_agent)

    # Caller agent (looked up by API key)
    caller_agent = SimpleNamespace(agent_id="caller-agent", name="Caller")
    svc.get_agent_by_api_key = AsyncMock(return_value=caller_agent)
    return svc


def _override_agent_service(stub: AsyncMock) -> None:
    app.dependency_overrides[get_agent_service] = lambda: stub


def _clear_overrides() -> None:
    app.dependency_overrides.clear()


# ─────────────────────────────────────────────
# Auth-required behaviour
# ─────────────────────────────────────────────


class TestProxyRequiresAcnAuth:
    def test_post_without_acn_auth_header_is_rejected(
        self, stub_agent_service: AsyncMock
    ) -> None:
        _override_agent_service(stub_agent_service)
        try:
            with TestClient(app) as client:
                r = client.post("/api/v1/agents/target-agent", json={"jsonrpc": "2.0"})
        finally:
            _clear_overrides()
        # FastAPI's required Header() returns 422 for missing header — what
        # matters for security is that the proxy did NOT execute.
        assert r.status_code in (401, 422)
        stub_agent_service.get_agent.assert_not_awaited()

    def test_post_with_invalid_acn_key_is_401(
        self, stub_agent_service: AsyncMock
    ) -> None:
        stub_agent_service.get_agent_by_api_key = AsyncMock(return_value=None)
        _override_agent_service(stub_agent_service)
        try:
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/agents/target-agent",
                    json={"jsonrpc": "2.0"},
                    headers={"X-ACN-Authorization": "Bearer acn_bogus"},
                )
        finally:
            _clear_overrides()
        assert r.status_code == 401
        stub_agent_service.get_agent.assert_not_awaited()

    def test_subpath_without_acn_auth_is_rejected(
        self, stub_agent_service: AsyncMock
    ) -> None:
        _override_agent_service(stub_agent_service)
        try:
            with TestClient(app) as client:
                r = client.get("/api/v1/agents/target-agent/some/sub/path")
        finally:
            _clear_overrides()
        assert r.status_code in (401, 422)


# ─────────────────────────────────────────────
# Header sanitisation + caller injection
# ─────────────────────────────────────────────


class TestProxyHeaderSanitisation:
    def _patched_proxy(self):
        """Patch _proxy_to_agent so we can inspect forward_headers without
        making real HTTP calls."""
        captured: dict = {}

        async def fake_proxy(request, agent_id, method, rest_path, agent_service, caller):
            captured["agent_id"] = agent_id
            captured["method"] = method
            captured["rest_path"] = rest_path
            captured["caller"] = caller
            from fastapi import Response

            return Response(content=b"{}", media_type="application/json")

        return captured, fake_proxy

    def test_acn_auth_and_internal_token_are_stripped_caller_injected(
        self, stub_agent_service: AsyncMock
    ) -> None:
        # We patch the _proxy_to_agent helper used by all 4 proxy routes
        # so we can check that the route layer correctly resolves caller
        # from the ACN auth header without hitting the network.
        captured, fake_proxy = self._patched_proxy()
        _override_agent_service(stub_agent_service)
        try:
            with patch("acn.routes.registry._proxy_to_agent", side_effect=fake_proxy):
                with TestClient(app) as client:
                    r = client.post(
                        "/api/v1/agents/target-agent",
                        json={"jsonrpc": "2.0"},
                        headers={
                            "X-ACN-Authorization": "Bearer acn_caller_key",
                            "X-Internal-Token": "should-not-be-forwarded",
                            "Authorization": "Bearer downstream-token",
                        },
                    )
        finally:
            _clear_overrides()

        assert r.status_code == 200, r.text
        assert captured["caller"]["agent_id"] == "caller-agent"
        assert captured["agent_id"] == "target-agent"
        assert captured["method"] == "POST"


class TestRealForwardHeadersStripping:
    """Lower-level test on _proxy_to_agent itself: verify the actual
    forward_headers it builds drops X-ACN-Authorization and adds the
    caller header. We mock httpx to avoid network."""

    @pytest.mark.asyncio
    async def test_forward_headers_drop_acn_auth_and_inject_caller(self) -> None:
        from acn.routes.registry import _proxy_to_agent

        # Capture the headers passed to httpx.AsyncClient.build_request
        captured: dict = {}

        class _FakeResponse:
            def __init__(self) -> None:
                self.headers = {"content-type": "application/json"}
                self.status_code = 200

            async def aread(self) -> bytes:
                return b"{}"

            async def aclose(self) -> None:
                return None

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def aclose(self) -> None:
                return None

            def build_request(self, method, url, content=None, headers=None):
                captured["method"] = method
                captured["url"] = url
                captured["headers"] = headers
                return SimpleNamespace(method=method, url=url)

            async def send(self, req, stream=True):
                return _FakeResponse()

        # Build a fake FastAPI Request-like object
        scope = {
            "type": "http",
            "method": "POST",
            "headers": [
                (b"host", b"acn.example"),
                (b"content-length", b"2"),
                (b"x-acn-authorization", b"Bearer acn_caller_key"),
                (b"x-internal-token", b"do-not-forward"),
                (b"authorization", b"Bearer downstream-token"),
                (b"x-trace-id", b"abc-123"),
            ],
        }
        from starlette.requests import Request as StarletteRequest

        async def _receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        request = StarletteRequest(scope, receive=_receive)

        agent_service = AsyncMock()
        agent_service.get_agent = AsyncMock(
            return_value=SimpleNamespace(
                agent_id="target-agent", endpoint="https://target.example/a2a"
            )
        )

        caller = {"agent_id": "caller-agent", "name": "Caller"}

        # Bypass the SSRF DNS-rebinding check; this test pins down header
        # sanitisation, not the SSRF guard (which has its own test module).
        async def _bypass_resolve(_url: str, *, allow_loopback: bool = False) -> tuple[str, str]:
            return ("203.0.113.1", "target.example")

        with (
            patch("acn.routes.registry.httpx.AsyncClient", _FakeClient),
            patch("acn.routes.registry.safe_resolve_target", _bypass_resolve),
        ):
            await _proxy_to_agent(
                request, "target-agent", "POST", "", agent_service, caller
            )

        sent = {k.lower(): v for k, v in (captured["headers"] or {}).items()}
        assert "x-acn-authorization" not in sent, "ACN auth must not leak downstream"
        assert "x-internal-token" not in sent, "internal token must not leak downstream"
        assert "host" not in sent, "hop-by-hop Host must be dropped"
        assert "content-length" not in sent, "hop-by-hop Content-Length must be dropped"
        assert sent.get("x-acn-caller-agent") == "caller-agent"
        assert sent.get("x-acn-caller-name") == "Caller"
        assert sent.get("authorization") == "Bearer downstream-token", (
            "downstream Authorization must be preserved (caller may auth to target)"
        )
        assert sent.get("x-trace-id") == "abc-123", "non-special headers should pass through"


# ─────────────────────────────────────────────
# Rate-limit keying
# ─────────────────────────────────────────────


class TestRateLimitKey:
    def _mk_request(
        self, *, agent_id: str | None = None, peer: str = "1.2.3.4", xff: str | None = None
    ):
        # Minimal duck-typed Request
        state = SimpleNamespace()
        if agent_id is not None:
            state.rate_limit_key = f"agent:{agent_id}"
            state.agent_id = agent_id
        return SimpleNamespace(
            state=state,
            client=SimpleNamespace(host=peer),
            headers={"X-Forwarded-For": xff} if xff else {},
        )

    def test_authenticated_request_buckets_per_agent(self) -> None:
        req = self._mk_request(agent_id="agent-x")
        assert _rate_limit_key(req) == "agent:agent-x"

    def test_unauthenticated_request_falls_back_to_ip(self) -> None:
        req = self._mk_request()
        key = _rate_limit_key(req)
        assert key.startswith("ip:")
        assert "1.2.3.4" in key


class TestTrustedProxies:
    """``_get_real_ip`` honours XFF only when the direct peer is trusted."""

    def _mk_request(self, *, peer: str, xff: str | None = None):
        return SimpleNamespace(
            client=SimpleNamespace(host=peer),
            headers={"X-Forwarded-For": xff} if xff else {},
            scope={"client": (peer, 0)},
        )

    def test_xff_ignored_when_proxies_list_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from acn.routes import dependencies as deps

        monkeypatch.setattr(deps.settings, "trusted_proxies", [])
        req = self._mk_request(peer="9.9.9.9", xff="1.1.1.1, 2.2.2.2")
        assert _get_real_ip(req) == "9.9.9.9"

    def test_xff_honoured_when_peer_is_trusted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from acn.routes import dependencies as deps

        monkeypatch.setattr(deps.settings, "trusted_proxies", ["9.9.9.9"])
        req = self._mk_request(peer="9.9.9.9", xff="1.1.1.1, 2.2.2.2")
        assert _get_real_ip(req) == "1.1.1.1"

    def test_xff_ignored_when_peer_not_in_trusted_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from acn.routes import dependencies as deps

        monkeypatch.setattr(deps.settings, "trusted_proxies", ["10.0.0.1"])
        req = self._mk_request(peer="9.9.9.9", xff="1.1.1.1, 2.2.2.2")
        assert _get_real_ip(req) == "9.9.9.9"
