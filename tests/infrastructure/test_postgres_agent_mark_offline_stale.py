"""Regression test for P0-4: PostgresAgentRepository.mark_offline_stale

Previous implementation called `find_all()` which loaded every agent row
into memory, making the periodic heartbeat-sweep unusable past ~100k
rows. The new implementation must:

1. Only query rows currently in ONLINE state.
2. Stream those rows in batches (no single unbounded result set).
3. Update only the stale subset of each batch.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.sql import Select, Update

from acn.core.entities.agent import AgentStatus
from acn.infrastructure.persistence.postgres.agent_repository import (
    PostgresAgentRepository,
)


def _session_yielding(execute_side_effects):
    """Build a fake async session usable via `async with factory()`."""
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    session.execute.side_effect = execute_side_effects
    return session


def _select_result(agent_ids):
    scalar_proxy = MagicMock()
    scalar_proxy.all.return_value = list(agent_ids)
    result = MagicMock()
    result.scalars.return_value = scalar_proxy
    return result


def _update_result():
    result = MagicMock()
    result.rowcount = 1
    return result


@pytest.mark.asyncio
async def test_returns_zero_when_no_online_agents():
    empty_session = _session_yielding([_select_result([])])
    factory = MagicMock(return_value=empty_session)
    repo = PostgresAgentRepository(session_factory=factory, redis_client=AsyncMock())

    assert await repo.mark_offline_stale(batch_size=10) == 0
    # Single SELECT, no UPDATE
    assert empty_session.execute.await_count == 1
    empty_session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_only_queries_online_status():
    """The SELECT must filter by status=ONLINE so a million OFFLINE rows
    don't get dragged through memory every tick."""
    session = _session_yielding([_select_result([])])
    factory = MagicMock(return_value=session)
    repo = PostgresAgentRepository(session_factory=factory, redis_client=AsyncMock())

    await repo.mark_offline_stale(batch_size=100)

    stmt = session.execute.await_args_list[0].args[0]
    assert isinstance(stmt, Select)
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert f"status = '{AgentStatus.ONLINE.value}'" in compiled


@pytest.mark.asyncio
async def test_pages_through_multiple_batches_and_updates_only_stale():
    # Build one session per SQL statement to keep side_effect lists tidy
    select_batch_1 = _session_yielding([_select_result(["a1", "a2", "a3"])])
    update_batch_1 = _session_yielding([_update_result()])
    select_batch_2 = _session_yielding([_select_result(["a4"])])  # last batch (< batch_size)
    # batch 2 has no stale rows → no UPDATE session needed

    factory = MagicMock(side_effect=[select_batch_1, update_batch_1, select_batch_2])

    fake_redis = AsyncMock()
    repo = PostgresAgentRepository(session_factory=factory, redis_client=fake_redis)

    # a1, a3 are stale; a2, a4 alive
    async def fake_filter_alive(ids):
        alive = {"a2", "a4"}
        return {x for x in ids if x in alive}

    repo.filter_alive = fake_filter_alive  # type: ignore[method-assign]

    total = await repo.mark_offline_stale(batch_size=3)

    assert total == 2  # a1 + a3
    update_stmt = update_batch_1.execute.await_args_list[0].args[0]
    assert isinstance(update_stmt, Update)
    update_batch_1.commit.assert_awaited_once()

    # Second batch had no stale → no third factory call beyond the SELECT
    assert factory.call_count == 3
    # All sessions eventually entered & exited
    select_batch_2.__aexit__.assert_awaited()


@pytest.mark.asyncio
async def test_cursor_advances_even_when_whole_batch_is_alive():
    """If every agent in a batch is alive we still need to advance the
    keyset cursor, otherwise we'd loop forever re-reading the same batch.
    """
    s1 = _session_yielding([_select_result(["a1", "a2"])])  # batch 1: full
    s2 = _session_yielding([_select_result([])])  # batch 2: terminator

    factory = MagicMock(side_effect=[s1, s2])

    repo = PostgresAgentRepository(
        session_factory=factory, redis_client=AsyncMock()
    )
    repo.filter_alive = AsyncMock(return_value={"a1", "a2"})  # type: ignore[method-assign]

    total = await repo.mark_offline_stale(batch_size=2)
    assert total == 0
    # Must have issued exactly two SELECTs and terminated (no infinite loop)
    assert factory.call_count == 2

    second_stmt = s2.execute.await_args_list[0].args[0]
    compiled = str(second_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "agent_id > 'a2'" in compiled
