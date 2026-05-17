"""Implicit-heartbeat contract tests for the WebSocket path.

The HTTP-side hook is exercised in
``tests/routes/test_implicit_heartbeat_dependencies.py``. This module
locks down the symmetric WS contract in ``SubnetManager._message_loop``:
every inbound ``HEARTBEAT`` frame must schedule
``AgentService.touch_alive(agent_id)`` so a WS-attached agent that
sends nothing but heartbeats still keeps Redis ``alive`` TTL fresh.

Test strategy
-------------
We drive the real ``_message_loop`` once with a mocked WebSocket that
returns a single HEARTBEAT frame and then raises ``WebSocketDisconnect``
to terminate cleanly. We then assert:

* ``agent_service.touch_alive`` was awaited exactly once with the
  connection's ``agent_id``, AND
* the existing HEARTBEAT_ACK response was still sent (regression guard:
  the implicit-heartbeat insertion must not have shifted the ack path).

A second test pins the opt-out invariant: legacy fixtures construct
``SubnetManager`` without ``agent_service=``, and the HEARTBEAT branch
must still complete (ACK sent, no AttributeError on the ``None`` hook).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketDisconnect

from acn.infrastructure.messaging.subnet_manager import (
    GatewayConnection,
    GatewayMessageType,
    SubnetManager,
)


def _heartbeat_frame() -> str:
    return json.dumps({"type": GatewayMessageType.HEARTBEAT})


def _mock_ws_yielding_one_heartbeat() -> MagicMock:
    """A WebSocket mock that returns one HEARTBEAT frame then disconnects.

    ``WebSocketDisconnect`` is what the real Starlette WS surface raises
    on ``receive_text()`` after the peer closes, so this is the same
    termination path ``_message_loop`` already handles upstream of this
    test (in ``handle_connection``'s try/except).
    """
    ws = MagicMock()
    ws.receive_text = AsyncMock(side_effect=[_heartbeat_frame(), WebSocketDisconnect()])
    ws.send_json = AsyncMock()
    return ws


def _make_connection(ws: MagicMock, agent_id: str = "agent-ws") -> GatewayConnection:
    return GatewayConnection(
        connection_id="conn-1",
        subnet_id="public",
        agent_id=agent_id,
        websocket=ws,
    )


@pytest.mark.asyncio
async def test_ws_heartbeat_frame_renews_alive_ttl_via_touch_alive():
    """A HEARTBEAT frame on the gateway WS must schedule
    ``touch_alive(agent_id)`` exactly once and still send the HEARTBEAT_ACK."""
    agent_service = AsyncMock()
    manager = SubnetManager(
        registry=MagicMock(),
        redis_client=AsyncMock(),
        agent_service=agent_service,
    )
    ws = _mock_ws_yielding_one_heartbeat()
    connection = _make_connection(ws)

    with pytest.raises(WebSocketDisconnect):
        await manager._message_loop(connection)

    # Drain the fire-and-forget renewal task. We hold a strong ref to it
    # in ``manager._alive_renewal_tasks`` precisely so this drain is
    # possible — without that the task could be GC'd before the await
    # resolves on Python 3.11+ and the assertion would silently miss.
    pending = list(manager._alive_renewal_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    agent_service.touch_alive.assert_awaited_once_with("agent-ws")
    ws.send_json.assert_awaited_once()
    sent = ws.send_json.await_args.args[0]
    assert sent["type"] == GatewayMessageType.HEARTBEAT_ACK


@pytest.mark.asyncio
async def test_ws_heartbeat_without_agent_service_is_noop_not_crash():
    """Legacy callers construct ``SubnetManager`` without ``agent_service=``.
    The HEARTBEAT branch must complete normally — ACK sent, no
    ``AttributeError`` from dereferencing a ``None`` hook. This is the
    opt-out path the api.py rollout doc promises for downstream forks
    that haven't wired AgentService into their gateway yet."""
    manager = SubnetManager(
        registry=MagicMock(),
        redis_client=AsyncMock(),
        # agent_service omitted on purpose
    )
    ws = _mock_ws_yielding_one_heartbeat()
    connection = _make_connection(ws)

    with pytest.raises(WebSocketDisconnect):
        await manager._message_loop(connection)

    assert manager._alive_renewal_tasks == set()
    ws.send_json.assert_awaited_once()
    sent = ws.send_json.await_args.args[0]
    assert sent["type"] == GatewayMessageType.HEARTBEAT_ACK
