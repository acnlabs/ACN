"""Regression: alive (Redis) is the single source of truth for "online".

Background — see issue/discussion captured in the change introducing
``AgentService._filter_by_status``:

Before this refactor, ``search_agents("online")`` applied two filters in
sequence:

1. DB column ``Agent.status == "online"``
2. Redis alive key present

That dual-source design drifted whenever the two sides disagreed. The
concrete trigger reported by users: implicit-heartbeat (every authed
HTTP request schedules ``AgentService.touch_alive``) refreshed the
Redis alive TTL on join/subnet/business calls, but it never wrote the
DB column. An agent whose DB row had been swept to ``offline`` by the
30-min ``_heartbeat_watchdog`` therefore stayed *invisible* in
``search_agents("online")`` until the agent issued an explicit
``POST /heartbeat`` — defeating the implicit-heartbeat design.

This test guards against that regression by simulating exactly that
combination (DB.status = OFFLINE + alive present) and asserting that
the agent is returned.
"""

from __future__ import annotations

import pytest

from acn.core.entities import Agent
from acn.services import AgentService


def _offline_db_agent(agent_id: str = "agent-drifted") -> Agent:
    """An agent that exists in the repository.

    Before Phase 2, this fixture also pinned ``status=AgentStatus.OFFLINE``
    on the entity to prove the old watchdog-stamped column was *not*
    consulted by the read side. The column is now gone entirely, so the
    contradictory DB state no longer needs to be set up — but the test
    intent (alive key is the only signal that flips ``status="online"``
    in responses) remains valuable, hence the tests are kept.
    """
    return Agent(
        agent_id=agent_id,
        owner="user-x",
        name="Drifted Agent",
        endpoint="https://drifted.example.com",
        tags=["coding"],
    )


@pytest.mark.asyncio
async def test_search_online_includes_db_offline_but_alive_in_redis(
    mock_agent_repository,
) -> None:
    """The bug: DB.status=OFFLINE + Redis alive=present should still surface."""
    drifted = _offline_db_agent()
    mock_agent_repository.find_by_tags.return_value = [drifted]
    mock_agent_repository.filter_alive.return_value = {drifted.agent_id}

    service = AgentService(mock_agent_repository)
    agents = await service.search_agents(tags=["coding"], status="online")

    assert [a.agent_id for a in agents] == [drifted.agent_id], (
        "Agent with refreshed Redis alive must be visible in 'online' "
        "listings even when its legacy DB.status column is stale."
    )


@pytest.mark.asyncio
async def test_search_online_excludes_alive_missing_even_if_db_online(
    mock_agent_repository,
) -> None:
    """An agent without an alive key must NOT show up under ``status='online'``."""
    stale_online = Agent(
        agent_id="agent-stale",
        owner="user-y",
        name="Stale Online",
        endpoint="https://stale.example.com",
        tags=["coding"],
    )
    mock_agent_repository.find_by_tags.return_value = [stale_online]
    mock_agent_repository.filter_alive.return_value = set()  # no alive

    service = AgentService(mock_agent_repository)
    agents = await service.search_agents(tags=["coding"], status="online")

    assert agents == [], (
        "Missing alive key must not appear under status='online' — the alive "
        "key is the single source of truth."
    )


@pytest.mark.asyncio
async def test_search_offline_is_complement_of_alive(
    mock_agent_repository,
) -> None:
    """``status="offline"`` returns candidates whose alive key is absent."""
    alive_agent = Agent(
        agent_id="agent-alive",
        owner="u",
        name="Alive",
        endpoint="https://alive.example.com",
        tags=["coding"],
    )
    dead_agent = Agent(
        agent_id="agent-dead",
        owner="u",
        name="Dead",
        endpoint="https://dead.example.com",
        tags=["coding"],
    )
    mock_agent_repository.find_by_tags.return_value = [alive_agent, dead_agent]
    mock_agent_repository.filter_alive.return_value = {alive_agent.agent_id}

    service = AgentService(mock_agent_repository)
    agents = await service.search_agents(tags=["coding"], status="offline")

    assert [a.agent_id for a in agents] == [dead_agent.agent_id], (
        "An agent with DB.status=ONLINE but no alive key must be reported "
        "as offline — DB.status is not consulted on the read path."
    )


@pytest.mark.asyncio
async def test_search_all_skips_alive_lookup(mock_agent_repository) -> None:
    """``status="all"`` must NOT issue an alive lookup (perf + semantics)."""
    a = _offline_db_agent("a")
    b = _offline_db_agent("b")
    mock_agent_repository.find_by_tags.return_value = [a, b]

    service = AgentService(mock_agent_repository)
    agents = await service.search_agents(tags=["coding"], status="all")

    assert {x.agent_id for x in agents} == {"a", "b"}
    mock_agent_repository.filter_alive.assert_not_called()


@pytest.mark.asyncio
async def test_is_alive_and_batch_alive_wrap_filter_alive(
    mock_agent_repository,
) -> None:
    """The new helpers route through ``repository.filter_alive`` only."""
    mock_agent_repository.filter_alive.return_value = {"x"}

    service = AgentService(mock_agent_repository)

    assert await service.is_alive("x") is True
    assert await service.is_alive("y") is False
    assert await service.batch_alive(["x", "y"]) == {"x"}
    assert await service.batch_alive([]) == set()


@pytest.mark.asyncio
async def test_search_online_immediately_drops_agent_when_alive_expires(
    mock_agent_repository,
) -> None:
    """Alive TTL expiry must reflect in the next listing — no watchdog delay.

    Before this refactor the 30-min ``_heartbeat_watchdog`` was the only
    code path that propagated "alive disappeared" into a value the read
    side observed (``Agent.status = OFFLINE``). After the refactor the
    read side queries Redis directly, so an alive key vanishing on tick
    N+1 must remove the agent from the next ``status='online'`` listing
    immediately — proving the watchdog is genuinely unnecessary.
    """
    agent = _offline_db_agent("agent-blip")
    mock_agent_repository.find_by_tags.return_value = [agent]
    mock_agent_repository.filter_alive.side_effect = [
        # 1st call: alive
        {agent.agent_id},
        # 2nd call: alive key has just expired (TTL hit or Redis blip)
        set(),
    ]

    service = AgentService(mock_agent_repository)

    first = await service.search_agents(tags=["coding"], status="online")
    second = await service.search_agents(tags=["coding"], status="online")

    assert [a.agent_id for a in first] == [agent.agent_id]
    assert second == [], (
        "An alive key expiring between two calls must make the agent "
        "fall out of 'online' results on the very next call — no watchdog "
        "lag, since the read side consults Redis directly."
    )
