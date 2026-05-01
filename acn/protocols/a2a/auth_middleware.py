"""A2A ``from_agent`` strong-validation middleware (Phase 2 PR #2).

Why this middleware exists
==========================

The A2A protocol entry (``POST /a2a/jsonrpc``) carries the sender's
declared identity in the JSON-RPC payload's
``params.message.metadata.from_agent`` field. Phase 1 added a defensive
sanitiser (``_safe_a2a_from_agent`` in ``server.py``) that strips
``system:`` prefixes so an external caller cannot forge the system
namespace and bypass closed policies. That was sufficient when the
only policy that depended on sender identity was "closed for everyone
except system" — non-system senders all looked the same to the policy
service.

PR #2 changes that. ``mode=allowlist`` makes the policy decision
**conditional on the sender's actual agent id**: senders on the
list go to inbox, others divert to manifest. If the A2A entry kept
trusting the unauthenticated ``from_agent`` field, any external
caller could:

1. Craft an A2A request claiming ``from_agent="alice"``.
2. Have it routed to ``bob``'s inbox if and only if ``alice`` is on
   ``bob``'s allowlist.

i.e. impersonate any allowlisted sender. Even worse, this is
silent — the request succeeds, the recipient sees a message
"from alice" that alice never sent.

This middleware closes the gap by binding ``from_agent`` to the
caller's authenticated identity (their ACN API key, the same
mechanism every REST route uses). Two enforcement modes:

* ``Authorization: Bearer <api_key>`` present → resolve the api_key
  to an ``agent_id`` via ``AgentService.get_agent_by_api_key``. If
  the request body's ``from_agent`` matches this agent_id, the
  request passes through untouched. If it doesn't match, the
  middleware returns a 403 JSON-RPC error before the request ever
  reaches the executor.
* ``Authorization`` absent → the request can still be served (e.g.
  the ``GET /a2a/.well-known/agent-card.json`` discovery path used
  by anonymous clients) but for ``POST /a2a/jsonrpc`` we **rewrite**
  ``metadata.from_agent`` to the safe fallback ``"unknown"`` so
  the downstream policy service treats it as an unauthenticated
  external caller. Existing policy modes (``open`` / ``closed`` /
  ``manifest``) remain functional in this anonymous mode; only
  ``allowlist`` is meaningfully affected because no allowlist
  contains the literal ``"unknown"`` agent id.

Implementation choice: ASGI middleware vs FastAPI dependency
============================================================

A FastAPI dependency would be the path of least resistance, but the
A2A FastAPI app is built by ``A2AFastAPIApplication`` from the
``a2a-sdk`` library — its routes are not declared with our Depends
annotations and we can't easily inject. ASGI middleware sits at the
HTTP layer, before the SDK's routing, and works regardless of how
the SDK structures its endpoints. The price is that we have to
replay the request body manually after reading it (Starlette / ASGI
``receive`` is single-shot by default); the helper below caches the
body and synthesises a fresh ``receive`` for the downstream call.

Why JSON-RPC error response with HTTP 400 (PR #2 v3)
=====================================================

The A2A protocol uses JSON-RPC 2.0 over HTTP; clients expect
``{"jsonrpc": "2.0", "error": {"code": ..., "message": ...}, "id": ...}``
not a bare HTTP 401. We use JSON-RPC code ``-32600`` ("Invalid
Request") in the body with a descriptive message.

For the HTTP status we deliberately diverge from the bare JSON-RPC
"always 200" convention: a ``from_agent`` mismatch is a security
event (impersonation attempt) and we want it visible in standard
4xx/5xx access-log alerting that operators wire up by default.
JSON-RPC 2.0 §5 explicitly permits 4xx for transport-level errors;
the body still carries the structured ``error`` envelope so
spec-compliant clients keep parsing as JSON-RPC. PR #2 v3 review
P1-A2 made this swap.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

# PR #2 v3 P2-A3: switched from stdlib ``logging`` to ``structlog``.
# Other modules in the policy/allowlist call-stack
# (``policy_service.py``, ``allowlist_service.py``, ``api.py``) all
# emit structured key=value logs through ``structlog.get_logger``;
# stdlib ``logging.getLogger(...).warning(extra={...})`` would dump
# the structured fields into ``LogRecord.__dict__["extra"]`` where
# common log aggregators (Datadog, Loki) skip them silently. Using
# the same logger flavour across the whole pipeline keeps
# ``a2a_from_agent_*`` events queryable alongside policy events.
logger = structlog.get_logger(__name__)


# RFC 6750 §2.1 leaves scheme matching as SHOULD case-insensitive,
# and clients in the wild produce ``bearer <token>`` (lower) or
# ``Bearer  <token>`` (extra whitespace) often enough that strict
# parsing would silently demote authenticated calls to anonymous.
# The regex below tolerates: case-folding, leading whitespace before
# the scheme name, any amount of whitespace between scheme and token,
# and trailing whitespace/CRLF on the token. Group(1) is the api_key.
# PR #2 v3 review P1-A4 introduced this.
_BEARER_RE = re.compile(r"^\s*bearer\s+(\S+?)\s*$", re.IGNORECASE)

# JSON-RPC standard error code for "Invalid Request" — used when the
# request shape itself violates the protocol contract. We pick this
# rather than an application-defined code so generic JSON-RPC clients
# can surface the failure without bespoke handling.
_JSONRPC_INVALID_REQUEST = -32600

# Sentinel written into ``metadata.from_agent`` when the caller is
# anonymous (no Authorization header). Mirrors
# ``_A2A_SAFE_FROM_AGENT_FALLBACK`` in server.py — kept in sync via
# the constant below. Callers checking this string should use the
# import alias rather than hardcoding.
_ANONYMOUS_FROM_AGENT = "unknown"

# Routes the middleware enforces. Other A2A paths (agent card,
# health, well-known discovery) skip the auth pipeline so anonymous
# clients can still discover us. The "send" path is the only one
# that creates network-visible side effects on the recipient.
#
# PR #2 v3 P2-A1: this is matched by **exact equality**, not
# ``str.endswith``. ``endswith("/jsonrpc")`` would silently start
# enforcing any future SDK path that happens to share the suffix
# (e.g. ``/api/v1/jsonrpc``, ``/internal/jsonrpc``) — semantically
# both are JSON-RPC entries, but the caller's intent for which
# endpoints carry the from_agent contract should be explicit. New
# routes belong in this tuple deliberately, never via inheritance
# from a suffix match.
_ENFORCED_PATHS: frozenset[str] = frozenset({"/jsonrpc"})


class A2AFromAgentValidationMiddleware:
    """ASGI middleware enforcing ``from_agent`` matches the caller.

    Args:
        app: Downstream ASGI app (the FastAPI A2A app from
            ``create_a2a_app``).
        agent_lookup: Async callable that resolves an api_key to an
            ``agent_id`` or ``None``. Decoupled from
            ``AgentService`` directly so tests can inject a stub
            without standing up the whole service graph; in
            production ``api.py`` binds this to a closure around
            ``AgentService.get_agent_by_api_key``.

    Body-handling notes:
    - We only read the body if the request is a ``POST /jsonrpc``
      (the only path we enforce). Other paths are passed through
      with no body buffering — this matters for streaming
      endpoints.
    - On enforcement paths we accumulate the full body before
      proceeding. A2A JSON-RPC messages are typically <100KB, well
      below any sane limit; if a future use case streams huge
      bodies through ``/jsonrpc`` we would need to shift to a
      partial-parse approach. For now, materialising the body is
      the simpler, well-tested path.
    - The body is replayed to downstream as a single
      ``http.request`` event with ``more_body=False``. We do not
      preserve incremental delivery semantics; the SDK consumes
      the body in one shot anyway.
    """

    def __init__(
        self,
        app: Any,
        agent_lookup: Callable[[str], Awaitable[str | None]],
    ) -> None:
        self.app = app
        self._agent_lookup = agent_lookup

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        # Cheap early-out: only HTTP requests on the enforced path
        # are inspected. WebSocket / lifespan / other ASGI scopes
        # short-circuit so we never block startup or other transports.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")
        if method != "POST" or path not in _ENFORCED_PATHS:
            await self.app(scope, receive, send)
            return

        # Materialise the body. A2A JSON-RPC requests are small;
        # this won't OOM under realistic load.
        body = await _read_full_body(receive)

        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            # Let the downstream JSON-RPC handler return the
            # standard parse error — we don't want to swallow that
            # responsibility just because we touched the body.
            await self._forward(scope, receive, send, body)
            return

        # ``Authorization`` is the canonical header name; ASGI gives
        # us bytes-tuples lowercased per spec.
        api_key = _extract_bearer_token(scope.get("headers") or [])
        caller_agent_id: str | None = None
        if api_key is not None:
            try:
                caller_agent_id = await self._agent_lookup(api_key)
            except Exception as exc:  # noqa: BLE001
                # Lookup failures (Redis blip, PG down) are logged
                # and treated as "unauthenticated" — the request is
                # forwarded with from_agent rewritten to "unknown".
                # Fail-closed alternative ("reject the request")
                # would convert any auth backend hiccup into a
                # full A2A outage; we'd rather degrade to anonymous.
                logger.warning(
                    "a2a_from_agent_lookup_failed",
                    error=str(exc),
                )

        # Locate metadata.from_agent in the JSON-RPC envelope. The
        # canonical path under A2A protocol v0.3 is
        # params.message.metadata.from_agent; we also accept it at
        # params.metadata.from_agent for robustness against minor
        # SDK shape drift.
        declared = _extract_from_agent(payload)

        rewrote = False
        rejection: dict[str, Any] | None = None

        if caller_agent_id is None:
            # Anonymous caller — rewrite to the safe sentinel so
            # the downstream policy gate has no ambiguity. Any
            # ``from_agent`` value the client put becomes
            # "unknown" — we are NOT trying to fail the request,
            # we are documenting that the value is unverified.
            if declared is not None and declared != _ANONYMOUS_FROM_AGENT:
                _rewrite_from_agent(payload, _ANONYMOUS_FROM_AGENT)
                rewrote = True
        elif declared is None:
            # Authenticated caller, but didn't declare from_agent.
            # Backfill so policy decisions get the right identity
            # (this is the common case — most A2A clients haven't
            # been updated to set metadata.from_agent yet).
            _rewrite_from_agent(payload, caller_agent_id)
            rewrote = True
        elif declared != caller_agent_id:
            # Mismatch — the heart of the security check. The
            # request is rejected with a JSON-RPC 403-equivalent
            # error before any side-effect can fire. We log at
            # WARNING (operationally interesting; expected to be
            # rare) with both ids so on-call can spot a sustained
            # pattern of impersonation attempts.
            logger.warning(
                "a2a_from_agent_mismatch",
                declared=declared,
                authenticated=caller_agent_id,
                path=path,
            )
            rejection = _build_rejection(
                rpc_id=payload.get("id"),
                authenticated=caller_agent_id,
                declared=declared,
            )

        if rejection is not None:
            await _send_jsonrpc_error(send, rejection)
            return

        # Re-serialise the body if we rewrote anything; otherwise
        # forward the original bytes verbatim to avoid touching the
        # downstream's hash-of-body checks (none today, but cheap
        # to preserve).
        forwarded_body = json.dumps(payload).encode("utf-8") if rewrote else body

        await self._forward(scope, receive, send, forwarded_body)

    async def _forward(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
        body: bytes,
    ) -> None:
        """Send ``body`` downstream as a single http.request event.

        After the synthesised http.request has been delivered once,
        any further ``receive()`` call from downstream is delegated
        to the **original** ASGI ``receive`` (PR #2 v3 P2-A2). The
        upstream server holds the real client connection: it will
        block this coroutine until the client actually disconnects
        (yielding ``http.disconnect``), which is the proper ASGI
        contract for "no more body, but the connection is still
        open". The previous implementation returned a synthesised
        ``http.disconnect`` on the second call, which falsely told
        downstream that the client had hung up.
        """
        # Adjust Content-Length if we rewrote the body. ASGI
        # downstream apps usually re-derive it but keeping headers
        # honest avoids surprising HTTP/1.1 reverse proxies.
        scope = _with_content_length(scope, len(body))

        sent = False

        async def replay() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": False,
                }
            # Body already replayed once. Hand off to the real
            # ``receive`` so http.disconnect propagates from the
            # upstream server when the client actually drops, rather
            # than us synthesising it prematurely.
            return await receive()

        await self.app(scope, replay, send)


# ----------------------------------------------------------------------
# Helpers — module-private; tested via the middleware's contract tests.
# ----------------------------------------------------------------------


async def _read_full_body(
    receive: Callable[[], Awaitable[dict[str, Any]]],
) -> bytes:
    """Drain http.request events until ``more_body`` is False.

    Concatenates body chunks. We do not enforce a size cap here —
    the framework / proxy in front of us is expected to do so. (A
    bare ``A2AFromAgentValidationMiddleware`` in a test harness
    has no upstream limits, but tests construct synthetic small
    bodies.)
    """
    chunks: list[bytes] = []
    while True:
        event = await receive()
        if event["type"] == "http.request":
            chunks.append(event.get("body", b""))
            if not event.get("more_body", False):
                break
        elif event["type"] == "http.disconnect":
            break
    return b"".join(chunks)


def _extract_bearer_token(headers: list[tuple[bytes, bytes]]) -> str | None:
    """Read the api_key portion of ``Authorization: Bearer <key>``.

    Tolerates real-world variation in how clients construct the
    header (PR #2 v3 review P1-A4):

    * Case-insensitive scheme: ``bearer`` / ``Bearer`` / ``BEARER``
      all match (RFC 6750 §2.1 SHOULD).
    * Multiple whitespace characters between scheme and token.
    * Leading or trailing whitespace / CRLF on the line.

    Strict matchers (``startswith("Bearer ")``) silently demote
    these to anonymous, which would falsely apply the
    ``"unknown"`` rewrite to authenticated callers. The regex
    constructed at module scope (``_BEARER_RE``) handles all three.

    Returns ``None`` for missing headers, malformed UTF-8, or
    schemes other than ``Bearer``; the caller treats them all as
    "unauthenticated" → ``from_agent`` rewritten to ``"unknown"``.
    """
    for name, value in headers:
        if name.lower() != b"authorization":
            continue
        try:
            decoded = value.decode("latin-1")
        except UnicodeDecodeError:
            return None
        match = _BEARER_RE.match(decoded)
        return match.group(1) if match else None
    return None


def _extract_from_agent(payload: dict[str, Any]) -> str | None:
    """Pull ``metadata.from_agent`` from the JSON-RPC envelope.

    Looks in two locations to be tolerant of SDK shape drift:

    1. ``params.message.metadata.from_agent`` — the canonical path
       under A2A protocol v0.3 (matches the executor's own
       ``context.metadata`` extraction). This is the primary path.
    2. ``params.metadata.from_agent`` — fallback for older shapes.

    Returns ``None`` when neither path resolves to a string. We
    deliberately do NOT raise on malformed shapes because the
    downstream JSON-RPC handler will return a clean parse error;
    short-circuiting here would just duplicate that response.
    """
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return None
    message = params.get("message") or {}
    if isinstance(message, dict):
        meta = message.get("metadata") or {}
        if isinstance(meta, dict):
            value = meta.get("from_agent")
            if isinstance(value, str):
                return value
    meta = params.get("metadata") or {}
    if isinstance(meta, dict):
        value = meta.get("from_agent")
        if isinstance(value, str):
            return value
    return None


def _rewrite_from_agent(payload: dict[str, Any], new_value: str) -> None:
    """Mutate the payload to set ``metadata.from_agent`` = ``new_value``.

    Writes to the canonical path
    (``params.message.metadata.from_agent``) and creates intermediate
    dicts as needed. We do NOT also update the fallback path —
    duplicating the value across two metadata buckets would make
    later debugging confusing. The executor reads the canonical
    path first.
    """
    params = payload.setdefault("params", {})
    if not isinstance(params, dict):
        return
    message = params.setdefault("message", {})
    if not isinstance(message, dict):
        return
    metadata = message.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        return
    metadata["from_agent"] = new_value


def _build_rejection(
    rpc_id: Any,
    authenticated: str,
    declared: str,
) -> dict[str, Any]:
    """Construct the JSON-RPC error envelope for a from_agent mismatch.

    The error message is intentionally informative (includes both
    ids) — at this layer the caller authenticated successfully, so
    leaking their own agent_id back to them is fine. We do NOT
    leak the declared id back if it is from a different agent
    namespace; it's the caller's own input, so they already know
    it.
    """
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {
            "code": _JSONRPC_INVALID_REQUEST,
            "message": (
                f"from_agent mismatch: authenticated as {authenticated!r} "
                f"but message declared {declared!r}; refusing to forward to "
                f"avoid impersonation"
            ),
        },
    }


async def _send_jsonrpc_error(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    body: dict[str, Any],
) -> None:
    """Emit the JSON-RPC error response with HTTP 400.

    PR #2 v3 review P1-A2: status was 200 (pure JSON-RPC convention)
    but a ``from_agent`` mismatch is a security event we want visible
    in 4xx access-log alerting. The body still carries the structured
    JSON-RPC ``error`` envelope so spec-compliant clients keep
    parsing it as JSON-RPC.
    """
    payload = json.dumps(body).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 400,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": payload,
            "more_body": False,
        }
    )


def _with_content_length(scope: dict[str, Any], length: int) -> dict[str, Any]:
    """Return a scope copy with Content-Length aligned to the new body length.

    Avoids mutating the caller's scope in place — ASGI middleware
    composition is much easier to reason about when each layer
    treats the scope as immutable.
    """
    new_headers: list[tuple[bytes, bytes]] = []
    seen = False
    for name, value in scope.get("headers") or []:
        if name.lower() == b"content-length":
            new_headers.append((name, str(length).encode("ascii")))
            seen = True
        else:
            new_headers.append((name, value))
    if not seen:
        new_headers.append((b"content-length", str(length).encode("ascii")))
    new_scope = dict(scope)
    new_scope["headers"] = new_headers
    return new_scope


__all__ = ["A2AFromAgentValidationMiddleware"]
