"""Tests for A2AFromAgentValidationMiddleware (Phase 2 PR #2 P0-1).

The middleware closes the impersonation hole introduced by allowlist
mode: an external A2A caller could otherwise put any
``from_agent`` value in their request metadata and impersonate any
allowlisted sender. These tests pin every branch.

Test approach: drive the middleware as a raw ASGI app (no
FastAPI mount). The ``send`` and ``receive`` callables are
hand-rolled, the downstream "app" is a stub that records what it
received. This keeps the suite focused on middleware behaviour
without coupling to A2A SDK shape.
"""

from __future__ import annotations

import json

from acn.protocols.a2a.auth_middleware import (
    A2AFromAgentValidationMiddleware,
    _extract_bearer_token,
)

# ---------------------------------------------------------------------------
# ASGI scaffolding helpers
# ---------------------------------------------------------------------------


class _StubDownstream:
    """Records the body the middleware forwards."""

    def __init__(self):
        self.scope = None
        self.received_body = b""
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.scope = scope
        self.calls += 1
        # Drain the (cached) receive once so tests can inspect what
        # the middleware actually forwarded post-rewrite.
        event = await receive()
        if event.get("type") == "http.request":
            self.received_body = event.get("body", b"")
        # Send a trivial 200 so the test pipeline completes.
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok":true}'})


def _build_scope(
    *, path: str = "/jsonrpc", method: str = "POST", auth: str | None = None
) -> dict:
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
    ]
    if auth is not None:
        headers.append((b"authorization", auth.encode("latin-1")))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "headers": headers,
    }


def _make_receive(body: bytes):
    """One-shot ASGI receive returning the body then disconnect."""
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


class _SendCollector:
    """Captures all messages sent so we can assert on responses."""

    def __init__(self):
        self.messages: list[dict] = []

    async def __call__(self, message):
        self.messages.append(message)

    @property
    def status(self) -> int | None:
        for m in self.messages:
            if m["type"] == "http.response.start":
                return m["status"]
        return None

    @property
    def body_bytes(self) -> bytes:
        chunks = [
            m["body"]
            for m in self.messages
            if m["type"] == "http.response.body"
        ]
        return b"".join(chunks)


def _make_lookup(mapping: dict[str, str | None]):
    async def _lookup(api_key: str) -> str | None:
        return mapping.get(api_key)

    return _lookup


# ---------------------------------------------------------------------------
# Pass-through paths — non-enforced routes
# ---------------------------------------------------------------------------


async def test_get_agent_card_is_passed_through_unchanged():
    """Anonymous discovery (GET ``.well-known/agent-card.json``) must
    NOT be touched. The middleware only enforces POST /jsonrpc."""
    downstream = _StubDownstream()
    mw = A2AFromAgentValidationMiddleware(downstream, agent_lookup=_make_lookup({}))

    scope = _build_scope(path="/.well-known/agent-card.json", method="GET")
    send = _SendCollector()
    await mw(scope, _make_receive(b""), send)

    assert downstream.calls == 1
    assert send.status == 200


# ---------------------------------------------------------------------------
# Enforced path — match success
# ---------------------------------------------------------------------------


async def test_authenticated_call_with_matching_from_agent_passes():
    """Happy path: authenticated caller declared ``from_agent`` that
    matches their api_key → request is forwarded untouched."""
    downstream = _StubDownstream()
    lookup = _make_lookup({"key-alice": "alice"})
    mw = A2AFromAgentValidationMiddleware(downstream, agent_lookup=lookup)

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "r-1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": "m-1",
                    "parts": [{"kind": "text", "text": "hi"}],
                    "metadata": {"from_agent": "alice"},
                }
            },
        }
    ).encode("utf-8")

    send = _SendCollector()
    await mw(
        _build_scope(auth="Bearer key-alice"),
        _make_receive(body),
        send,
    )

    assert downstream.calls == 1
    forwarded = json.loads(downstream.received_body)
    assert forwarded["params"]["message"]["metadata"]["from_agent"] == "alice"


# ---------------------------------------------------------------------------
# Enforced path — mismatch rejected
# ---------------------------------------------------------------------------


