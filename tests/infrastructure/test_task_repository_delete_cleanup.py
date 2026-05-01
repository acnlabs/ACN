"""Regression test for P0-1: RedisTaskRepository.delete must purge all
participation side-car keys, not just the primary task hash.

Before the fix, deleting a task with participants would leak:
- acn:participation:{pid}               (per participation hash)
- acn:task:{id}:participations          (sorted set of pids)
- acn:task:{id}:active_count            (Lua-maintained counter)
- acn:user:{uid}:task:{id}:participations  (per-user-per-task set)
- acn:user:{uid}:all_participations     (append-only list, needs lrem)

P2-D (pipeline optimisation): the three serial loops that caused O(N_pids)
and O(users × pids) round-trips are now batched into non-transactional
pipelines.  Commands are queued synchronously on the pipe and flushed with
a single `await pipe.execute()`.  Tests therefore:
- mock `redis.pipeline()` as a *sync* method returning an async context manager
- check `pipe.*` call counts / args (not `fake_redis.*`)
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _pipe_cm(pipe: MagicMock) -> MagicMock:
    """Return an async-context-manager wrapping *pipe*."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=pipe)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_task_with_no_participants_still_cleans_sidecar_keys():
    """With zero participants, the task-scoped sidecar keys must still be deleted."""
    pipe = MagicMock()
    pipe.execute = AsyncMock(return_value=[])

    fake_redis = AsyncMock()
    fake_redis.pipeline = MagicMock(return_value=_pipe_cm(pipe))
    fake_redis.zrange.return_value = []

    repo = RedisTaskRepository(redis_client=fake_redis)
    repo.find_by_id = AsyncMock(return_value=_make_task())  # type: ignore[method-assign]

    assert await repo.delete("task-001") is True

    # All delete / srem / zrem calls go through the pipeline
    delete_keys = {args for c in pipe.delete.call_args_list for args in c.args}
    assert "acn:task:task-001:participations" in delete_keys
    assert "acn:task:task-001:active_count" in delete_keys
    assert "acn:task:completions:task-001" in delete_keys

    # execute must be called (pipeline flushed)
    pipe.execute.assert_awaited()


@pytest.mark.asyncio
async def test_delete_task_purges_participation_hashes_and_user_indices():
    """delete() must clean per-participation hashes and user-scoped indices."""
    p1 = _make_participation("p1", "user-a")
    p2 = _make_participation("p2", "user-b")

    pipe = MagicMock()
    # execute() is called up to 3 times:
    #   call 1 → step-1 hgetall results  (list of raw dicts for p1, p2)
    #   call 2 → step-2+3 task deletions  (return values ignored)
    #   call 3 → step-4 user-index lrems  (return values ignored)
    pipe.execute = AsyncMock(side_effect=[
        [],   # step-1: empty — we mock _dict_to_participation instead
        [],   # step-2+3
        [],   # step-4
    ])

    fake_redis = AsyncMock()
    fake_redis.pipeline = MagicMock(return_value=_pipe_cm(pipe))
    fake_redis.zrange.return_value = ["p1", "p2"]

    repo = RedisTaskRepository(redis_client=fake_redis)
    repo.find_by_id = AsyncMock(return_value=_make_task())  # type: ignore[method-assign]

    # Patch _dict_to_participation so step-1 pipeline execution can return
    # the prepared Participation objects without needing a real Redis hash.
    call_count = 0

    def _fake_dict_to_participation(raw):
        nonlocal call_count
        result = [p1, p2][call_count]
        call_count += 1
        return result

    repo._dict_to_participation = _fake_dict_to_participation  # type: ignore[method-assign]

    # Override execute side_effect to yield participation-like raw dicts on first call
    # so the loop in step-1 iterates twice.
    pipe.execute.side_effect = [
        [{"participation_id": "p1"}, {"participation_id": "p2"}],
        [],
        [],
    ]

    assert await repo.delete("task-001") is True

    # Step-1: hgetall called once per pid
    hgetall_keys = [c.args[0] for c in pipe.hgetall.call_args_list]
    assert "acn:participation:p1" in hgetall_keys
    assert "acn:participation:p2" in hgetall_keys

    # Step-2+3: per-participation hashes deleted in one pipeline call
    delete_args_flat = [args for c in pipe.delete.call_args_list for args in c.args]
    assert "acn:participation:p1" in delete_args_flat
    assert "acn:participation:p2" in delete_args_flat

    # Step-4: user-scoped sets and lrems — all through pipeline
    delete_args_flat_all = [args for c in pipe.delete.call_args_list for args in c.args]
    assert "acn:user:user-a:task:task-001:participations" in delete_args_flat_all
    assert "acn:user:user-b:task:task-001:participations" in delete_args_flat_all

    lrem_calls = [c.args for c in pipe.lrem.call_args_list]
    assert ("acn:user:user-a:all_participations", 0, "p1") in lrem_calls
    assert ("acn:user:user-a:all_participations", 0, "p2") in lrem_calls
    assert ("acn:user:user-b:all_participations", 0, "p1") in lrem_calls
    assert ("acn:user:user-b:all_participations", 0, "p2") in lrem_calls


@pytest.mark.asyncio
async def test_delete_handles_bytes_pids_from_redis():
    """Some redis-py configs return zrange members as bytes; we must decode."""
    p1 = _make_participation("p1", "user-a")

    pipe = MagicMock()
    pipe.execute = AsyncMock(side_effect=[
        [{"participation_id": "p1"}],  # step-1 hgetall result
        [],                             # step-2+3
        [],                             # step-4
    ])

    fake_redis = AsyncMock()
    fake_redis.pipeline = MagicMock(return_value=_pipe_cm(pipe))
    fake_redis.zrange.return_value = [b"p1"]  # bytes from redis-py

    repo = RedisTaskRepository(redis_client=fake_redis)
    repo.find_by_id = AsyncMock(return_value=_make_task())  # type: ignore[method-assign]
    repo._dict_to_participation = MagicMock(return_value=p1)  # type: ignore[method-assign]

    assert await repo.delete("task-001") is True

    # hgetall must receive a str key, not bytes repr
    hgetall_keys = [c.args[0] for c in pipe.hgetall.call_args_list]
    assert "acn:participation:p1" in hgetall_keys
    assert b"acn:participation:p1" not in hgetall_keys

    # delete key must be built with str
    delete_args_flat = [args for c in pipe.delete.call_args_list for args in c.args]
    assert "acn:participation:p1" in delete_args_flat
    assert b"acn:participation:p1" not in delete_args_flat
