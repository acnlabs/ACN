"""Redis subnet repository — ADR-0003 nesting index regressions.

Pins the contract that ``save`` / ``delete`` maintain the two
secondary indexes used by ``find_by_parent`` and
``find_by_linked_task``:

- ``acn:subnets:children:{parent_id}`` SET
- ``acn:subnets:by_linked_task:{task_id}`` SET

Index maintenance happens inside a single ``pipeline`` per write
so a process crash between the main HSET and the index update is
the only failure mode that can desync them — same weak-atomicity
profile ACN already accepts for the owner index.

Uses the same AsyncMock pipeline-proxy pattern as
``test_subnet_repository_redis_save.py``.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.entities import Subnet
from acn.infrastructure.persistence.redis.subnet_repository import (
    RedisSubnetRepository,
)


class _PipeProxy:
    """Pipeline proxy that records ``sadd`` / ``srem`` / ``execute``
    invocations as ``(verb, args, kwargs)`` tuples."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def sadd(self, *args, **kwargs):
        self.calls.append(("sadd", args, kwargs))

    def srem(self, *args, **kwargs):
        self.calls.append(("srem", args, kwargs))

    def delete(self, *args, **kwargs):
        self.calls.append(("delete", args, kwargs))

    async def execute(self):
        self.calls.append(("execute", (), {}))
        return []


def _make_redis_mock() -> AsyncMock:
    """AsyncMock client wired so ``hgetall`` defaults to ``{}`` (new
    subnet path) and ``pipeline()`` returns a fresh ``_PipeProxy``
    each call."""
    redis = AsyncMock()
    redis.hgetall.return_value = {}

    pipes: list[_PipeProxy] = []

    def _new_pipe(*args, **kwargs):
        p = _PipeProxy()
        pipes.append(p)
        return p

    redis.pipeline = MagicMock(side_effect=_new_pipe)
    redis._pipes = pipes  # expose for assertions
    return redis


def _pipe_verbs(pipes: list[_PipeProxy], verb: str) -> list[tuple]:
    """Flatten all ``verb`` calls across recorded pipeline contexts."""
    return [args for pipe in pipes for v, args, _ in pipe.calls if v == verb]


# ---------------------------------------------------------------------------
# save() — index population on insert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_child_subnet_adds_to_children_index() -> None:
    redis = _make_redis_mock()
    repo = RedisSubnetRepository(redis)
    child = Subnet(
        slug="subnet-child",
        name="child",
        owner="agent-owner",
        parent_slug="subnet-parent",
    )

    await repo.save(child)

    sadd_calls = _pipe_verbs(redis._pipes, "sadd")
    assert (
        "acn:subnets:children:subnet-parent",
        "subnet-child",
    ) in sadd_calls, (
        f"children index SADD missing; recorded sadd calls: {sadd_calls!r}"
    )


@pytest.mark.asyncio
async def test_save_task_scoped_subnet_adds_to_linked_task_index() -> None:
    redis = _make_redis_mock()
    repo = RedisSubnetRepository(redis)
    scoped = Subnet(
        slug="subnet-scoped",
        name="scoped",
        owner="agent-owner",
        parent_slug="subnet-parent",
        lifecycle="task_scoped",
        linked_task_id="task-xyz",
    )

    await repo.save(scoped)

    sadd_calls = _pipe_verbs(redis._pipes, "sadd")
    assert (
        "acn:subnets:by_linked_task:task-xyz",
        "subnet-scoped",
    ) in sadd_calls, (
        f"by_linked_task index SADD missing; recorded sadd calls: {sadd_calls!r}"
    )


@pytest.mark.asyncio
async def test_save_top_level_subnet_does_not_touch_nesting_indexes() -> None:
    """Top-level persistent subnets must not pollute the nesting
    indexes — keeps the partial-index sizes bounded by the actual
    nesting cardinality."""
    redis = _make_redis_mock()
    repo = RedisSubnetRepository(redis)
    flat = Subnet(
        slug="subnet-flat",
        name="flat",
        owner="agent-owner",
    )

    await repo.save(flat)

    sadd_calls = _pipe_verbs(redis._pipes, "sadd")
    for key, _ in sadd_calls:
        assert not key.startswith("acn:subnets:children:"), (
            f"unexpected children-index SADD on top-level subnet: {key}"
        )
        assert not key.startswith("acn:subnets:by_linked_task:"), (
            f"unexpected linked-task-index SADD on top-level subnet: {key}"
        )


# ---------------------------------------------------------------------------
# delete() — index eviction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_child_subnet_removes_from_children_index() -> None:
    redis = _make_redis_mock()
    repo = RedisSubnetRepository(redis)

    # Wire ``find_by_id`` (via hgetall) to return a stored child row
    # so ``delete`` actually proceeds past its existence guard.
    redis.hgetall.return_value = {
        "slug": "subnet-child",
        "name": "child",
        "owner": "agent-owner",
        "is_private": "False",
        "security_config": "{}",
        "member_agent_ids": "[]",
        "metadata": "{}",
        "description": "",
        "harness_url": "",
        "harness_secret": "",
        "parent_slug": "subnet-parent",
        "lifecycle": "persistent",
        "linked_task_id": "",
    }

    deleted = await repo.delete("subnet-child")

    assert deleted is True
    srem_calls = _pipe_verbs(redis._pipes, "srem")
    assert (
        "acn:subnets:children:subnet-parent",
        "subnet-child",
    ) in srem_calls, (
        f"children index SREM missing; recorded srem calls: {srem_calls!r}"
    )


