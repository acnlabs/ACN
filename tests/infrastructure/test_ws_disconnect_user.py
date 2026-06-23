"""Unit tests for ``WebSocketManager.disconnect_user`` (P3 §15.7 C2).

When an agent's API key rotates on an ownership hand-off, evicting the auth
cache stops the *old* key from passing a fresh handshake — but a socket that
authenticated *before* the rotation stays open and keeps relaying traffic.
``disconnect_user`` force-closes those live tails so the previous owner's
instance is forced to reconnect with the now-dead key.

Behaviours under test:
- closes every connection held by the target principal and returns the count;
- sends a structured close frame (code/reason);
- removes the connections from the manager registry;
- leaves connections of other principals untouched;
- is a no-op (returns 0) for a principal with no live connection.
"""

from __future__ import annotations

import pytest

from acn.infrastructure.messaging.websocket_manager import WebSocketManager


class FakeWebSocket:
    """Minimal WebSocket double recording close(code, reason)."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str | None = None

    async def accept(self) -> None:  # pragma: no cover - trivial
        return None

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = reason


@pytest.fixture
def manager() -> WebSocketManager:
    return WebSocketManager(redis_client=object())  # type: ignore[arg-type]


async def _connect(manager: WebSocketManager, agent_id: str) -> tuple[FakeWebSocket, str]:
    ws = FakeWebSocket()
    conn_id = await manager.connect(ws, user_id=agent_id)  # type: ignore[arg-type]
    return ws, conn_id


@pytest.mark.asyncio
async def test_disconnect_user_closes_all_and_returns_count(manager: WebSocketManager) -> None:
    agent_id = "agent-rotated"
    ws1, cid1 = await _connect(manager, agent_id)
    ws2, cid2 = await _connect(manager, agent_id)

    closed = await manager.disconnect_user(agent_id, reason="ownership_transfer")

    assert closed == 2
    assert ws1.closed and ws2.closed
    # Structured close frame so a conformant client can tell rotation from a drop.
    assert ws1.close_code == 4001
    assert ws1.close_reason == "ownership_transfer"
    # Removed from the registry — no live tail remains.
    assert cid1 not in manager._connections
    assert cid2 not in manager._connections
    assert not manager.is_user_connected(agent_id)


@pytest.mark.asyncio
async def test_disconnect_user_leaves_other_principals(manager: WebSocketManager) -> None:
    victim = "agent-a"
    bystander = "agent-b"
    ws_a, _ = await _connect(manager, victim)
    ws_b, cid_b = await _connect(manager, bystander)

    closed = await manager.disconnect_user(victim, reason="key_rotated")

    assert closed == 1
    assert ws_a.closed
    assert not ws_b.closed
    assert cid_b in manager._connections
    assert manager.is_user_connected(bystander)


@pytest.mark.asyncio
async def test_disconnect_user_noop_when_offline(manager: WebSocketManager) -> None:
    assert await manager.disconnect_user("nobody") == 0