async def test_authenticated_call_with_mismatched_from_agent_is_rejected():
    """The core security check: caller authenticated as ``alice`` but
    declared ``from_agent="bob"``. Must NOT reach downstream;
    JSON-RPC error response with ``-32600`` Invalid Request, returned
    over HTTP 400 (PR #2 v3 P1-A2: surfaces in 4xx alerting)."""
    downstream = _StubDownstream()
    lookup = _make_lookup({"key-alice": "alice"})
    mw = A2AFromAgentValidationMiddleware(downstream, agent_lookup=lookup)

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "r-99",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": "m-1",
                    "parts": [{"kind": "text", "text": "hi"}],
                    "metadata": {"from_agent": "bob"},  # impersonation attempt
                }
            },
        }
    ).encode("utf-8")

    send = _SendCollector()
    await mw(
        _build_scope(auth="Bearer key-alice"),
        _make_receive(body),
        send,
    )

    # Downstream must NEVER run — that's the whole point.
    assert downstream.calls == 0

    # PR #2 v3 P1-A2: rejection now uses HTTP 400 so the security
    # event lands in standard 4xx access-log alerting instead of being
    # silently flattened into a 200 with an error body.
    assert send.status == 400

    payload = json.loads(send.body_bytes)
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == "r-99"
    assert payload["error"]["code"] == -32600
    # Both ids appear in the message so an operator reading audit
    # logs can see exactly who tried to impersonate whom.
    assert "alice" in payload["error"]["message"]
    assert "bob" in payload["error"]["message"]


# ---------------------------------------------------------------------------
# Enforced path — backfill
# ---------------------------------------------------------------------------


async def test_authenticated_call_with_no_from_agent_is_backfilled():
    """Common case: A2A clients that haven't been updated to set
    ``metadata.from_agent``. The middleware backfills the
    authenticated id so downstream policy decisions get the right
    sender — without rejecting the request."""
    downstream = _StubDownstream()
    lookup = _make_lookup({"key-alice": "alice"})
    mw = A2AFromAgentValidationMiddleware(downstream, agent_lookup=lookup)

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "r-1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": "m-1",
                    "parts": [{"kind": "text", "text": "hi"}],
                    # No metadata.from_agent declared.
                }
            },
        }
    ).encode("utf-8")

    send = _SendCollector()
    await mw(
        _build_scope(auth="Bearer key-alice"),
        _make_receive(body),
        send,
    )

    assert downstream.calls == 1
    forwarded = json.loads(downstream.received_body)
    assert forwarded["params"]["message"]["metadata"]["from_agent"] == "alice"


# ---------------------------------------------------------------------------
# Enforced path — anonymous demotion
# ---------------------------------------------------------------------------


async def test_anonymous_caller_has_from_agent_rewritten_to_unknown():
    """No Authorization header → anonymous. Any declared
    ``from_agent`` is rewritten to ``"unknown"`` so downstream
    policy treats it as untrusted external. (The request is NOT
    rejected — open / closed / manifest modes still work for
    anonymous callers.)"""
    downstream = _StubDownstream()
    mw = A2AFromAgentValidationMiddleware(downstream, agent_lookup=_make_lookup({}))

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "r-1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": "m-1",
                    "parts": [{"kind": "text", "text": "hi"}],
                    "metadata": {"from_agent": "system:platform"},
                }
            },
        }
    ).encode("utf-8")

    send = _SendCollector()
    await mw(_build_scope(auth=None), _make_receive(body), send)

    assert downstream.calls == 1
    forwarded = json.loads(downstream.received_body)
    # The dangerous ``system:`` prefix is stripped to "unknown" so
    # an anonymous caller cannot trigger the system: policy
    # exemption. Defence-in-depth alongside _safe_a2a_from_agent
    # in server.py.
    assert forwarded["params"]["message"]["metadata"]["from_agent"] == "unknown"


# ---------------------------------------------------------------------------
# Lookup failure path
# ---------------------------------------------------------------------------


async def test_agent_lookup_failure_treated_as_anonymous():
    """If the agent lookup raises (Redis blip, PG down), the
    middleware degrades to anonymous — request passes through with
    from_agent rewritten to "unknown", instead of returning 5xx.
    Fail-loud here would convert any auth backend hiccup into a
    full A2A outage."""
    downstream = _StubDownstream()

    async def _exploding(_api_key: str):
        raise RuntimeError("Redis down")

    mw = A2AFromAgentValidationMiddleware(downstream, agent_lookup=_exploding)

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "r-1",
            "params": {
                "message": {"metadata": {"from_agent": "alice"}}
            },
        }
    ).encode("utf-8")

    send = _SendCollector()
    await mw(
        _build_scope(auth="Bearer key-alice"),
        _make_receive(body),
        send,
    )

    assert downstream.calls == 1
    forwarded = json.loads(downstream.received_body)
    assert forwarded["params"]["message"]["metadata"]["from_agent"] == "unknown"


