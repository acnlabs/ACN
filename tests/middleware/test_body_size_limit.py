"""Tests for :class:`acn.middleware.BodySizeLimitMiddleware` (security audit H6).

Why these tests matter:

* The middleware is the *only* layer that bounds unbounded ``dict`` fields
  exposed by the API surface (``message``, ``metadata``, ``ui_spec``,
  ``agent_card``).  Skipping it would silently re-open the H6 DoS hole.
* It also has to *not* interfere with normal small requests, ``GET``
  traffic, or non-HTTP scopes (e.g. websocket / lifespan), so each of those
  paths is exercised here.

The tests drive the middleware directly through the ASGI contract — no
FastAPI app or HTTP server is involved.  This keeps them fast, hermetic,
and decoupled from the rest of the stack so a regression in the
middleware shows up as a focused failure here instead of a confusing
crash three layers downstream.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from acn.middleware import BodySizeLimitMiddleware


class _DownstreamApp:
    """Minimal ASGI app: drains the request body and echoes its size."""

    def __init__(self) -> None:
        self.calls = 0
        self.last_body: bytes = b""

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        self.calls += 1
        body = b""
        more = True
        while more:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                break
            body += msg.get("body", b"") or b""
            more = msg.get("more_body", False)
        self.last_body = body
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps({"received": len(body)}).encode(),
            }
        )


def _http_scope(method: str, *, headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": "/api/v1/foo",
        "headers": headers or [],
    }


async def _drive(
    middleware: BodySizeLimitMiddleware,
    scope: dict[str, Any],
    body_chunks: list[bytes],
) -> tuple[list[dict[str, Any]], bool]:
    """Run the middleware against a list of body chunks and capture send events.

    Returns (sent_messages, downstream_was_called).
    """

    sent: list[dict[str, Any]] = []
    iterator = iter(body_chunks)

    async def receive() -> dict[str, Any]:
        try:
            chunk = next(iterator)
        except StopIteration:
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    await middleware(scope, receive, send)
    return sent, getattr(middleware, "_test_downstream_called", False)


# ---------------------------------------------------------------------------
# Header pre-check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_length_under_limit_passes_through() -> None:
    """A POST with Content-Length within the cap must reach the downstream app."""

    app = _DownstreamApp()
    mw = BodySizeLimitMiddleware(app, max_bytes=1024)

    scope = _http_scope(
        "POST",
        headers=[(b"content-length", b"100"), (b"content-type", b"application/json")],
    )
    body = b"x" * 100
    sent, _ = await _drive(mw, scope, [body])

    assert app.calls == 1, "downstream app should have been invoked"
    assert app.last_body == body
    assert sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_content_length_over_limit_rejected_immediately() -> None:
    """Header pre-check: oversized declared body returns 413 without invoking app."""

    app = _DownstreamApp()
    mw = BodySizeLimitMiddleware(app, max_bytes=1024)

    scope = _http_scope("POST", headers=[(b"content-length", b"5000")])
    sent, _ = await _drive(mw, scope, [b"y" * 5000])

    assert app.calls == 0, "downstream must NOT be called when header pre-check trips"
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413
    body_msg = next(m for m in sent if m["type"] == "http.response.body")
    parsed = json.loads(body_msg["body"])
    assert parsed["detail"] == "Request body too large."
    assert parsed["max_bytes"] == 1024


@pytest.mark.asyncio
async def test_malformed_content_length_rejected() -> None:
    """A non-integer Content-Length is rejected — defensive against header smuggling."""

    app = _DownstreamApp()
    mw = BodySizeLimitMiddleware(app, max_bytes=1024)

    scope = _http_scope("POST", headers=[(b"content-length", b"not-a-number")])
    sent, _ = await _drive(mw, scope, [b""])

    assert app.calls == 0
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_content_length_exactly_at_limit_passes() -> None:
    """Boundary: Content-Length == max_bytes is allowed (cap is exclusive ``>``)."""

    app = _DownstreamApp()
    mw = BodySizeLimitMiddleware(app, max_bytes=100)

    scope = _http_scope("POST", headers=[(b"content-length", b"100")])
    body = b"z" * 100
    sent, _ = await _drive(mw, scope, [body])

    assert app.calls == 1
    assert sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_negative_content_length_rejected() -> None:
    """Negative Content-Length is rejected — round-2 audit finding.

    ``int(b"-1")`` parses fine but ``-1 > max_bytes`` is False, so a naive
    ``> max_bytes``-only check would let a hostile client smuggle a
    declared-negative Content-Length past the pre-check (and rely on
    Uvicorn's downstream behaviour, which historically has varied).
    RFC 7230 forbids negative Content-Length: we treat it as malformed.
    """

    app = _DownstreamApp()
    mw = BodySizeLimitMiddleware(app, max_bytes=1024)

    scope = _http_scope("POST", headers=[(b"content-length", b"-1")])
    sent, _ = await _drive(mw, scope, [b"abc"])

    assert app.calls == 0, "downstream must NOT see a request with negative Content-Length"
    assert sent[0]["status"] == 413


# ---------------------------------------------------------------------------
# Streaming guard (chunked uploads with no Content-Length)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
@pytest.mark.asyncio
async def test_streaming_methods_all_get_receive_wrap(method: str) -> None:
    """Every method in ``_STREAMING_METHODS`` must run the receive wrap.

    Round-2 audit finding: only POST was directly tested against the
    streaming guard; if a future refactor accidentally drops ``PUT`` or
    ``PATCH`` from ``_STREAMING_METHODS`` (a one-character typo), the
    earlier tests would still all pass.  This parametrised test pins the
    contract: any method that *should* stream-guard, *does*.
    """

    app = _DownstreamApp()
    mw = BodySizeLimitMiddleware(app, max_bytes=1024)

    scope = _http_scope(method)  # no Content-Length → streaming path
    chunks = [b"a" * 600, b"b" * 600]  # 1200 bytes → trips guard on chunk 2
    sent, _ = await _drive(mw, scope, chunks)

    assert app.calls == 1
    assert app.last_body == b"a" * 600, (
        f"{method} must stop receiving after the size cap is exceeded"
    )


@pytest.mark.asyncio
async def test_streaming_body_under_limit_succeeds() -> None:
    """Chunked upload (no Content-Length) totalling under the cap reaches the app."""

    app = _DownstreamApp()
    mw = BodySizeLimitMiddleware(app, max_bytes=1024)

    scope = _http_scope("POST")  # no Content-Length header
    chunks = [b"a" * 256, b"b" * 256, b"c" * 256]  # 768 bytes total
    sent, _ = await _drive(mw, scope, chunks)

    assert app.calls == 1
    assert app.last_body == b"".join(chunks)
    assert sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_streaming_body_over_limit_disconnects() -> None:
    """Chunked upload overflowing mid-stream gets a disconnect — defence-in-depth.

    The downstream app is invoked (header pre-check is moot, no
    Content-Length), but the wrapped ``receive`` returns ``http.disconnect``
    once the running total exceeds the cap so the app sees a truncated
    body and should abort.  We don't try to inject a 413 here because
    the response start may already be in flight.

    Tight assertion: ``last_body`` must equal exactly the first chunk
    (600 bytes) — chunk 2 takes us past 1024 so the wrapped receive must
    return disconnect *before* the second chunk is delivered.  An
    earlier loose ``<=1624`` bound let a regression slip through where
    the disconnect fired one chunk too late.
    """

    app = _DownstreamApp()
    mw = BodySizeLimitMiddleware(app, max_bytes=1024)

    scope = _http_scope("POST")
    chunks = [b"a" * 600, b"b" * 600]  # 1200 bytes total → trips on chunk 2
    sent, _ = await _drive(mw, scope, chunks)

    assert app.calls == 1
    assert app.last_body == b"a" * 600, (
        "downstream must see only the first chunk before disconnect"
    )


# ---------------------------------------------------------------------------
# Method gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "HEAD", "DELETE", "OPTIONS"])
@pytest.mark.asyncio
async def test_safe_methods_skip_streaming_wrap_but_still_header_check(method: str) -> None:
    """Safe methods still get the Content-Length pre-check.

    Audit-finding refinement: an earlier draft skipped *all* size logic for
    GET/HEAD/DELETE/OPTIONS, which let a malicious client send
    ``GET /x HTTP/1.1\\r\\nContent-Length: 9999999999\\r\\n\\r\\n``
    and bypass the cap entirely.  The pre-check is O(1) so we keep it for
    every method; only the (more expensive) ``receive`` wrap is gated on
    POST/PUT/PATCH.
    """

    app = _DownstreamApp()
    mw = BodySizeLimitMiddleware(app, max_bytes=10)

    over = _http_scope(method, headers=[(b"content-length", b"99999")])
    sent_over, _ = await _drive(mw, over, [b""])
    assert app.calls == 0, f"{method} with oversized Content-Length must be rejected"
    assert sent_over[0]["status"] == 413

    under = _http_scope(method, headers=[(b"content-length", b"5")])
    sent_under, _ = await _drive(mw, under, [b"hello"])
    assert app.calls == 1, f"{method} with small Content-Length must pass through"
    assert sent_under[0]["status"] == 200


# ---------------------------------------------------------------------------
# Non-HTTP scopes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_http_scope_passes_through() -> None:
    """Lifespan / websocket scopes must be untouched — H6 is HTTP-only."""

    _ = _DownstreamApp()  # constructed only to assert non-HTTP scopes never reach it

    captured: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "lifespan.startup"}

    async def send(msg: dict[str, Any]) -> None:
        captured.append(msg)

    async def lifespan_app(scope, receive, send):  # type: ignore[no-untyped-def]
        await send({"type": "lifespan.startup.complete"})

    mw_ls = BodySizeLimitMiddleware(lifespan_app, max_bytes=10)
    await mw_ls({"type": "lifespan"}, receive, send)

    assert captured == [{"type": "lifespan.startup.complete"}]


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_max_bytes_must_be_positive() -> None:
    """Guard against accidental ``max_bytes=0`` config — would block all writes."""

    with pytest.raises(ValueError, match="max_bytes"):
        BodySizeLimitMiddleware(lambda *_: None, max_bytes=0)
    with pytest.raises(ValueError, match="max_bytes"):
        BodySizeLimitMiddleware(lambda *_: None, max_bytes=-1)


# ---------------------------------------------------------------------------
# CORS echo on 413 (round-2 audit)
# ---------------------------------------------------------------------------


def _headers_dict(msg: dict[str, Any]) -> dict[bytes, bytes]:
    return dict(msg.get("headers", []))


@pytest.mark.asyncio
async def test_413_echoes_cors_headers_when_origin_allowed() -> None:
    """413 must carry ``Access-Control-Allow-Origin`` for allowed origins.

    Round-2 audit finding: without this, a browser whose request hits the
    body cap sees a generic CORS error instead of a clean 413, and the
    developer wastes time chasing imaginary CORS misconfigurations.
    """

    app = _DownstreamApp()
    mw = BodySizeLimitMiddleware(
        app, max_bytes=10, cors_allow_origins=["https://app.example.com"]
    )
    scope = _http_scope(
        "POST",
        headers=[
            (b"content-length", b"99999"),
            (b"origin", b"https://app.example.com"),
        ],
    )
    sent, _ = await _drive(mw, scope, [b""])

    assert sent[0]["status"] == 413
    headers = _headers_dict(sent[0])
    assert headers.get(b"access-control-allow-origin") == b"https://app.example.com"
    assert headers.get(b"access-control-allow-credentials") == b"true"
    assert headers.get(b"vary") == b"Origin"


@pytest.mark.asyncio
async def test_413_omits_cors_headers_when_origin_not_allowed() -> None:
    """Disallowed origins must NOT receive an Allow-Origin echo — that
    would itself be a CORS misconfiguration."""

    app = _DownstreamApp()
    mw = BodySizeLimitMiddleware(
        app, max_bytes=10, cors_allow_origins=["https://app.example.com"]
    )
    scope = _http_scope(
        "POST",
        headers=[
            (b"content-length", b"99999"),
            (b"origin", b"https://attacker.example.com"),
        ],
    )
    sent, _ = await _drive(mw, scope, [b""])

    assert sent[0]["status"] == 413
    headers = _headers_dict(sent[0])
    assert b"access-control-allow-origin" not in headers


@pytest.mark.asyncio
async def test_413_with_wildcard_origin_echoes_request_origin() -> None:
    """Wildcard ``["*"]`` echoes the request Origin (never bare ``*``).

    Echoing bare ``*`` together with ``Allow-Credentials: true`` is
    forbidden by the CORS spec; we always reflect the actual Origin to
    keep the response valid.
    """

    app = _DownstreamApp()
    mw = BodySizeLimitMiddleware(app, max_bytes=10, cors_allow_origins=["*"])
    scope = _http_scope(
        "POST",
        headers=[
            (b"content-length", b"99999"),
            (b"origin", b"https://anything.example.com"),
        ],
    )
    sent, _ = await _drive(mw, scope, [b""])

    headers = _headers_dict(sent[0])
    assert headers.get(b"access-control-allow-origin") == b"https://anything.example.com"


@pytest.mark.asyncio
async def test_413_no_origin_header_no_cors_echo() -> None:
    """Non-browser clients (no Origin header) get a plain 413."""

    app = _DownstreamApp()
    mw = BodySizeLimitMiddleware(
        app, max_bytes=10, cors_allow_origins=["https://app.example.com"]
    )
    scope = _http_scope("POST", headers=[(b"content-length", b"99999")])
    sent, _ = await _drive(mw, scope, [b""])

    headers = _headers_dict(sent[0])
    assert b"access-control-allow-origin" not in headers
