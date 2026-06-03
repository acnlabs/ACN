"""Unit tests for MessageRouter ADR-0012 Mode B relay delivery (P2d).

Covers ``MessageRouter._route_relay_or_inbox`` — the ACN-mediated
(`POST /communication/send`) counterpart to the HTTP gateway proxy relay.
An agent registered with ``delivery="relay"`` has no direct endpoint, so
``route()`` must push the message over the agent's live WebSocket when
connected, and fall back to the offline inbox otherwise.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.infrastructure.messaging.message_router import (
    MessageRouter,
    create_text_message,
)


@pytest.fixture
def fake_pipe() -> MagicMock:
    pipe = MagicMock()
    pipe.execute = AsyncMock(return_value=[])
    return pipe


@pytest.fixture
def fake_redis(fake_pipe) -> AsyncMock:
    mock = AsyncMock()
    pipe_cm = MagicMock()
    pipe_cm.__aenter__ = AsyncMock(return_value=fake_pipe)
    pipe_cm.__aexit__ = AsyncMock(return_value=False)
    mock.pipeline = MagicMock(return_value=pipe_cm)
    return mock


@pytest.fixture
def mock_agent_service() -> AsyncMock:
    svc = AsyncMock()
    # A relay agent IS alive (WS connected), but liveness is irrelevant on the
    # relay branch — it short-circuits before the is_alive pre-check.
    svc.is_alive = AsyncMock(return_value=True)
    return svc


def _relay_agent_info():
    """Agent record with NO direct endpoint (delivery=relay shape)."""
    info = MagicMock()
    info.endpoint = ""
    info.a2a_endpoint = ""
    info.status = "online"
    return info


def _jsonrpc_message_reply() -> dict:
    return {
        "status": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "abc",
                "result": {
                    "kind": "message",
                    "messageId": "m-reply",
                    "role": "agent",
                    "parts": [{"kind": "text", "text": "pong"}],
                },
            }
        ),
        "body_encoding": "utf-8",
    }


class TestRelayDelivery:
    @pytest.mark.asyncio
    async def test_connected_agent_relays_in_real_time(
        self, mock_agent_service, fake_redis, fake_pipe
    ):
        mock_agent_service.find_agent = AsyncMock(return_value=_relay_agent_info())
        ws_manager = MagicMock()
        ws_manager.relay_request_to_agent = AsyncMock(
            return_value=_jsonrpc_message_reply()
        )
        router = MessageRouter(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            ws_manager=ws_manager,
        )

        result = await router.route(
            from_agent="agent-a",
            to_agent="agent-b",
            message=create_text_message("ping"),
        )

        ws_manager.relay_request_to_agent.assert_awaited_once()
        call = ws_manager.relay_request_to_agent.await_args
        assert call.args[0] == "agent-b"
        assert call.kwargs["method"] == "POST"
        # The relayed body must be a JSON-RPC message/send envelope.
        sent_body = json.loads(call.kwargs["body"])
        assert sent_body["method"] == "message/send"
        # Headers must mirror a direct HTTP A2A POST (Mode A parity): the
        # a2a protocol version header is present so strict servers accept it.
        from a2a.utils.constants import PROTOCOL_VERSION_0_3, VERSION_HEADER

        headers = call.kwargs["headers"]
        assert headers[VERSION_HEADER.lower()] == PROTOCOL_VERSION_0_3
        assert headers["content-type"] == "application/json"

        assert result["delivery_mode"] == "relay"
        assert result["status"] == "delivered"
        assert result["response"]["messageId"] == "m-reply"
        # Real-time delivery must NOT touch the inbox.
        fake_pipe.zadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_offline_relay_agent_parks_in_inbox(
        self, mock_agent_service, fake_redis, fake_pipe
    ):
        mock_agent_service.find_agent = AsyncMock(return_value=_relay_agent_info())
        ws_manager = MagicMock()
        # None => agent holds no live WS connection.
        ws_manager.relay_request_to_agent = AsyncMock(return_value=None)
        router = MessageRouter(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            ws_manager=ws_manager,
        )

        result = await router.route(
            from_agent="agent-a",
            to_agent="agent-b",
            message=create_text_message("ping"),
        )

        assert result["delivery_mode"] == "inbox"
        # Inbox write must have happened on the recipient's key.
        assert fake_pipe.zadd.call_count == 1
        key, _members = fake_pipe.zadd.call_args.args
        assert key == "acn:inbox:agent-b"

    @pytest.mark.asyncio
    async def test_relay_timeout_falls_back_to_inbox(
        self, mock_agent_service, fake_redis, fake_pipe
    ):
        mock_agent_service.find_agent = AsyncMock(return_value=_relay_agent_info())
        ws_manager = MagicMock()
        ws_manager.relay_request_to_agent = AsyncMock(side_effect=TimeoutError())
        router = MessageRouter(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            ws_manager=ws_manager,
        )

        result = await router.route(
            from_agent="agent-a",
            to_agent="agent-b",
            message=create_text_message("ping"),
        )

        assert result["delivery_mode"] == "inbox"
        assert fake_pipe.zadd.call_count == 1

    @pytest.mark.asyncio
    async def test_no_ws_manager_falls_back_to_inbox(
        self, mock_agent_service, fake_redis, fake_pipe
    ):
        """Legacy wiring (ws_manager=None): relay agent degrades to inbox."""
        mock_agent_service.find_agent = AsyncMock(return_value=_relay_agent_info())
        router = MessageRouter(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
        )

        result = await router.route(
            from_agent="agent-a",
            to_agent="agent-b",
            message=create_text_message("ping"),
        )

        assert result["delivery_mode"] == "inbox"
        assert fake_pipe.zadd.call_count == 1

    @pytest.mark.asyncio
    async def test_relay_branch_skips_is_alive_precheck(
        self, mock_agent_service, fake_redis
    ):
        """Relay liveness is the WS connection, not the heartbeat alive key.

        A relay agent whose alive-key happens to be absent must still be
        attempted over WS (and only inbox'd when the WS relay reports offline),
        so ``route()`` must not consult ``is_alive`` on the relay branch.
        """
        mock_agent_service.find_agent = AsyncMock(return_value=_relay_agent_info())
        mock_agent_service.is_alive = AsyncMock(return_value=False)
        ws_manager = MagicMock()
        ws_manager.relay_request_to_agent = AsyncMock(
            return_value=_jsonrpc_message_reply()
        )
        router = MessageRouter(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            ws_manager=ws_manager,
        )

        result = await router.route(
            from_agent="agent-a",
            to_agent="agent-b",
            message=create_text_message("ping"),
        )

        assert result["delivery_mode"] == "relay"
        mock_agent_service.is_alive.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_route_stream_rejects_relay_agent(
        self, mock_agent_service, fake_redis
    ):
        """Streaming is not relayed (P2d covers message/send only): a relay
        agent (no direct endpoint) must surface a clear error, not crash in
        _get_client on an empty URL."""
        mock_agent_service.find_agent = AsyncMock(return_value=_relay_agent_info())
        router = MessageRouter(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            ws_manager=MagicMock(),
        )
        # _get_client must never be reached for a relay agent.
        router._get_client = AsyncMock()

        with pytest.raises(ValueError, match="streaming"):
            async for _ in router.route_stream(
                from_agent="agent-a",
                to_agent="agent-b",
                message=create_text_message("ping"),
            ):
                pass

        router._get_client.assert_not_awaited()
