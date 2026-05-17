"""Redis subnet repository save / round-trip regression tests.

These pin down two storage contracts that future refactors of
``RedisSubnetRepository`` cannot silently break:

  1. ``save()`` must never pass a Python ``bool`` to ``redis.hset(mapping=...)``.
     redis-py rejects raw bools with
     ``DataError: Invalid input of type: 'bool'``, which previously caused
     ``POST /api/v1/subnets`` to 500 in Redis-fallback mode.
  2. The string form must round-trip through ``_dict_to_subnet`` so that
     ``is_private`` is preserved across a save/load cycle.

Mirrors the AsyncMock style used in ``test_follow_repository``.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.entities import Subnet
from acn.infrastructure.persistence.redis.subnet_repository import (
    RedisSubnetRepository,
)


def _make_redis_mock() -> AsyncMock:
    """Build a redis-py async client mock that:
    - records calls to ``hset``;
    - exposes an awaitable ``pipeline()`` async context manager that
      itself records ``sadd`` / ``srem`` / ``execute`` calls.

    ``hgetall`` is wired to default to ``{}`` so ``save()`` 's
    "read old row first" lookup (ADR-0003 incremental index
    maintenance) cleanly returns "no existing subnet" without
    needing every test to override it.
    """
    redis = AsyncMock()
    redis.hgetall.return_value = {}

    class _PipeProxy:
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

        async def execute(self):
            self.calls.append(("execute", (), {}))
            return []

    pipe = _PipeProxy()
    # pipeline() is synchronous on redis.asyncio (returns the context manager)
    redis.pipeline = MagicMock(return_value=pipe)
    redis._pipe = pipe  # expose for assertions
    return redis


@pytest.mark.asyncio
async def test_save_serialises_is_private_as_string_not_bool() -> None:
    """Regression: bool ``is_private`` must be stringified before HSET.

    Previously the field was passed through as Python ``True``/``False``,
    which redis-py refuses (``DataError: Invalid input of type 'bool'``),
    crashing every subnet create on Redis-fallback deployments.
    """
    redis = _make_redis_mock()
    repo = RedisSubnetRepository(redis)
    subnet = Subnet(
        subnet_id="subnet-test-1",
        name="test",
        owner="agent-owner",
        is_private=True,
    )

    await repo.save(subnet)

    redis.hset.assert_awaited_once()
    _, kwargs = redis.hset.await_args
    mapping = kwargs["mapping"]
    # No bare bools in the HSET payload.
    assert not any(isinstance(v, bool) for v in mapping.values()), (
        f"bool leaked into Redis HSET payload: {mapping!r}"
    )
    # Round-trip format expected by `_dict_to_subnet`.
    assert mapping["is_private"] == "True"


@pytest.mark.asyncio
async def test_save_round_trip_preserves_is_private() -> None:
    """``save`` + ``_dict_to_subnet`` must preserve ``is_private`` value."""
    redis = _make_redis_mock()
    repo = RedisSubnetRepository(redis)

    for value in (True, False):
        subnet = Subnet(
            subnet_id=f"subnet-rt-{value}",
            name="rt",
            owner="agent-owner",
            is_private=value,
        )
        await repo.save(subnet)
        _, kwargs = redis.hset.await_args
        loaded = repo._dict_to_subnet(kwargs["mapping"])
        assert loaded.is_private is value


@pytest.mark.asyncio
async def test_save_normalises_none_fields_to_empty_string() -> None:
    """Existing contract: ``None`` for nullable str fields must serialise as ''.

    Locks in current behaviour so the bool-fix patch does not regress the
    surrounding ``if ... is None`` normalisation block.
    """
    redis = _make_redis_mock()
    repo = RedisSubnetRepository(redis)
    subnet = Subnet(
        subnet_id="subnet-none",
        name="nones",
        owner="agent-owner",
        description=None,
        harness_url=None,
        harness_secret=None,
    )

    await repo.save(subnet)

    _, kwargs = redis.hset.await_args
    mapping = kwargs["mapping"]
    assert mapping["description"] == ""
    assert mapping["harness_url"] == ""
    assert mapping["harness_secret"] == ""