# ---------------------------------------------------------------------------
# Malformed body — pass through to let JSON-RPC handler emit standard error
# ---------------------------------------------------------------------------


async def test_malformed_json_passes_through_unchanged():
    """If the body isn't JSON, we don't try to parse / rewrite
    anything — let the downstream JSON-RPC layer return the
    standard parse error. We MUST forward the original bytes so
    the JSON-RPC handler sees the exact malformation."""
    downstream = _StubDownstream()
    mw = A2AFromAgentValidationMiddleware(downstream, agent_lookup=_make_lookup({}))

    body = b"this is not json"

    send = _SendCollector()
    await mw(_build_scope(auth=None), _make_receive(body), send)

    assert downstream.calls == 1
    assert downstream.received_body == body


# ---------------------------------------------------------------------------
# Non-HTTP / non-POST scopes
# ---------------------------------------------------------------------------


async def test_lifespan_scope_is_passed_through():
    """Non-HTTP scopes (lifespan / websocket / etc.) must be
    forwarded unchanged. A regression here would break startup."""
    downstream_calls = 0

    async def _downstream(scope, receive, send):
        nonlocal downstream_calls
        downstream_calls += 1

    mw = A2AFromAgentValidationMiddleware(
        _downstream, agent_lookup=_make_lookup({})
    )

    scope = {"type": "lifespan"}

    async def _receive():
        return {"type": "lifespan.startup"}

    async def _send(_msg):
        pass

    await mw(scope, _receive, _send)
    assert downstream_calls == 1


# ---------------------------------------------------------------------------
# Bearer token parsing — PR #2 v3 review P1-A4
# ---------------------------------------------------------------------------
#
# Real-world clients produce headers with case variation, extra
# whitespace, and CRLF leftovers. Strict ``startswith("Bearer ")``
# silently demoted these to anonymous, which then forced a
# ``from_agent="unknown"`` rewrite on otherwise-authenticated
# callers. The regex-based extractor handles all three.


def _hdr(value: str) -> list[tuple[bytes, bytes]]:
    """Single-header list shaped like ASGI scope['headers']."""
    return [(b"authorization", value.encode("latin-1"))]


def test_bearer_lowercase_scheme_extracts_token():
    """RFC 6750 §2.1 scheme MUST match case-insensitively."""
    assert _extract_bearer_token(_hdr("bearer abc-123")) == "abc-123"


def test_bearer_uppercase_scheme_extracts_token():
    assert _extract_bearer_token(_hdr("BEARER abc-123")) == "abc-123"


def test_bearer_mixed_case_scheme_extracts_token():
    assert _extract_bearer_token(_hdr("Bearer abc-123")) == "abc-123"


def test_bearer_multiple_spaces_between_scheme_and_token():
    """Multi-space separators are valid HTTP — must not silently anonymise."""
    assert _extract_bearer_token(_hdr("Bearer    abc-123")) == "abc-123"


def test_bearer_tab_separator_is_accepted():
    assert _extract_bearer_token(_hdr("Bearer\tabc-123")) == "abc-123"


def test_bearer_trailing_whitespace_is_stripped():
    """Some HTTP clients append CR/LF to header values."""
    assert _extract_bearer_token(_hdr("Bearer abc-123  ")) == "abc-123"
    assert _extract_bearer_token(_hdr("Bearer abc-123\r\n")) == "abc-123"


def test_bearer_empty_token_returns_none():
    """``Bearer `` alone with no token must not pass — anonymous fallback."""
    assert _extract_bearer_token(_hdr("Bearer ")) is None
    assert _extract_bearer_token(_hdr("Bearer")) is None


def test_non_bearer_scheme_returns_none():
    """Basic / Digest / unknown schemes are treated as anonymous."""
    assert _extract_bearer_token(_hdr("Basic dXNlcjpwYXNz")) is None
    assert _extract_bearer_token(_hdr("Digest username=alice")) is None


def test_no_authorization_header_returns_none():
    assert _extract_bearer_token([]) is None


def test_authorization_header_case_insensitive_lookup():
    """ASGI lowercases header names per spec but be defensive anyway."""
    assert (
        _extract_bearer_token([(b"Authorization", b"Bearer abc-123")])
        == "abc-123"
    )
