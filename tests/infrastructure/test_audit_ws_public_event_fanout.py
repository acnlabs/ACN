"""End-to-end test for the public system-event realtime channel.

Verifies the full chain that backs the public WebSocket feed:

1. ``AuditLogger.log_event`` is called with a public-eligible event.
2. The logger writes to the audit stream and then publishes to the
   shared Redis Pub/Sub channel ``acn:ws:broadcast:system-events``.
3. ``WebSocketManager._listen_pubsub`` receives the published message
   and fans it out to every local connection subscribed to the
   ``broadcast:system-events`` channel.
4. The client receives a JSON frame whose body matches the fixed
   ``to_public_broadcast_payload`` schema.

A non-eligible event must NOT reach the WebSocket — that pins the
filter contract end-to-end so a regression in
``publish_public_system_event`` cannot silently leak internal-only
events to the public channel.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fakeredis import aioredis as _fakeredis_async

from acn.infrastructure.messaging.websocket_manager import (
    Connection,
    WebSocketManager,
)
from acn.monitoring.audit import AuditEventType, AuditLogger


@pytest.fixture
async def shared_redis() -> AsyncGenerator[Any, None]:
    """A single fakeredis async client shared by AuditLogger and WebSocketManager.

    Both components must talk to the same backing fakeredis server so the
    pubsub message published by ``AuditLogger`` is delivered to the
    pubsub subscription registered by ``WebSocketManager``.
    """
    client = _fakeredis_async.FakeRedis(decode_responses=False)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


async def _register_fake_connection(
    ws_manager: WebSocketManager,
    *,
    connection_id: str,
    user_id: str | None = None,
) -> AsyncMock:
    """Inject a fake Connection bypassing the real ``websocket.accept()`` path.

    Returns the AsyncMock websocket so the test can read the frames the
    manager pushed via ``send_json``.
    """
    fake_ws = AsyncMock()
    connection = Connection(
        connection_id=connection_id,
        websocket=fake_ws,
        user_id=user_id,
    )
    ws_manager._connections[connection_id] = connection
    return fake_ws


async def _wait_for_send_json_call(
    fake_ws: AsyncMock,
    *,
    matcher,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Poll the fake websocket until ``matcher`` returns True or timeout.

    Polling rather than a single sleep keeps the test resilient to small
    scheduling jitter between the publisher coroutine and the pubsub
    listener task without forcing an artificially long fixed delay.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        for call in fake_ws.send_json.await_args_list:
            payload = call.args[0]
            if matcher(payload):
                return payload
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                "fake websocket did not receive a matching frame within "
                f"{timeout}s. Captured: {fake_ws.send_json.await_args_list!r}"
            )
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_eligible_event_fans_out_to_public_ws_channel(shared_redis) -> None:
    audit = AuditLogger(redis=shared_redis)
    ws_manager = WebSocketManager(redis_client=shared_redis)
    await ws_manager.start()
    try:
        fake_ws = await _register_fake_connection(
            ws_manager, connection_id="conn-public-1"
        )
        await ws_manager.subscribe("conn-public-1", "broadcast:system-events")

        event_id = await audit.log_event(
            event_type=AuditEventType.AGENT_REGISTERED,
            target_id="agent-realtime-7",
            target_type="agent",
            details={
                "source": "join",
                "visibility": "real",
                "public_broadcast_eligible": True,
                "internal_only": "must-not-leak",
            },
        )

        frame = await _wait_for_send_json_call(
            fake_ws,
            matcher=lambda f: f.get("type") == "public_system_event"
            and f.get("event", {}).get("event_id") == event_id,
        )

        assert frame["event"] == {
            "schema_version": 1,
            "event_id": event_id,
            "timestamp": frame["event"]["timestamp"],
            "event_type": "agent_registered",
            "agent_id": "agent-realtime-7",
            "source": "join",
        }
    finally:
        await ws_manager.stop()


@pytest.mark.asyncio
async def test_non_eligible_event_does_not_reach_public_ws_channel(
    shared_redis,
) -> None:
    audit = AuditLogger(redis=shared_redis)
    ws_manager = WebSocketManager(redis_client=shared_redis)
    await ws_manager.start()
    try:
        fake_ws = await _register_fake_connection(
            ws_manager, connection_id="conn-public-2"
        )
        await ws_manager.subscribe("conn-public-2", "broadcast:system-events")

        await audit.log_event(
            event_type=AuditEventType.SUBNET_CREATED,
            target_id="subnet-private-1",
            target_type="subnet",
            details={
                "is_private": True,
                "join_policy": "approval",
                "public_broadcast_eligible": False,
            },
        )

        # Give the listener some time to definitively NOT push anything.
        await asyncio.sleep(0.15)

        public_frames = [
            call.args[0]
            for call in fake_ws.send_json.await_args_list
            if call.args and call.args[0].get("type") == "public_system_event"
        ]
        assert public_frames == [], (
            "non-eligible audit events must not fan out to the public WS channel; "
            f"got {public_frames!r}"
        )
    finally:
        await ws_manager.stop()
