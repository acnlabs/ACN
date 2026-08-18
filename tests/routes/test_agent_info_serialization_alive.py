"""Regression: AgentInfo.status is derived from Redis alive, not DB column.

Companion to ``tests/services/test_agent_service_alive_single_source.py``:
that file proves the *read filter* (``search_agents``) trusts only alive;
this file proves the *serialization* path that turns an ``Agent`` entity
into the wire-shape ``AgentInfo`` also trusts only alive.

End-to-end of the original bug: an agent's DB.status had drifted to
``offline`` because the 30-min ``_heartbeat_watchdog`` had swept it
while implicit-heartbeat (per-request ``touch_alive``) had since
refreshed its Redis alive TTL. Before this refactor every listing
endpoint inherited the stale ``Agent.status.value`` into its response.
After this refactor the route-layer helpers
``_agent_entity_to_info`` / ``_agent_entities_to_infos`` consult
Redis alive exclusively, so the API surface matches what users see in
the dashboard.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from acn.core.entities import Agent
from acn.routes.registry import (
    _agent_entities_to_infos,
    _agent_entity_to_info,
    _agent_entity_to_info_with_alive,
)


def _drifted_agent(agent_id: str = "agent-drifted") -> Agent:
    """An agent fixture used by the alive-as-single-source regressions.

    The original Phase 1 version of this fixture also pinned
    ``status=AgentStatus.OFFLINE`` to prove the serializer ignored the
    legacy DB column. The column is gone in Phase 2, so the
    contradictory state no longer needs to be constructed — the
    serializer contract (``is_online`` arg is the *only* signal) is
    still what we want pinned.
    """
    return Agent(
        agent_id=agent_id,
        owner="user-x",
        name="Drifted Agent",
        endpoint="https://drifted.example.com",
        tags=["coding"],
    )


def test_agent_info_exposes_reception_mode_not_full_policy() -> None:
    """GET /agents/{id} must show mode, never reject_reason / allowlist."""
    agent = Agent(
        agent_id="agent-closed",
        owner="user-x",
        name="Closed",
        endpoint="https://closed.example.com",
        tags=["coding"],
        communication_policy={"mode": "manifest", "reject_reason": "on leave"},
    )
    info = _agent_entity_to_info(agent, is_online=True)
    dumped = info.model_dump()
    assert info.reception_mode == "manifest"
    assert dumped["reception_mode"] == "manifest"
    assert "communication_policy" not in dumped
    assert "reject_reason" not in dumped


def test_agent_info_reception_mode_defaults_open() -> None:
    info = _agent_entity_to_info(_drifted_agent(), is_online=True)
    assert info.reception_mode == "open"


def test_agent_entity_to_info_sync_helper_uses_is_online_arg() -> None:
    """Sync helper takes ``is_online`` verbatim — it has no other input."""
    drifted = _drifted_agent()

    info_online = _agent_entity_to_info(drifted, is_online=True)
    info_offline = _agent_entity_to_info(drifted, is_online=False)

    assert info_online.status == "online", (
        "is_online=True must produce 'online' regardless of DB.status."
    )
    assert info_offline.status == "offline"


@pytest.mark.asyncio
async def test_agent_entity_to_info_with_alive_queries_service() -> None:
    """The single-shot async wrapper routes through ``AgentService.is_alive``."""
    drifted = _drifted_agent()
    agent_service = AsyncMock()
    agent_service.is_alive = AsyncMock(return_value=True)

    info = await _agent_entity_to_info_with_alive(
        drifted, agent_service=agent_service
    )

    assert info.status == "online"
    agent_service.is_alive.assert_awaited_once_with(drifted.agent_id)


@pytest.mark.asyncio
async def test_agent_entities_to_infos_batches_alive_lookup() -> None:
    """The batch wrapper issues exactly one ``batch_alive`` call."""
    alive = _drifted_agent("alive-1")
    dead = _drifted_agent("dead-1")
    agent_service = AsyncMock()
    agent_service.batch_alive = AsyncMock(return_value={"alive-1"})

    infos = await _agent_entities_to_infos(
        [alive, dead], agent_service=agent_service
    )

    assert [(i.agent_id, i.status) for i in infos] == [
        ("alive-1", "online"),
        ("dead-1", "offline"),
    ]
    agent_service.batch_alive.assert_awaited_once_with(["alive-1", "dead-1"])


@pytest.mark.asyncio
async def test_agent_entities_to_infos_empty_list_skips_lookup() -> None:
    """Empty input must not hit Redis."""
    agent_service = AsyncMock()
    agent_service.batch_alive = AsyncMock()

    infos = await _agent_entities_to_infos([], agent_service=agent_service)

    assert infos == []
    agent_service.batch_alive.assert_not_called()


def test_build_erc8004_registration_file_active_field_uses_is_online() -> None:
    """ERC-8004 ``active`` field is derived from the explicit ``is_online`` arg."""
    from acn.config import Settings
    from acn.services.agent_service import build_erc8004_registration_file

    drifted = _drifted_agent("agent-erc-drifted")
    settings = Settings(
        gateway_base_url="https://api.test",
        erc8004_chain_id=8453,
        erc8004_identity_contract="0xidentity",
    )

    out_online = build_erc8004_registration_file(
        drifted, settings, is_online=True
    )
    out_offline = build_erc8004_registration_file(
        drifted, settings, is_online=False
    )

    assert out_online["active"] is True
    assert out_offline["active"] is False
