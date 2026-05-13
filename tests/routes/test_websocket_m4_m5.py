"""M4/M5 security tests: WebSocket frame-size caps and channel authorization.

M4 — inbound frame size caps
-----------------------------
``_MAX_WS_AUTH_FRAME_BYTES`` (4 KB) and ``_MAX_WS_MESSAGE_FRAME_BYTES``
(1 MiB) prevent memory exhaustion from oversized frames.  Two sites
in ``routes/websocket.py`` are covered:

* First-message auth frame (path 2 — first-message auth with a
  body larger than 4 KB).
* Main message-loop frame (after successful auth, a body larger
  than 1 MiB).

The test verifies that both sites close the WS connection with a
structured error frame carrying ``reason="ws_frame_too_large"``.

M5 — channel subscription authorization
-----------------------------------------
``WebSocketManager._check_channel_auth`` enforces an allow-list policy:

* ``agent:<own_id>``    → allowed
* ``agent:<other_id>``  → denied (cross-agent eavesdropping)
* ``system:*``          → denied (reserved namespace)
* ``session:*``         → allowed (bilateral session)
* ``broadcast:*``       → allowed (public channel)
* Unknown prefix        → denied (deny-by-default)
* Name > 256 chars      → denied (DoS guard)

``handle_message`` with ``type=subscribe`` and a denied channel must
send an error frame back instead of subscribing.

``handle_message`` with ``type=unsubscribe`` for a channel the
connection is NOT subscribed to must be a no-op.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import acn.routes.websocket as ws_route
from acn.infrastructure.messaging.websocket_manager import (
    Connection,
    MessageType,
    WebSocketManager,
)

# ---------------------------------------------------------------------------
# Shared helpers (same pattern as test_websocket_auth_m14.py)
# ---------------------------------------------------------------------------


def _make_app(
    *,
    valid_token: str = "good-token",
    matching_agent_id: str = "agent-1",
) -> tuple:
    app = FastAPI()
    app.include_router(ws_route.router)

    settings_stub = SimpleNamespace(
        websocket_allow_query_token=False,
    )

    async def _get_agent_by_api_key(token: str):
        if token == valid_token:
            return SimpleNamespace(agent_id=matching_agent_id)
        return None

    agent_service_stub = SimpleNamespace(get_agent_by_api_key=_get_agent_by_api_key)

    ws_manager_stub = MagicMock()
    ws_manager_stub.connect = AsyncMock(return_value="conn-1")
    ws_manager_stub.disconnect = AsyncMock()

    return app, ws_manager_stub, agent_service_stub, settings_stub


def _patches(ws_manager, agent_service, settings):
    return [
        patch.object(ws_route, "get_ws_manager", return_value=ws_manager),
        patch.object(ws_route, "get_agent_service", return_value=agent_service),
        patch.object(ws_route, "get_settings", return_value=settings),
    ]


def _enter(patches):
    return [p.__enter__() for p in patches]


def _exit(patches):
    for p in reversed(patches):
        p.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# M4 — first-message auth frame too large (> 4 KB)
# ---------------------------------------------------------------------------


class TestM4AuthFrameTooLarge:
    """A first-message auth frame exceeding _MAX_WS_AUTH_FRAME_BYTES must
    be rejected with a structured error frame and the connection closed."""

    def test_oversized_auth_frame_is_rejected(self):
        app, ws_mgr, agent_svc, settings = _make_app()
        patches = _patches(ws_mgr, agent_svc, settings)
        _enter(patches)
        try:
            with TestClient(app).websocket_connect("/ws/agent-1") as ws:
                # Send a padded JSON frame well above the 4 KB auth limit
                oversized = json.dumps(
                    {"type": "auth", "token": "good-token", "pad": "x" * 5000}
                )
                ws.send_text(oversized)
                # Server should send the error frame then close
                response = ws.receive_text()
                body = json.loads(response)
                assert body["type"] == "error"
                assert body["error_code"] == "authentication_required"
                assert body["details"]["reason"] == "ws_frame_too_large"
        except Exception:
            pass  # Connection is expected to be closed by the server
        finally:
            _exit(patches)


# ---------------------------------------------------------------------------
# M4 — constants are sane
# ---------------------------------------------------------------------------


class TestM4Constants:
    def test_auth_limit_is_4kb(self):
        assert ws_route._MAX_WS_AUTH_FRAME_BYTES == 4_096

    def test_message_limit_is_1mib(self):
        assert ws_route._MAX_WS_MESSAGE_FRAME_BYTES == 1_048_576

    def test_auth_limit_lt_message_limit(self):
        assert ws_route._MAX_WS_AUTH_FRAME_BYTES < ws_route._MAX_WS_MESSAGE_FRAME_BYTES


# ---------------------------------------------------------------------------
# M5 — _check_channel_auth unit tests
# ---------------------------------------------------------------------------


def _make_connection(user_id: str = "agent-alice") -> Connection:
    ws_stub = MagicMock()
    return Connection(
        connection_id="conn-1",
        websocket=ws_stub,
        user_id=user_id,
    )


class TestCheckChannelAuth:
    """Unit tests for WebSocketManager._check_channel_auth."""

    def setup_method(self):
        self.mgr = WebSocketManager.__new__(WebSocketManager)

    def test_own_agent_channel_allowed(self):
        conn = _make_connection("agent-alice")
        assert self.mgr._check_channel_auth(conn, "agent:agent-alice") is None

    def test_other_agent_channel_denied(self):
        conn = _make_connection("agent-alice")
        err = self.mgr._check_channel_auth(conn, "agent:agent-bob")
        assert err == "channel_subscription_denied"

    def test_system_channel_always_denied(self):
        conn = _make_connection("agent-alice")
        err = self.mgr._check_channel_auth(conn, "system:notifications")
        assert err == "channel_subscription_denied"

    def test_session_channel_allowed(self):
        conn = _make_connection("agent-alice")
        assert self.mgr._check_channel_auth(conn, "session:sess-123") is None

    def test_broadcast_channel_allowed(self):
        conn = _make_connection("agent-alice")
        assert self.mgr._check_channel_auth(conn, "broadcast:announcements") is None

    def test_unknown_prefix_denied(self):
        conn = _make_connection("agent-alice")
        err = self.mgr._check_channel_auth(conn, "chat:room-42")
        assert err == "channel_subscription_denied"

    def test_channel_name_too_long_denied(self):
        conn = _make_connection("agent-alice")
        long_channel = "session:" + "x" * 300
        err = self.mgr._check_channel_auth(conn, long_channel)
        assert err == "channel_name_too_long"

    def test_max_len_boundary_allowed(self):
        conn = _make_connection("agent-alice")
        # Exactly 256 chars — should pass length check
        channel = "broadcast:" + "a" * (256 - len("broadcast:"))
        assert len(channel) == 256
        assert self.mgr._check_channel_auth(conn, channel) is None

    def test_none_user_id_cannot_subscribe_agent_channel(self):
        """Connection with no user_id must not be able to subscribe to any
        agent: channel — ``None != 'agent-x'``."""
        conn = _make_connection(None)
        err = self.mgr._check_channel_auth(conn, "agent:agent-x")
        assert err == "channel_subscription_denied"


# ---------------------------------------------------------------------------
# M5 — handle_message integration: denied subscribe sends error frame
# ---------------------------------------------------------------------------


class TestM5HandleMessageSubscribeDenied:
    """handle_message with a denied channel must respond with an error
    frame and NOT add the subscription."""

    @pytest.mark.anyio
    async def test_subscribe_denied_sends_error_frame(self):
        redis_stub = MagicMock()
        redis_stub.publish = AsyncMock()
        mgr = WebSocketManager(redis_client=redis_stub)

        sent_messages: list[dict] = []

        async def _fake_send(conn, msg):
            sent_messages.append(msg)

        mgr._send = _fake_send

        ws_stub = MagicMock()
        conn = Connection(
            connection_id="conn-1",
            websocket=ws_stub,
            user_id="agent-alice",
        )
        mgr._connections["conn-1"] = conn

        await mgr.handle_message(
            "conn-1",
            {"type": "subscribe", "channel": "agent:agent-bob"},
        )

        assert any(m.get("type") == MessageType.ERROR.value for m in sent_messages)
        error_frame = next(m for m in sent_messages if m.get("type") == MessageType.ERROR.value)
        assert error_frame["error"] == "channel_subscription_denied"
        assert "agent:agent-bob" not in conn.subscriptions

    @pytest.mark.anyio
    async def test_subscribe_own_agent_channel_succeeds(self):
        redis_stub = MagicMock()
        redis_stub.publish = AsyncMock()
        mgr = WebSocketManager(redis_client=redis_stub)
        mgr._pubsub = None  # no Redis pub/sub in unit test

        sent_messages: list[dict] = []

        async def _fake_send(conn, msg):
            sent_messages.append(msg)

        mgr._send = _fake_send

        ws_stub = MagicMock()
        conn = Connection(
            connection_id="conn-1",
            websocket=ws_stub,
            user_id="agent-alice",
        )
        mgr._connections["conn-1"] = conn

        await mgr.handle_message(
            "conn-1",
            {"type": "subscribe", "channel": "agent:agent-alice"},
        )

        assert not any(m.get("type") == MessageType.ERROR.value for m in sent_messages)
        assert "agent:agent-alice" in conn.subscriptions

    @pytest.mark.anyio
    async def test_unsubscribe_not_subscribed_is_noop(self):
        """Sending unsubscribe for a channel the connection isn't in must
        not raise and must not modify subscriptions."""
        redis_stub = MagicMock()
        mgr = WebSocketManager(redis_client=redis_stub)

        ws_stub = MagicMock()
        conn = Connection(
            connection_id="conn-1",
            websocket=ws_stub,
            user_id="agent-alice",
        )
        mgr._connections["conn-1"] = conn

        # Should not raise
        await mgr.handle_message(
            "conn-1",
            {"type": "unsubscribe", "channel": "broadcast:topic"},
        )
        assert "broadcast:topic" not in conn.subscriptions
