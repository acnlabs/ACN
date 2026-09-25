"""Mode B reply matching stays inside one process.

The deploy command starts a single uvicorn process, so this is enough
today. Two manager objects must not complete each other's wait.
"""

from __future__ import annotations

import asyncio

from acn.infrastructure.messaging.websocket_manager import WebSocketManager


def test_relay_reply_does_not_cross_managers():
    holder = WebSocketManager(redis_client=object())  # type: ignore[arg-type]
    other = WebSocketManager(redis_client=object())  # type: ignore[arg-type]
    waiting: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
    holder._relay_futures["corr-1"] = waiting

    assert other.resolve_relay_response("corr-1", {"ok": True}) is False
    assert waiting.done() is False
    assert holder.resolve_relay_response("corr-1", {"ok": True}) is True
    assert waiting.result() == {"ok": True}
