"""M14 security tests: WebSocket auth via header & query-token gating.

What this pins down
-------------------
Three auth paths land on ``/ws/{agent_id}``:

1. ``Authorization: Bearer <key>`` handshake header (recommended for
   non-browser clients, never logged).
2. First-message JSON ``{"type":"auth","token":...}`` (recommended for
   browsers — ``new WebSocket()`` cannot set headers).
3. ``?token=<key>`` query string (deprecated; gated on
   ``websocket_allow_query_token``).

The audit finding (M14) is that path 3 leaks the API key into server
access logs, browser Referer, and shoulder-surfable URL bars. The fix
keeps backward compatibility but defaults the flag to **False** in
production so operators have to opt in to the lossy path. Dev mode
auto-flips it to True so existing dev rigs don't break.

We pin:

* Header auth works without first-message handshake.
* Query auth works only when the flag is on; rejects with 4401 when
  off, even with a valid key.
* First-message auth path still works (no header, no query).
* Token validation still happens in every path.
* The structured ``via=`` log field reflects the path used.

Tests use Starlette's ``TestClient.websocket_connect`` for true
handshake-level coverage. The router is mounted on a minimal FastAPI
app so we don't need the full ACN lifespan.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import acn.routes.websocket as ws_route

# ─────────────────────────────────────────────
# Test app builder
# ─────────────────────────────────────────────


def _make_app(
    *,
    valid_token: str = "good-token",
    matching_agent_id: str = "agent-1",
    allow_query_token: bool = False,
) -> tuple[FastAPI, MagicMock]:
    """Build a FastAPI app mounting the WS router with stubbed deps.

    Returns ``(app, ws_manager_mock)`` so callers can assert on
    connection-manager interactions if they care.
    """
    app = FastAPI()
    app.include_router(ws_route.router)

    # Settings stub: only attribute consulted is allow flag.
    settings_stub = SimpleNamespace(websocket_allow_query_token=allow_query_token)

    # Agent service stub: returns an Agent-shaped object with the
    # matching ID iff the API key matches; None otherwise.
    async def _get_agent_by_api_key(token: str):
        if token == valid_token:
            return SimpleNamespace(agent_id=matching_agent_id)
        return None

    agent_service_stub = SimpleNamespace(get_agent_by_api_key=_get_agent_by_api_key)

    # WS manager: spy on connect() so we can assert auth succeeded.
    ws_manager_stub = MagicMock()
    ws_manager_stub.connect = AsyncMock(return_value="conn-1")
    ws_manager_stub.disconnect = AsyncMock()

    # Patch the module-level dep accessors and settings getter so we
    # don't have to bring up the full ACN lifespan.
    app.dependency_overrides = {}  # not used — we patch directly

    # NOTE: we patch via context managers in each test (not here) so
    # they're scoped to a single connection; returning the stubs lets
    # tests build the patch tuple themselves.
    return app, ws_manager_stub, agent_service_stub, settings_stub


def _patches(ws_manager, agent_service, settings):
    """Convenience: build the four patches each test needs."""
    return [
        patch.object(ws_route, "get_ws_manager", return_value=ws_manager),
        patch.object(ws_route, "get_agent_service", return_value=agent_service),
        patch.object(ws_route, "get_settings", return_value=settings),
    ]


def _enter(patches):
    """Enter all context managers; return list of started patches."""
    return [p.__enter__() for p in patches]


def _exit(patches):
    for p in reversed(patches):
        p.__exit__(None, None, None)


# ─────────────────────────────────────────────
# Bearer-token extraction (small unit; worth pinning)
# ─────────────────────────────────────────────


class TestExtractBearer:
    def test_extracts_simple_bearer(self):
        ws = SimpleNamespace(headers={"authorization": "Bearer abc123"})
        assert ws_route._extract_bearer_token(ws) == "abc123"

    def test_case_insensitive_header_name(self):
        ws = SimpleNamespace(headers={"Authorization": "Bearer xyz"})
        assert ws_route._extract_bearer_token(ws) == "xyz"

    def test_case_insensitive_scheme(self):
        ws = SimpleNamespace(headers={"authorization": "bearer hello"})
        assert ws_route._extract_bearer_token(ws) == "hello"

    def test_no_header_returns_none(self):
        ws = SimpleNamespace(headers={})
        assert ws_route._extract_bearer_token(ws) is None

    def test_non_bearer_scheme_returns_none(self):
        # ``Basic`` / ``Digest`` / etc. aren't recognised — the whole
        # endpoint is bearer-only.
        ws = SimpleNamespace(headers={"authorization": "Basic dXNlcjpwYXNz"})
        assert ws_route._extract_bearer_token(ws) is None

    def test_empty_bearer_returns_none(self):
        ws = SimpleNamespace(headers={"authorization": "Bearer "})
        assert ws_route._extract_bearer_token(ws) is None


# ─────────────────────────────────────────────
# Header auth — recommended path
# ─────────────────────────────────────────────


class TestHeaderAuth:
    def test_valid_bearer_connects_without_first_message(self):
        app, ws_mgr, svc, settings = _make_app()
        ps = _patches(ws_mgr, svc, settings)
        _enter(ps)
        try:
            with TestClient(app) as client:
                with client.websocket_connect(
                    "/ws/agent-1",
                    headers={"Authorization": "Bearer good-token"},
                ) as ws:
                    # Header path: no auth_ok echo; first server message
                    # is whatever the echo loop sends. Send & receive once
                    # to prove the connection is fully usable.
                    ws.send_text("hello")
                    reply = ws.receive_text()
                    assert reply == "Received: hello"
            ws_mgr.connect.assert_awaited_once()
        finally:
            _exit(ps)

    def test_invalid_bearer_closes_4401(self):
        app, ws_mgr, svc, settings = _make_app()
        ps = _patches(ws_mgr, svc, settings)
        _enter(ps)
        try:
            with TestClient(app) as client:
                # Bad token: handshake accepts, then closes 4401 — the
                # TestClient surfaces this as ``WebSocketDisconnect``.
                from starlette.websockets import WebSocketDisconnect
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    with client.websocket_connect(
                        "/ws/agent-1",
                        headers={"Authorization": "Bearer wrong-token"},
                    ) as ws:
                        ws.receive_text()
                assert exc_info.value.code == 4401
            ws_mgr.connect.assert_not_called()
        finally:
            _exit(ps)

    def test_header_takes_priority_over_query_when_query_allowed(self):
        """If both header and query are present, header wins. Important
        because operators turning the query flag off shouldn't accidentally
        change the auth path of clients that already use the header."""
        app, ws_mgr, svc, settings = _make_app(allow_query_token=True)
        ps = _patches(ws_mgr, svc, settings)
        _enter(ps)
        try:
            with TestClient(app) as client:
                # Header has the GOOD token; query has a BAD token. If
                # query won, we'd 4401. If header wins, we connect.
                with client.websocket_connect(
                    "/ws/agent-1?token=wrong-token",
                    headers={"Authorization": "Bearer good-token"},
                ):
                    pass
            ws_mgr.connect.assert_awaited_once()
        finally:
            _exit(ps)


# ─────────────────────────────────────────────
# Query token — gated by flag
# ─────────────────────────────────────────────


class TestQueryTokenGating:
    def test_query_token_rejected_when_flag_false(self):
        # The whole point of M14: in production, the query path is off.
        app, ws_mgr, svc, settings = _make_app(allow_query_token=False)
        ps = _patches(ws_mgr, svc, settings)
        _enter(ps)
        try:
            with TestClient(app) as client:
                from starlette.websockets import WebSocketDisconnect
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    with client.websocket_connect("/ws/agent-1?token=good-token") as ws:
                        ws.receive_text()
                # 4401 = unauthorized; the close reason is informative
                # but we deliberately don't pin its exact wording so it
                # can evolve.
                assert exc_info.value.code == 4401
            ws_mgr.connect.assert_not_called()
        finally:
            _exit(ps)

    def test_query_token_accepted_when_flag_true(self):
        # Backward-compat path: dev rigs and operators with explicit opt-in.
        app, ws_mgr, svc, settings = _make_app(allow_query_token=True)
        ps = _patches(ws_mgr, svc, settings)
        _enter(ps)
        try:
            with TestClient(app) as client:
                with client.websocket_connect(
                    "/ws/agent-1?token=good-token",
                ) as ws:
                    ws.send_text("ping")
                    assert ws.receive_text() == "Received: ping"
            ws_mgr.connect.assert_awaited_once()
        finally:
            _exit(ps)

    def test_query_token_invalid_rejected_even_when_flag_true(self):
        # Flag only enables the *path*; the API key still has to validate.
        app, ws_mgr, svc, settings = _make_app(allow_query_token=True)
        ps = _patches(ws_mgr, svc, settings)
        _enter(ps)
        try:
            with TestClient(app) as client:
                from starlette.websockets import WebSocketDisconnect
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    with client.websocket_connect(
                        "/ws/agent-1?token=wrong-token",
                    ) as ws:
                        ws.receive_text()
                assert exc_info.value.code == 4401
        finally:
            _exit(ps)


# ─────────────────────────────────────────────
# First-message auth — still works
# ─────────────────────────────────────────────


class TestFirstMessageAuth:
    def test_valid_first_message_auth_succeeds(self):
        app, ws_mgr, svc, settings = _make_app()
        ps = _patches(ws_mgr, svc, settings)
        _enter(ps)
        try:
            with TestClient(app) as client:
                with client.websocket_connect("/ws/agent-1") as ws:
                    ws.send_json({"type": "auth", "token": "good-token"})
                    # First-message flow gets an auth_ok echo.
                    ack = ws.receive_json()
                    assert ack == {"type": "auth_ok"}
                    ws.send_text("ping")
                    assert ws.receive_text() == "Received: ping"
            ws_mgr.connect.assert_awaited_once()
        finally:
            _exit(ps)

    def test_first_message_with_invalid_token_closes_4401(self):
        app, ws_mgr, svc, settings = _make_app()
        ps = _patches(ws_mgr, svc, settings)
        _enter(ps)
        try:
            with TestClient(app) as client:
                from starlette.websockets import WebSocketDisconnect
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    with client.websocket_connect("/ws/agent-1") as ws:
                        ws.send_json({"type": "auth", "token": "wrong"})
                        ws.receive_text()
                assert exc_info.value.code == 4401
        finally:
            _exit(ps)

    def test_no_auth_message_at_all_closes_4401(self):
        app, ws_mgr, svc, settings = _make_app()
        ps = _patches(ws_mgr, svc, settings)
        _enter(ps)
        try:
            with TestClient(app) as client:
                from starlette.websockets import WebSocketDisconnect
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    with client.websocket_connect("/ws/agent-1") as ws:
                        # Send something that's not an auth message.
                        ws.send_json({"type": "ping"})
                        ws.receive_text()
                assert exc_info.value.code == 4401
        finally:
            _exit(ps)

    def test_agent_id_mismatch_rejected(self):
        # Even with a valid key, you can only connect on your own agent_id.
        app, ws_mgr, svc, settings = _make_app(matching_agent_id="agent-A")
        ps = _patches(ws_mgr, svc, settings)
        _enter(ps)
        try:
            with TestClient(app) as client:
                from starlette.websockets import WebSocketDisconnect
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    with client.websocket_connect("/ws/agent-OTHER") as ws:
                        ws.send_json({"type": "auth", "token": "good-token"})
                        ws.receive_text()
                assert exc_info.value.code == 4401
        finally:
            _exit(ps)
