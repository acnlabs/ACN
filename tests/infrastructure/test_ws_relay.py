"""Unit tests for ADR-0012 Mode B — WebSocket relay correlation.

Covers ``WebSocketManager.relay_request_to_agent`` /
``resolve_relay_response``: the real-time webhook delivery path for agents
that registered without a public HTTP endpoint.

Behaviours under test:
- offline agent (no live WS connection) → returns ``None`` (caller backstops).
- connected agent → pushes an ``a2a_request`` frame and returns the
  correlated ``a2a_response`` payload.
- connected but silent agent → raises ``TimeoutError``.
- ``resolve_relay_response`` is a no-op for unknown / already-settled ids.
- non-UTF-8 body is transported as base64.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from acn.infrastructure.messaging.websocket_manager import (
    MessageType,
    WebSocketManager,
)


class FakeWebSocket:
    """Minimal WebSocket double recording frames sent via ``send_json``."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def accept(self) -> None:  # pragma: no cover - trivial
        return None

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)

    async def close(self, *args, **kwargs) -> None:  # pragma: no cover - trivial
        self.closed = True


@pytest.fixture
def manager() -> WebSocketManager:
    # Redis is unused by the relay path; a bare object is enough since no
    # relay method touches ``self.redis``.
    return WebSocketManager(redis_client=object())  # type: ignore[arg-type]


async def _connect(manager: WebSocketManager, agent_id: str) -> FakeWebSocket:
    ws = FakeWebSocket()
    await manager.connect(ws, user_id=agent_id)  # type: ignore[arg-type]
    ws.sent.clear()  # drop the "Connected to ACN" welcome frame
    return ws


def _last_request_frame(ws: FakeWebSocket) -> dict:
    requests = [f for f in ws.sent if f.get("type") == MessageType.A2A_REQUEST.value]
    assert requests, f"no a2a_request frame was pushed; got {ws.sent}"
    return requests[-1]


@pytest.mark.asyncio
async def test_offline_agent_returns_none(manager: WebSocketManager) -> None:
    result = await manager.relay_request_to_agent(
        "agent-offline",
        method="POST",
        path="/",
        headers={},
        body=b"{}",
        timeout=0.1,
    )
    assert result is None


@pytest.mark.asyncio
async def test_connected_agent_relays_and_returns_response(
    manager: WebSocketManager,
) -> None:
    agent_id = "agent-online"
    ws = await _connect(manager, agent_id)

    relay_task = asyncio.create_task(
        manager.relay_request_to_agent(
            agent_id,
            method="POST",
            path="/",
            headers={"X-ACN-Caller-Agent": "caller-1"},
            body=b'{"jsonrpc":"2.0","method":"message/send"}',
            timeout=2.0,
        )
    )

    # Wait for the request frame to be pushed, then reply as the agent would.
    frame = await asyncio.wait_for(_await_frame(ws), timeout=1.0)
    correlation_id = frame["id"]
    assert frame["method"] == "POST"
    assert frame["body_encoding"] == "utf-8"
    assert frame["headers"]["X-ACN-Caller-Agent"] == "caller-1"

    matched = manager.resolve_relay_response(
        correlation_id,
        {
            "type": "a2a_response",
            "id": correlation_id,
            "status": 200,
            "headers": {"content-type": "application/json"},
            "body": '{"result":"ok"}',
        },
    )
    assert matched is True

    result = await asyncio.wait_for(relay_task, timeout=1.0)
    assert result is not None
    assert result["status"] == 200
    assert result["body"] == '{"result":"ok"}'


@pytest.mark.asyncio
async def test_connected_but_silent_agent_times_out(manager: WebSocketManager) -> None:
    agent_id = "agent-silent"
    await _connect(manager, agent_id)

    with pytest.raises(TimeoutError):
        await manager.relay_request_to_agent(
            agent_id,
            method="POST",
            path="/",
            headers={},
            body=b"{}",
            timeout=0.1,
        )

    # The pending future must not leak after a timeout.
    assert manager._relay_futures == {}


@pytest.mark.asyncio
async def test_relay_pushes_to_single_connection_when_agent_has_many(
    manager: WebSocketManager,
) -> None:
    # Two live connections for the same agent. A request/response relay must
    # hit exactly one — broadcasting would double-execute non-idempotent work.
    agent_id = "agent-dup"
    ws1 = await _connect(manager, agent_id)
    ws2 = await _connect(manager, agent_id)

    relay_task = asyncio.create_task(
        manager.relay_request_to_agent(
            agent_id, method="POST", path="/", headers={}, body=b"{}", timeout=2.0
        )
    )
    await asyncio.sleep(0.02)

    pushed_1 = [f for f in ws1.sent if f.get("type") == MessageType.A2A_REQUEST.value]
    pushed_2 = [f for f in ws2.sent if f.get("type") == MessageType.A2A_REQUEST.value]
    assert len(pushed_1) + len(pushed_2) == 1, "exactly one connection must receive the frame"

    frame = (pushed_1 or pushed_2)[0]
    manager.resolve_relay_response(frame["id"], {"status": 200, "body": "{}"})
    await asyncio.wait_for(relay_task, timeout=1.0)


@pytest.mark.asyncio
async def test_resolve_unknown_correlation_is_noop(manager: WebSocketManager) -> None:
    assert manager.resolve_relay_response("does-not-exist", {"status": 200}) is False


@pytest.mark.asyncio
async def test_non_utf8_body_is_base64_encoded(manager: WebSocketManager) -> None:
    agent_id = "agent-binary"
    ws = await _connect(manager, agent_id)
    raw = b"\xff\xfe\x00\x01"  # invalid UTF-8

    relay_task = asyncio.create_task(
        manager.relay_request_to_agent(
            agent_id,
            method="POST",
            path="/",
            headers={},
            body=raw,
            timeout=2.0,
        )
    )
    frame = await asyncio.wait_for(_await_frame(ws), timeout=1.0)
    assert frame["body_encoding"] == "base64"
    assert base64.b64decode(frame["body"]) == raw

    manager.resolve_relay_response(frame["id"], {"status": 204, "body": ""})
    await asyncio.wait_for(relay_task, timeout=1.0)


async def _await_frame(ws: FakeWebSocket) -> dict:
    """Poll until the relay coroutine has pushed its a2a_request frame."""
    for _ in range(100):
        requests = [f for f in ws.sent if f.get("type") == MessageType.A2A_REQUEST.value]
        if requests:
            return requests[-1]
        await asyncio.sleep(0.005)
    raise AssertionError(f"a2a_request frame never arrived; sent={ws.sent}")
