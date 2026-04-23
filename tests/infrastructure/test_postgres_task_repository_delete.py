"""Regression test for P0-2: PostgresTaskRepository.delete must purge
the Redis side-car keys (acn:task:{id}:active_count and
acn:task:completions:{id}) that otherwise live forever.

Participation rows are removed by the `participations.task_id` FK with
ON DELETE CASCADE (alembic revision 1e400bcfd4ec), so we deliberately
don't issue a separate DELETE for them — the test below pins that
contract so a future refactor doesn't silently regress it.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.sql import Delete

from acn.infrastructure.persistence.postgres.models import TaskModel
from acn.infrastructure.persistence.postgres.task_repository import (
    PostgresTaskRepository,
)


def _make_session_factory(rowcount: int):
    """Build a fake async_sessionmaker that yields a session supporting
    `async with` and recording .execute() / .commit() calls.
    """
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None

    task_result = MagicMock()
    task_result.rowcount = rowcount
    session.execute.side_effect = [task_result]

    factory = MagicMock(return_value=session)
    return factory, session


@pytest.mark.asyncio
async def test_delete_only_issues_one_sql_statement_and_purges_redis():
    factory, session = _make_session_factory(rowcount=1)
    fake_redis = AsyncMock()
    repo = PostgresTaskRepository(session_factory=factory, redis_client=fake_redis)

    assert await repo.delete("task-001") is True

    # Exactly one DELETE — against tasks. Participation rows cascade from
    # the DB-level FK; duplicating the DELETE in Python would hide that
    # dependency and diverge on a future schema change.
    assert session.execute.await_count == 1
    stmt = session.execute.await_args_list[0].args[0]
    assert isinstance(stmt, Delete)
    assert stmt.table.name == TaskModel.__tablename__
    session.commit.assert_awaited_once()

    # Redis side-cars purged in a single DEL
    fake_redis.delete.assert_awaited_once_with(
        "acn:task:task-001:active_count",
        "acn:task:completions:task-001",
    )


@pytest.mark.asyncio
async def test_delete_missing_task_does_not_touch_redis():
    factory, session = _make_session_factory(rowcount=0)
    fake_redis = AsyncMock()
    repo = PostgresTaskRepository(session_factory=factory, redis_client=fake_redis)

    assert await repo.delete("nope") is False

    # SQL layer still fires (idempotent). Redis must stay quiet so we don't
    # erase side-cars of a concurrently-created replacement task.
    assert session.execute.await_count == 1
    fake_redis.delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_commits_before_redis_cleanup():
    """Ordering matters: if commit fails we must not touch Redis, or we'd
    destroy side-cars of a still-live task."""
    factory, session = _make_session_factory(rowcount=1)
    fake_redis = AsyncMock()

    order: list[str] = []
    session.commit.side_effect = lambda: order.append("commit") or None

    async def record_delete(*args, **kwargs):
        order.append("redis_delete")

    fake_redis.delete.side_effect = record_delete

    repo = PostgresTaskRepository(session_factory=factory, redis_client=fake_redis)
    await repo.delete("task-001")

    assert order == ["commit", "redis_delete"]
