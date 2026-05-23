"""Regression tests for ``SubnetManager._handle_registration``.

The 2026-05 AgentRegistry removal rewrote this path: instead of
delegating to ``AgentRegistry.register_agent`` (which baked in
``owner=f"gateway:{slug}"`` + ``status="online"``), the gateway now

1. persists an ``Agent`` entity through ``agent_service.repository.save``
   with **``owner=None``** — explicit product decision to mirror the
   autonomous-join semantics (gateway-attached agents are unclaimed and
   can be claimed later via ``Agent.claim``), and
2. seeds the Redis ``alive`` key by calling
   ``agent_service.touch_alive(agent_id)`` so the first HTTP discovery
   after register already sees the agent online (subsequent WS
   ``HEARTBEAT`` frames renew it — see
   ``test_subnet_manager_implicit_heartbeat.py``).

These tests pin the three invariants the rewrite established:

* ``repository.save`` is called with an ``Agent`` that has ``owner is None``;
* ``agent_service.touch_alive`` is awaited exactly once with the
  connection's ``agent_id``;
* the in-memory ``connection.agent_info.owner`` still carries the
  synthetic ``f"gateway:{slug}"`` marker (legacy DTO compat —
  internal caches stay self-describing without overloading the
  persisted owner field).

Without these tests, a future refactor that re-introduces
``owner=f"gateway:{...}"`` on the persisted ``Agent`` (or drops the
``touch_alive`` seed) would silently regress without breaking any
existing assertion — the alive-as-SSOT contract would degrade to "agent
appears registered but first discovery shows it as offline until a
HEARTBEAT lands".
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.entities import Agent
from acn.infrastructure.messaging.subnet_manager import (
    GatewayConnection,
    GatewayMessageType,
    SubnetManager,
)


def _register_frame(
    *,
    name: str = "test-agent",
    description: str = "fixture agent",
    tags: list[str] | None = None,
    metadata: dict | None = None,
) -> str:
    return json.dumps(
        {
            "type": GatewayMessageType.REGISTER,
            "agent_info": {
                "name": name,
                "description": description,
                "tags": tags or ["demo"],
                "metadata": metadata or {},
            },
        }
    )


def _make_ws_yielding_register() -> MagicMock:
    ws = MagicMock()
    ws.receive_text = AsyncMock(return_value=_register_frame())
    ws.send_json = AsyncMock()
    return ws


def _make_connection(ws: MagicMock, agent_id: str = "agent-1") -> GatewayConnection:
    return GatewayConnection(
        connection_id="conn-1",
        slug="enterprise-a",
        agent_id=agent_id,
        websocket=ws,
    )


def _make_manager() -> tuple[SubnetManager, AsyncMock]:
    agent_service = AsyncMock()
    # ``repository`` is what ``_handle_registration`` reaches into for
    # ``.save(Agent(...))``; the AsyncMock attribute access auto-creates
    # ``.repository.save`` as another AsyncMock, which is exactly what
    # the assertions below want.
    #
    # ``find_by_id`` must return ``None`` to model a brand-new agent that
    # does not yet exist in the registry. The impersonation guard added in
    # ``_handle_registration`` checks the repository before writing; a
    # truthy AsyncMock default would trip it for every new registration.
    agent_service.repository.find_by_id = AsyncMock(return_value=None)
    manager = SubnetManager(
        agent_service=agent_service,
        redis_client=AsyncMock(),
        gateway_base_url="https://gw.test",
    )
    return manager, agent_service


@pytest.mark.asyncio
async def test_handle_registration_persists_agent_with_owner_none():
    """Gateway-registered agent must be persisted with ``owner=None``.

    The product decision (see ``docs/agent-registry-removal.md`` §3.2)
    is that gateway agents are "unclaimed" — they enter the system
    without an owner and can later be ``claim()``-ed by a real user.
    Any future refactor that re-introduces ``owner=f"gateway:{...}"``
    on the persisted entity must update this test consciously, because
    it would re-create the dual-source problem (Owner identity vs
    routing identity collapsed into one mutable string)."""
    manager, agent_service = _make_manager()
    ws = _make_ws_yielding_register()
    connection = _make_connection(ws)

    await manager._handle_registration(connection)

    agent_service.repository.save.assert_awaited_once()
    saved = agent_service.repository.save.await_args.args[0]
    assert isinstance(saved, Agent), "must persist a domain entity, not a DTO"
    assert saved.owner is None, (
        "gateway-registered agent must have owner=None — gateway "
        "namespacing belongs on the in-memory DTO, not on the persisted "
        "ownership field"
    )
    assert saved.agent_id == "agent-1"
    assert saved.subnet_ids == ["enterprise-a"]
    assert "gateway" in saved.metadata
    assert saved.metadata["slug"] == "enterprise-a"


@pytest.mark.asyncio
async def test_handle_registration_seeds_alive_key_via_touch_alive():
    """Register must seed the Redis alive key, not wait for the first
    HEARTBEAT frame.

    Without this seed, a discovery query landing in the gap between
    register-ack and the first heartbeat would see the freshly-registered
    agent as offline — a flaky-by-design state that would be very hard
    to diagnose in production."""
    manager, agent_service = _make_manager()
    ws = _make_ws_yielding_register()
    connection = _make_connection(ws, agent_id="agent-seeded")

    await manager._handle_registration(connection)

    agent_service.touch_alive.assert_awaited_once_with("agent-seeded")


@pytest.mark.asyncio
async def test_handle_registration_sets_in_memory_owner_to_gateway_marker():
    """``connection.agent_info.owner`` keeps the synthetic
    ``gateway:{slug}`` string so the in-memory DTO stays
    self-describing.

    This is the deliberate split from the persisted entity (which has
    ``owner=None`` — see test above). The DTO marker is used only by
    internal cache inspection / debug logs; it never leaks to the
    persisted store and never authenticates a request."""
    manager, _agent_service = _make_manager()
    ws = _make_ws_yielding_register()
    connection = _make_connection(ws)

    await manager._handle_registration(connection)

    assert connection.agent_info is not None
    assert connection.agent_info.owner == "gateway:enterprise-a"
    assert connection.agent_info.agent_id == "agent-1"