@pytest.mark.asyncio
async def test_delete_task_scoped_subnet_removes_from_linked_task_index() -> None:
    redis = _make_redis_mock()
    repo = RedisSubnetRepository(redis)
    redis.hgetall.return_value = {
        "slug": "subnet-scoped",
        "name": "scoped",
        "owner": "agent-owner",
        "is_private": "False",
        "security_config": "{}",
        "member_agent_ids": "[]",
        "metadata": "{}",
        "description": "",
        "harness_url": "",
        "harness_secret": "",
        "parent_slug": "subnet-parent",
        "lifecycle": "task_scoped",
        "linked_task_id": "task-xyz",
    }

    deleted = await repo.delete("subnet-scoped")

    assert deleted is True
    srem_calls = _pipe_verbs(redis._pipes, "srem")
    assert (
        "acn:subnets:by_linked_task:task-xyz",
        "subnet-scoped",
    ) in srem_calls, (
        f"by_linked_task index SREM missing; recorded srem calls: {srem_calls!r}"
    )


# ---------------------------------------------------------------------------
# find_by_parent / find_by_linked_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_by_parent_reads_children_index_and_hydrates() -> None:
    redis = _make_redis_mock()
    repo = RedisSubnetRepository(redis)
    redis.smembers.return_value = {"subnet-a", "subnet-b"}

    # Each ``find_by_id`` call inside the loop hits ``hgetall`` —
    # return a minimal row that satisfies ``_dict_to_subnet``. The
    # same payload comes back for both subnet IDs; ``find_by_parent``
    # only cares that hydration happens, not which row is which.
    redis.hgetall.return_value = {
        "slug": "subnet-a",
        "name": "a",
        "owner": "agent-owner",
        "is_private": "False",
        "security_config": "{}",
        "member_agent_ids": "[]",
        "metadata": "{}",
        "description": "",
        "harness_url": "",
        "harness_secret": "",
        "parent_slug": "subnet-parent",
        "lifecycle": "persistent",
        "linked_task_id": "",
    }

    children = await repo.find_by_parent("subnet-parent")

    redis.smembers.assert_awaited_with("acn:subnets:children:subnet-parent")
    assert len(children) == 2


@pytest.mark.asyncio
async def test_find_by_linked_task_reads_index_and_hydrates() -> None:
    redis = _make_redis_mock()
    repo = RedisSubnetRepository(redis)
    redis.smembers.return_value = {"subnet-scoped"}
    redis.hgetall.return_value = {
        "slug": "subnet-scoped",
        "name": "scoped",
        "owner": "agent-owner",
        "is_private": "False",
        "security_config": "{}",
        "member_agent_ids": "[]",
        "metadata": "{}",
        "description": "",
        "harness_url": "",
        "harness_secret": "",
        "parent_slug": "subnet-parent",
        "lifecycle": "task_scoped",
        "linked_task_id": "task-xyz",
    }

    subnets = await repo.find_by_linked_task("task-xyz")

    redis.smembers.assert_awaited_with("acn:subnets:by_linked_task:task-xyz")
    assert len(subnets) == 1
    assert subnets[0].linked_task_id == "task-xyz"


@pytest.mark.asyncio
async def test_find_by_parent_returns_empty_when_no_children() -> None:
    redis = _make_redis_mock()
    repo = RedisSubnetRepository(redis)
    redis.smembers.return_value = set()

    children = await repo.find_by_parent("subnet-childless")

    assert children == []


# ---------------------------------------------------------------------------
# round-trip — to_dict / _dict_to_subnet preserve nesting fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_round_trip_preserves_nesting_fields() -> None:
    redis = _make_redis_mock()
    repo = RedisSubnetRepository(redis)
    scoped = Subnet(
        slug="subnet-rt",
        name="rt",
        owner="agent-owner",
        parent_slug="subnet-parent",
        lifecycle="task_scoped",
        linked_task_id="task-rt",
    )

    await repo.save(scoped)

    _, kwargs = redis.hset.await_args
    mapping = kwargs["mapping"]
    loaded = repo._dict_to_subnet(mapping)

    assert loaded.parent_slug == "subnet-parent"
    assert loaded.lifecycle == "task_scoped"
    assert loaded.linked_task_id == "task-rt"


def test_dict_to_subnet_tolerates_legacy_rows_without_nesting_keys() -> None:
    """A Redis row written before ADR-0003 contains no
    ``parent_slug`` / ``lifecycle`` / ``linked_task_id`` keys.
    ``_dict_to_subnet`` must fall through to entity defaults rather
    than ``KeyError`` — otherwise upgrading the binary against an
    un-migrated Redis would crash on first read."""
    redis = _make_redis_mock()
    repo = RedisSubnetRepository(redis)
    legacy = {
        "slug": "subnet-legacy",
        "name": "legacy",
        "owner": "agent-owner",
        "is_private": "False",
        "security_config": "{}",
        "member_agent_ids": "[]",
        "metadata": "{}",
        "description": "",
        "harness_url": "",
        "harness_secret": "",
    }

    subnet = repo._dict_to_subnet(legacy)

    assert subnet.parent_slug is None
    assert subnet.lifecycle == "persistent"
    assert subnet.linked_task_id is None
