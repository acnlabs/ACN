"""Real row locks must serialize a submission and a withdrawal in either order.

Each test owns a random schema; it never drops existing business tables.
"""

import asyncio
import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from acn.core.entities.task import Participation
from acn.infrastructure.persistence.postgres.models import ParticipationModel
from acn.infrastructure.persistence.postgres.task_repository import PostgresTaskRepository

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get("ACN_INTEGRATION_PG_URL"), reason="needs isolated test PostgreSQL",
)]


@pytest.fixture
async def pg_repo():
    url = os.environ["ACN_INTEGRATION_PG_URL"]
    schema = "test_withdraw_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as conn:
            await conn.run_sync(ParticipationModel.__table__.create)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        repo = PostgresTaskRepository(factory, AsyncMock())
        part = Participation(
            participation_id="p-lock", task_id="task-lock", participant_id="agent-lock",
            participant_name="Solver", joined_at=datetime.now(UTC),
        )
        await repo.save_participation(part)
        yield repo, factory, part
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def _status(factory):
    async with factory() as session:
        return await session.scalar(select(ParticipationModel.status))


async def test_submission_lock_wins_over_withdrawal(pg_repo):
    repo, factory, _ = pg_repo
    async with factory() as writer:
        async with writer.begin():
            await writer.execute(update(ParticipationModel).values(
                status="submitted", submitted_at=datetime.now(UTC), submission="delivered",
            ))
            pending = asyncio.create_task(repo.atomic_cancel_participation(
                "p-lock", "task-lock", expected_status="active",
            ))
            # The write is uncommitted: cancellation must wait on the real row lock.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), 0.1)
    with pytest.raises(ValueError, match="expected status"):
        await asyncio.wait_for(pending, 3)
    assert await _status(factory) == "submitted"
    repo._redis.decr.assert_not_awaited()


async def test_withdrawal_lock_wins_over_stale_submission(pg_repo):
    repo, factory, stale_part = pg_repo
    stale_part.submit("late delivery")
    async with factory() as writer:
        async with writer.begin():
            await writer.execute(update(ParticipationModel).values(
                status="cancelled", cancelled_at=datetime.now(UTC),
            ))
            pending = asyncio.create_task(repo.save_participation(
                stale_part, expected_status="active",
            ))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), 0.1)
    with pytest.raises(ValueError, match="expected status"):
        await asyncio.wait_for(pending, 3)
    assert await _status(factory) == "cancelled"


async def test_matching_submission_is_saved(pg_repo):
    repo, factory, part = pg_repo
    part.submit("on time")
    await repo.save_participation(part, expected_status="active")
    assert await _status(factory) == "submitted"
