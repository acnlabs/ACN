"""Regression test for P1-3: acn:user:{uid}:all_participations must be
capped via ltrim after every lpush.

Read path only exposes the head (`lrange(0, limit-1)`, limit<=50). Without
a cap a power-user with 10^5 participations holds ~50MB in one key, and at
population scale that line-item alone can grow past TB.
"""

from datetime import datetime
from unittest.mock import AsyncMock, call

import pytest

from acn.core.entities.task import (
    Participation,
    ParticipationStatus,
)
from acn.infrastructure.persistence.redis.task_repository import (
    _ALL_PARTICIPATIONS_CAP,
    RedisTaskRepository,
)


def _make_participation(**overrides) -> Participation:
    defaults: dict = {
        "participation_id": "p-001",
        "task_id": "task-001",
        "participant_id": "user-a",
        "participant_name": "bot",
        "participant_type": "agent",
        "status": ParticipationStatus.APPLIED,
        "joined_at": datetime(2026, 1, 1),
    }
    defaults.update(overrides)
    return Participation(**defaults)


@pytest.mark.asyncio
async def test_add_application_trims_user_index_after_lpush():
    fake_redis = AsyncMock()
    repo = RedisTaskRepository(redis_client=fake_redis)

    await repo.add_application("task-001", _make_participation())

    index_key = "acn:user:user-a:all_participations"
    assert call(index_key, "p-001") in fake_redis.lpush.await_args_list
    assert (
        call(index_key, 0, _ALL_PARTICIPATIONS_CAP - 1)
        in fake_redis.ltrim.await_args_list
    )


@pytest.mark.asyncio
async def test_atomic_join_task_trims_user_index_after_lpush():
    """The Lua-based join path also writes all_participations; it must
    trim too, or the cap applied only on `add_application` would be
    bypassed for the far more common open-task join flow.
    """
    fake_redis = AsyncMock()
    repo = RedisTaskRepository(redis_client=fake_redis)

    # Stub the Lua script layer: register_script returns something
    # callable that, when awaited, yields the participation_id bytes
    # that Redis would normally return.
    lua_script = AsyncMock(return_value=b"p-001")
    repo._join_script = lua_script

    part = _make_participation(status=ParticipationStatus.ACTIVE)
    returned = await repo.atomic_join_task(
        task_id="task-001",
        participation=part,
        max_completions=None,
        allow_repeat=False,
    )
    assert returned == "p-001"

    index_key = "acn:user:user-a:all_participations"
    assert call(index_key, "p-001") in fake_redis.lpush.await_args_list
    assert (
        call(index_key, 0, _ALL_PARTICIPATIONS_CAP - 1)
        in fake_redis.ltrim.await_args_list
    )


@pytest.mark.asyncio
async def test_cap_chosen_well_above_read_limit():
    """If _ALL_PARTICIPATIONS_CAP ever drops near the default read
    `limit=50` we'll silently start losing in-flight entries during
    concurrent writes. Pin the invariant so reviewers notice.
    """
    assert _ALL_PARTICIPATIONS_CAP >= 100
