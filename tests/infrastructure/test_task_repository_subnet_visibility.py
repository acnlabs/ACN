"""Regression tests for P1-6: private-subnet task visibility.

The old implementation tried to read `acn:subnets:all` (never written
anywhere) and `acn:subnet:{sid}` (wrong key — the real one is
`acn:subnets:info:{sid}`). That made `visible_subnet_ids` permanently
empty, so every task with a non-null `subnet_id` was invisible to
every agent — i.e. private subnets were functionally broken.

The new implementation uses the agent's own `subnet_ids` field, read
once via HGET `acn:agents:{id} subnet_ids`.
"""

import json
from unittest.mock import AsyncMock

import pytest

from acn.core.entities.task import Task, TaskStatus
from acn.infrastructure.persistence.redis.task_repository import (
    RedisTaskRepository,
)


def _make_task(**overrides) -> Task:
    defaults: dict = {
        "task_id": "task-001",
        "creator_type": "human",
        "creator_id": "creator-001",
        "creator_name": "Alice",
        "title": "T",
        "description": "",
        "reward": "10",
        "reward_currency": "ap_points",
        "max_participants": 1,
        "status": TaskStatus.OPEN,
        "required_tags": [],
    }
    defaults.update(overrides)
    return Task(**defaults)


def _make_repo(task_by_id: dict[str, Task], open_ids: list[str], agent_subnet_ids):
    """Build a RedisTaskRepository with all reads stubbed."""
    fake_redis = AsyncMock()
    fake_redis.zrevrange.return_value = [t.encode() for t in open_ids]

    # hget("acn:agents:{uid}", "subnet_ids") is the ONLY hget the new
    # visibility path issues. Return a JSON list for agents we know about.
    async def fake_hget(key, field):
        if key.startswith("acn:agents:") and field == "subnet_ids":
            return json.dumps(agent_subnet_ids)
        return None

    fake_redis.hget.side_effect = fake_hget

    repo = RedisTaskRepository(redis_client=fake_redis)
    repo.find_by_id = AsyncMock(side_effect=lambda tid: task_by_id.get(tid))  # type: ignore[method-assign]
    return repo, fake_redis


@pytest.mark.asyncio
async def test_private_subnet_task_visible_to_member():
    """An agent whose subnet_ids include the task's subnet must see the task."""
    t = _make_task(subnet_id="subnet-sec")
    repo, _ = _make_repo(
        task_by_id={"task-001": t},
        open_ids=["task-001"],
        agent_subnet_ids=["public", "subnet-sec"],
    )

    results = await repo.find_open_tasks(requesting_agent_id="agent-a")
    assert [r.task_id for r in results] == ["task-001"]


@pytest.mark.asyncio
async def test_private_subnet_task_hidden_from_non_member():
    t = _make_task(subnet_id="subnet-sec")
    repo, _ = _make_repo(
        task_by_id={"task-001": t},
        open_ids=["task-001"],
        agent_subnet_ids=["public"],
    )

    results = await repo.find_open_tasks(requesting_agent_id="agent-b")
    assert results == []


@pytest.mark.asyncio
async def test_public_task_always_visible_even_without_requesting_agent():
    """Tasks with no subnet_id (public) bypass the visibility check."""
    t = _make_task(subnet_id=None)
    repo, fake_redis = _make_repo(
        task_by_id={"task-001": t},
        open_ids=["task-001"],
        agent_subnet_ids=[],
    )

    results = await repo.find_open_tasks(requesting_agent_id=None)
    assert [r.task_id for r in results] == ["task-001"]

    # No agent provided → we must not even HGET the (non-existent) agent
    fake_redis.hget.assert_not_called()


@pytest.mark.asyncio
async def test_visibility_never_touches_legacy_broken_keys():
    """Guard against anyone re-introducing the old `acn:subnets:all` or
    `acn:subnet:{sid}` paths. Both were empty/wrong and made private
    subnets invisible.
    """
    t = _make_task(subnet_id="subnet-sec")
    repo, fake_redis = _make_repo(
        task_by_id={"task-001": t},
        open_ids=["task-001"],
        agent_subnet_ids=["subnet-sec"],
    )

    await repo.find_open_tasks(requesting_agent_id="agent-a")

    for c in fake_redis.smembers.await_args_list:
        assert c.args[0] != "acn:subnets:all", (
            "visibility path must not read the never-written acn:subnets:all"
        )
    for c in fake_redis.hget.await_args_list:
        key = c.args[0]
        assert not (key.startswith("acn:subnet:") and not key.startswith("acn:subnets:")), (
            f"visibility path must not read the wrong-namespace key {key!r}"
        )


@pytest.mark.asyncio
async def test_corrupt_subnet_ids_does_not_crash_and_hides_private_tasks():
    """Defensive: if an agent's subnet_ids JSON is garbage, fall back to
    empty set (private tasks stay hidden) rather than 500-ing the list.
    """
    fake_redis = AsyncMock()
    fake_redis.zrevrange.return_value = [b"task-001"]

    async def fake_hget(key, field):
        return "{not json"  # garbage

    fake_redis.hget.side_effect = fake_hget
    t = _make_task(subnet_id="subnet-sec")
    repo = RedisTaskRepository(redis_client=fake_redis)
    repo.find_by_id = AsyncMock(return_value=t)  # type: ignore[method-assign]

    results = await repo.find_open_tasks(requesting_agent_id="agent-a")
    assert results == []
