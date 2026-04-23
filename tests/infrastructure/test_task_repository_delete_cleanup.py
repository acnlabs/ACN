"""Regression test for P0-1: RedisTaskRepository.delete must purge all
participation side-car keys, not just the primary task hash.

Before the fix, deleting a task with participants would leak:
- acn:participation:{pid}               (per participation hash)
- acn:task:{id}:participations          (sorted set of pids)
- acn:task:{id}:active_count            (Lua-maintained counter)
- acn:user:{uid}:task:{id}:participations  (per-user-per-task set)
- acn:user:{uid}:all_participations     (append-only list, needs lrem)
"""

from datetime import datetime
from unittest.mock import AsyncMock, call

import pytest

from acn.core.entities.task import (
    Participation,
    ParticipationStatus,
    Task,
    TaskStatus,
)
from acn.infrastructure.persistence.redis.task_repository import (
    RedisTaskRepository,
)


def _make_task(**overrides) -> Task:
    defaults: dict = {
        "task_id": "task-001",
        "creator_type": "human",
        "creator_id": "creator-001",
        "creator_name": "Alice",
        "title": "Test",
        "description": "",
        "reward": "10",
        "reward_currency": "ap_points",
        "max_participants": 5,
        "status": TaskStatus.OPEN,
        "require_join_approval": False,
        "required_tags": [],
    }
    defaults.update(overrides)
    return Task(**defaults)


def _make_participation(pid: str, uid: str, task_id: str = "task-001") -> Participation:
    return Participation(
        participation_id=pid,
        task_id=task_id,
        participant_id=uid,
        participant_name=f"agent-{uid}",
        participant_type="agent",
        status=ParticipationStatus.ACTIVE,
        joined_at=datetime(2025, 6, 1),
    )


@pytest.mark.asyncio
async def test_delete_task_with_no_participants_still_cleans_sidecar_keys():
    fake_redis = AsyncMock()
    fake_redis.zrange.return_value = []
    repo = RedisTaskRepository(redis_client=fake_redis)
    repo.find_by_id = AsyncMock(return_value=_make_task())  # type: ignore[method-assign]

    assert await repo.delete("task-001") is True

    # Even with zero participants, the task-scoped side-car keys must be deleted
    delete_calls = fake_redis.delete.await_args_list
    assert call("acn:task:task-001:participations") in delete_calls
    assert call("acn:task:task-001:active_count") in delete_calls
    assert call("acn:task:completions:task-001") in delete_calls


@pytest.mark.asyncio
async def test_delete_task_purges_participation_hashes_and_user_indices():
    fake_redis = AsyncMock()
    fake_redis.zrange.return_value = ["p1", "p2"]

    repo = RedisTaskRepository(redis_client=fake_redis)
    repo.find_by_id = AsyncMock(return_value=_make_task())  # type: ignore[method-assign]

    # Two different users so we cover the fan-out in the user cleanup loop
    repo.find_participation_by_id = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            _make_participation("p1", "user-a"),
            _make_participation("p2", "user-b"),
        ]
    )

    assert await repo.delete("task-001") is True

    delete_calls = fake_redis.delete.await_args_list

    # Per-participation hashes deleted in one call
    assert call("acn:participation:p1", "acn:participation:p2") in delete_calls

    # Per-user-per-task sets deleted (one per participant)
    assert call("acn:user:user-a:task:task-001:participations") in delete_calls
    assert call("acn:user:user-b:task:task-001:participations") in delete_calls

    # Global append-only list needs lrem, not delete (it spans all tasks)
    lrem_calls = fake_redis.lrem.await_args_list
    assert call("acn:user:user-a:all_participations", 0, "p1") in lrem_calls
    assert call("acn:user:user-a:all_participations", 0, "p2") in lrem_calls
    assert call("acn:user:user-b:all_participations", 0, "p1") in lrem_calls
    assert call("acn:user:user-b:all_participations", 0, "p2") in lrem_calls


@pytest.mark.asyncio
async def test_delete_handles_bytes_pids_from_redis():
    """Some redis-py configs return zrange members as bytes; we must decode."""
    fake_redis = AsyncMock()
    fake_redis.zrange.return_value = [b"p1"]

    repo = RedisTaskRepository(redis_client=fake_redis)
    repo.find_by_id = AsyncMock(return_value=_make_task())  # type: ignore[method-assign]
    repo.find_participation_by_id = AsyncMock(  # type: ignore[method-assign]
        return_value=_make_participation("p1", "user-a")
    )

    assert await repo.delete("task-001") is True

    # find_participation_by_id must receive a str, not bytes
    repo.find_participation_by_id.assert_awaited_once_with("p1")
    # delete key must be built with str, not bytes repr
    assert call("acn:participation:p1") in fake_redis.delete.await_args_list
