"""Tests for :class:`acn.middleware.SecurityHeadersMiddleware` (security audit M11).

Why these tests matter:

* The middleware is the only place ACN injects baseline browser hardening
  headers (``X-Content-Type-Options``, ``X-Frame-Options``,
  ``Referrer-Policy``, ``Permissions-Policy``, ``Strict-Transport-Security``).
  A regression silently turns off the entire layer of defence in one
  ``add_middleware`` line.
* The middleware MUST decorate **every** response — including ones produced
  by other middleware (rate-limit 429, body-cap 413, exception handler
  500) — because attackers care most about the responses we emit on edge
  cases, not the happy-path JSON.
* It MUST NOT clobber a header the downstream app set deliberately. We
  pin both directions (added when missing, preserved when present).

Driven through the raw ASGI contract — no FastAPI / HTTP server. Same
philosophy as ``test_body_size_limit.py``: keep the unit hermetic so a
regression here doesn't masquerade as a confusing failure three layers
downstream.
"""

from __future__ import annotations

from typing import Any

import pytest

from acn.middleware import SecurityHeadersMiddleware

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────


class _DownstreamApp:
    """Minimal ASGI app emitting a response with optional pre-set headers."""

    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b'{"ok":true}',
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.extra_headers = extra_headers or []
        self.calls = 0

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        self.calls += 1
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(self.body)).encode()),
        ]
        headers.extend(self.extra_headers)
        await send(
            {"type": "http.response.start", "status": self.status, "headers": headers}
        )
        await send({"type": "http.response.body", "body": self.body})


class _Capture:
    """Captures the messages an ASGI app emits via ``send``."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    @property
    def start(self) -> dict[str, Any]:
        for m in self.messages:
            if m.get("type") == "http.response.start":
                return m
        raise AssertionError("no http.response.start message captured")

    def header(self, name: bytes) -> bytes | None:
        for k, v in self.start.get("headers", []):
            if k == name:
                return v
        return None

    def headers(self, name: bytes) -> list[bytes]:
        return [v for k, v in self.start.get("headers", []) if k == name]


def _http_scope(method: str = "GET", path: str = "/x") -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
    }


async def _noop_receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


# ─────────────────────────────────────────────
# Defaults: every response gets the baseline set
# ─────────────────────────────────────────────


class TestBaselineHeadersInjected:
    @pytest.mark.asyncio
    async def test_all_default_headers_present_on_200(self):
        downstream = _DownstreamApp()
        mw = SecurityHeadersMiddleware(downstream)
        capture = _Capture()
        await mw(_http_scope(), _noop_receive, capture)

        assert capture.header(b"x-content-type-options") == b"nosniff"
        assert capture.header(b"x-frame-options") == b"DENY"
        assert capture.header(b"referrer-policy") == b"strict-origin-when-cross-origin"
        # Permissions-Policy: pin the exact denylist so a future loosening
        # is visible in diff review.
        pp = capture.header(b"permissions-policy")
        assert pp is not None
        assert b"camera=()" in pp
        assert b"microphone=()" in pp
        assert b"geolocation=()" in pp
        assert b"payment=()" in pp
        assert b"usb=()" in pp

    @pytest.mark.asyncio
    async def test_no_hsts_by_default(self):
        """HSTS is opt-in (production only). Default off so dev rigs over
        plain HTTP don't lock browsers into TLS-only access."""
        downstream = _DownstreamApp()
        mw = SecurityHeadersMiddleware(downstream)
        capture = _Capture()
        await mw(_http_scope(), _noop_receive, capture)
        assert capture.header(b"strict-transport-security") is None

    @pytest.mark.asyncio
    async def test_hsts_emitted_when_enabled(self):
        downstream = _DownstreamApp()
        mw = SecurityHeadersMiddleware(downstream, hsts=True)
        capture = _Capture()
        await mw(_http_scope(), _noop_receive, capture)
        hsts = capture.header(b"strict-transport-security")
        assert hsts is not None
        # max-age must be present and a positive number; includeSubDomains
        # is required for the apex+subdomain coverage we documented.
        assert hsts.startswith(b"max-age=")
        assert b"includeSubDomains" in hsts
        # We deliberately do NOT add ``preload``; assert that's still true
        # so a future PR doesn't silently flip the preload bit (which has
        # operational consequences — browser preload list registration).
        assert b"preload" not in hsts

    @pytest.mark.asyncio
    async def test_hsts_max_age_configurable(self):
        downstream = _DownstreamApp()
        mw = SecurityHeadersMiddleware(downstream, hsts=True, hsts_max_age=600)
        capture = _Capture()
        await mw(_http_scope(), _noop_receive, capture)
        hsts = capture.header(b"strict-transport-security")
        assert hsts == b"max-age=600; includeSubDomains"


# ─────────────────────────────────────────────
# Status-code coverage: hardening must apply to non-2xx too
# ─────────────────────────────────────────────


