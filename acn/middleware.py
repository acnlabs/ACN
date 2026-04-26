"""ACN HTTP middleware.

Exposes:

- :class:`BodySizeLimitMiddleware` — request-body size cap (security audit H6).
- :class:`SecurityHeadersMiddleware` — baseline response-header hardening
  (security audit M11).

Both are pure ASGI middleware (not ``starlette.middleware.base.BaseHTTPMiddleware``)
because the latter buffers the entire body before invoking the downstream app,
which defeats streaming guards.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog  # type: ignore[import-untyped]

logger = structlog.get_logger()


_RECEIVE = Callable[[], Awaitable[dict[str, Any]]]
_SEND = Callable[[dict[str, Any]], Awaitable[None]]


class BodySizeLimitMiddleware:
    """ASGI middleware that rejects requests whose body exceeds ``max_bytes``.

    Two-stage check:

    1. **Header pre-check** — if a ``Content-Length`` header is present and
       larger than ``max_bytes`` (or malformed), respond ``413`` immediately
       *before* the downstream app sees the request.  This is the common
       case: ``curl``, ``requests``, ``httpx``, browsers, and most agents
       set Content-Length automatically.
    2. **Streaming guard** — for chunked uploads (no Content-Length), wrap
       ``receive`` and accumulate bytes per chunk; once the total exceeds
       ``max_bytes`` we return ``http.disconnect`` so the downstream app
       aborts.  We can't cleanly inject a 413 here because the response
       headers may already have been sent, so disconnect is the safe
       conservative choice — the global 500 handler then masks any internal
       ``ClientDisconnect`` traceback.

    The header pre-check covers ~all real-world attackers and well-behaved
    clients; the streaming guard is defence-in-depth against an attacker
    who deliberately omits Content-Length to try to slip past stage 1.
    """

    def __init__(
        self,
        app: Any,
        max_bytes: int,
        *,
        cors_allow_origins: list[str] | None = None,
    ) -> None:
        """Construct the body-size limiter.

        ``cors_allow_origins`` is a (lower-cased) list mirroring
        ``settings.cors_origins``.  When the rejection path needs to send a
        413 it can echo the request's ``Origin`` header back as
        ``Access-Control-Allow-Origin`` if (and only if) it appears in this
        list (or the list is the wildcard ``["*"]``).  This is here purely
        so that browsers see the proper 413 instead of a generic CORS
        error — the security check itself doesn't depend on it.
        """

        if max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")
        self.app = app
        self.max_bytes = max_bytes
        self.cors_allow_origins = cors_allow_origins or []

    # Methods we wrap ``receive`` for. GET/HEAD/DELETE/OPTIONS aren't supposed
    # to carry meaningful bodies — wrapping their receive is pure overhead —
    # but they CAN still smuggle a giant Content-Length header (some
    # buggy/hostile clients do), so the *header* pre-check below runs for
    # every method.
    _STREAMING_METHODS = frozenset({"POST", "PUT", "PATCH"})

    async def __call__(self, scope: dict[str, Any], receive: _RECEIVE, send: _SEND) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    await self._reject(send, scope, reason="malformed_content_length")
                    return
                # Negative or oversized declared length: reject. Negative
                # was the audit-round-2 finding — ``int(b"-1")`` happily
                # parses but ``-1 > max_bytes`` is False, which would have
                # let a hostile/buggy client smuggle past the pre-check
                # entirely. RFC 7230 forbids negative Content-Length, so
                # treating it as malformed is correct.
                if declared < 0 or declared > self.max_bytes:
                    await self._reject(
                        send, scope, reason="content_length_invalid", declared=declared
                    )
                    return
                break

        if method not in self._STREAMING_METHODS:
            await self.app(scope, receive, send)
            return

        received = 0
        exceeded = False

        async def guarded_receive() -> dict[str, Any]:
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            msg = await receive()
            if msg.get("type") == "http.request":
                chunk = msg.get("body", b"") or b""
                received += len(chunk)
                if received > self.max_bytes:
                    exceeded = True
                    logger.warning(
                        "body_size_streaming_exceeded",
                        path=scope.get("path"),
                        method=scope.get("method"),
                        received=received,
                        max_bytes=self.max_bytes,
                    )
                    return {"type": "http.disconnect"}
            return msg

        await self.app(scope, guarded_receive, send)

    async def _reject(
        self,
        send: _SEND,
        scope: dict[str, Any],
        *,
        reason: str,
        declared: int | None = None,
    ) -> None:
        logger.warning(
            "body_size_rejected",
            path=scope.get("path"),
            method=scope.get("method"),
            reason=reason,
            declared=declared,
            max_bytes=self.max_bytes,
        )
        body = (
            b'{"detail":"Request body too large.","max_bytes":'
            + str(self.max_bytes).encode()
            + b"}"
        )
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        # Echo CORS headers when allowed — without these, browsers report a
        # generic CORS error and the developer never sees the 413, which
        # turns "your payload is too big" into a frustrating debugging
        # session.  We never wildcard back when credentials are involved
        # (CORS spec); we only echo the exact Origin.
        origin = self._get_origin(scope)
        if origin and self._origin_allowed(origin):
            headers.append((b"access-control-allow-origin", origin.encode()))
            headers.append((b"vary", b"Origin"))
            headers.append((b"access-control-allow-credentials", b"true"))
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _get_origin(scope: dict[str, Any]) -> str | None:
        for name, value in scope.get("headers", []):
            if name == b"origin":
                try:
                    return value.decode("latin-1")
                except Exception:
                    return None
        return None

    def _origin_allowed(self, origin: str) -> bool:
        if not self.cors_allow_origins:
            return False
        if "*" in self.cors_allow_origins:
            return True
        return origin in self.cors_allow_origins


class SecurityHeadersMiddleware:
    """Inject a baseline of response headers on every HTTP response.

    Why ASGI-level (not BaseHTTPMiddleware): we want the headers to land on
    *every* response — including 4xx/5xx generated by other middleware
    (rate-limit 429, body-cap 413, exception-handler 500), static handlers,
    and the OPTIONS preflight responses CORSMiddleware emits. Wrapping the
    ASGI ``send`` is the only place we can intercept all of them uniformly.

    What we add (security-audit M11):

    - ``X-Content-Type-Options: nosniff``
        Stops browsers from MIME-sniffing the body and re-categorising
        application/json as something exploitable. Cheap, universally safe.
    - ``X-Frame-Options: DENY``
        Belt-and-braces clickjacking guard. ACN's API never embeds in an
        iframe, so DENY is the right call (overridable via ``frame_options``
        ctor arg if a future docs page genuinely needs framing).
    - ``Referrer-Policy: strict-origin-when-cross-origin``
        Limits the URL leaked through ``Referer`` when API consumers click
        out — full URL same-origin, origin-only cross-origin, nothing on
        downgrade. Matches modern browser defaults but pins it explicitly.
    - ``Permissions-Policy`` (denylist of camera/microphone/geolocation/
        payment/usb)
        Defence-in-depth against any HTML doc paths (``/docs``, ``/redoc``)
        ever inadvertently embedding third-party widgets that try to claim
        sensor permissions. Effectively a no-op on JSON responses, free.
    - ``Strict-Transport-Security`` (production only — gated on ``hsts``)
        Forces HTTPS for the configured ``max-age``. We don't ship HSTS in
        dev mode because LocalStack/dev rigs run over plain HTTP. The
        ``api.py`` wiring decides whether to enable it based on
        ``settings.dev_mode`` and ``gateway_base_url``.

    What we deliberately do NOT add:

    - ``Content-Security-Policy``: ACN is a JSON API; CSP is meaningful for
        HTML and adds noise (every ``/docs`` and ``/redoc`` page would need
        bespoke directives for Swagger UI's inline scripts). The handful of
        HTML pages we serve are static FastAPI scaffolding; if we ever
        inline custom HTML, revisit.
    - ``Cross-Origin-{Opener,Embedder}-Policy``: same reason — only
        meaningful for HTML hosting cross-origin compute.

    Headers are applied AFTER the downstream app sets its own headers so
    we never clobber an intentional override (we use ``setdefault`` style
    via header-name presence check).
    """

    _DEFAULT_HEADERS: tuple[tuple[bytes, bytes], ...] = (
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"strict-origin-when-cross-origin"),
        # Subset of Permissions-Policy we actually want denied. The empty
        # parens mean "no origin allowed", including the document itself.
        (
            b"permissions-policy",
            b"camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        ),
    )

    def __init__(
        self,
        app: Any,
        *,
        hsts: bool = False,
        hsts_max_age: int = 31_536_000,  # 1 year, the OWASP/IETF recommendation
        frame_options: bytes = b"DENY",
    ) -> None:
        self.app = app
        self._headers: list[tuple[bytes, bytes]] = [
            (name, value)
            for name, value in self._DEFAULT_HEADERS
            if name != b"x-frame-options"
        ]
        self._headers.append((b"x-frame-options", frame_options))
        if hsts:
            # ``includeSubDomains`` is safe here: ACN is the apex API host
            # and we don't host non-TLS subdomains. ``preload`` is a
            # deliberate omission — HSTS preload requires a separate
            # registration step at hstspreload.org with browser vendors,
            # which is an explicit operational decision, not a default.
            self._headers.append(
                (
                    b"strict-transport-security",
                    f"max-age={int(hsts_max_age)}; includeSubDomains".encode(),
                )
            )

    async def __call__(self, scope: dict[str, Any], receive: _RECEIVE, send: _SEND) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                existing = message.get("headers") or []
                # Build a name set (bytes, ASCII-lower) so we don't clobber
                # an explicit header set by the downstream app — e.g. if a
                # specific endpoint deliberately serves
                # ``X-Frame-Options: SAMEORIGIN`` for an HTML preview.
                existing_names = {
                    (name.lower() if isinstance(name, bytes) else str(name).lower().encode())
                    for name, _value in existing
                }
                merged = list(existing)
                for name, value in self._headers:
                    if name not in existing_names:
                        merged.append((name, value))
                message = {**message, "headers": merged}
            await send(message)

        await self.app(scope, receive, send_with_headers)