class TestAppliesToAllStatusCodes:
    """The whole point of M11 is to harden *every* response.

    Attackers care about how the API behaves on edge cases (rate-limit
    429, body-cap 413, exception 500) at least as much as the happy path.
    A regression that only hardens 2xx would be silently weaker than no
    middleware at all — operators would see hardening on the docs page
    and assume it covers everything.
    """

    @pytest.mark.parametrize("status", [200, 400, 401, 403, 404, 413, 429, 500, 503])
    @pytest.mark.asyncio
    async def test_baseline_present(self, status):
        downstream = _DownstreamApp(status=status, body=b'{"detail":"x"}')
        mw = SecurityHeadersMiddleware(downstream)
        capture = _Capture()
        await mw(_http_scope(), _noop_receive, capture)

        assert capture.start["status"] == status
        assert capture.header(b"x-content-type-options") == b"nosniff"
        assert capture.header(b"x-frame-options") == b"DENY"


# ─────────────────────────────────────────────
# Override semantics: don't clobber downstream-set headers
# ─────────────────────────────────────────────


class TestDoesNotClobberDownstream:
    @pytest.mark.asyncio
    async def test_downstream_x_frame_options_preserved(self):
        """If a route deliberately sets a non-default X-Frame-Options
        (e.g. SAMEORIGIN for an inline preview), middleware must respect
        that — it's the route author's explicit decision."""
        downstream = _DownstreamApp(
            extra_headers=[(b"x-frame-options", b"SAMEORIGIN")],
        )
        mw = SecurityHeadersMiddleware(downstream)
        capture = _Capture()
        await mw(_http_scope(), _noop_receive, capture)

        all_xfo = capture.headers(b"x-frame-options")
        assert all_xfo == [b"SAMEORIGIN"], (
            "Downstream X-Frame-Options must not be duplicated or overwritten."
        )

    @pytest.mark.asyncio
    async def test_downstream_referrer_policy_preserved(self):
        downstream = _DownstreamApp(
            extra_headers=[(b"referrer-policy", b"no-referrer")],
        )
        mw = SecurityHeadersMiddleware(downstream)
        capture = _Capture()
        await mw(_http_scope(), _noop_receive, capture)
        assert capture.headers(b"referrer-policy") == [b"no-referrer"]

    @pytest.mark.asyncio
    async def test_case_insensitive_dedup(self):
        """ASGI permits header names in any case. The override-detection
        must lowercase-compare or it'll happily duplicate ``X-Frame-Options``
        twice (once as the literal pre-set bytes, once as our default)."""
        downstream = _DownstreamApp(
            extra_headers=[(b"X-Frame-Options", b"SAMEORIGIN")],
        )
        mw = SecurityHeadersMiddleware(downstream)
        capture = _Capture()
        await mw(_http_scope(), _noop_receive, capture)
        # Compare lower-cased only — Starlette/Uvicorn preserve case but
        # treat as case-insensitive per HTTP. We must not emit *two*
        # X-Frame-Options entries to a browser.
        xfo_count = sum(
            1
            for k, _ in capture.start["headers"]
            if k.lower() == b"x-frame-options"
        )
        assert xfo_count == 1


# ─────────────────────────────────────────────
# Non-HTTP scopes pass through untouched
# ─────────────────────────────────────────────


class TestNonHttpScope:
    @pytest.mark.asyncio
    async def test_websocket_scope_pass_through(self):
        """Websocket handshake messages have a different envelope; we
        must not try to inject HTTP headers into them. Passing the scope
        straight through is the correct behaviour."""
        sent_messages: list[dict[str, Any]] = []

        async def downstream(scope, receive, send):  # type: ignore[no-untyped-def]
            await send({"type": "websocket.accept"})

        async def capture(message):
            sent_messages.append(message)

        async def receive():
            return {"type": "websocket.connect"}

        scope = {"type": "websocket", "path": "/ws"}
        mw = SecurityHeadersMiddleware(downstream)
        await mw(scope, receive, capture)
        # The single message we sent must be unchanged — no headers
        # injected into a non-HTTP envelope.
        assert sent_messages == [{"type": "websocket.accept"}]


# ─────────────────────────────────────────────
# Body integrity: middleware must not corrupt the response body
# ─────────────────────────────────────────────


class TestBodyUntouched:
    @pytest.mark.asyncio
    async def test_response_body_passes_through_verbatim(self):
        """Header injection must not alter content-length / body framing —
        we never re-write the body, only append to ``response.start``
        headers."""
        body = b'{"ok":true,"data":[1,2,3]}'
        downstream = _DownstreamApp(body=body)
        mw = SecurityHeadersMiddleware(downstream)
        capture = _Capture()
        await mw(_http_scope(), _noop_receive, capture)

        body_msgs = [m for m in capture.messages if m.get("type") == "http.response.body"]
        assert len(body_msgs) == 1
        assert body_msgs[0]["body"] == body
        # Content-length must still match the actual body length we sent.
        cl = capture.header(b"content-length")
        assert cl == str(len(body)).encode()
