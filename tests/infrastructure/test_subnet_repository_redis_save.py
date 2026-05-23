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

    ADR-0004 note: this test constructs a private subnet which now
    requires ``join_policy='approval'`` per the entity invariant. The
    explicit kwarg keeps the original test intent (bool serialisation)
    intact without re-litigating policy semantics here — those live
    in ``test_subnet_repository_redis_join_policy.py``.
    """
    redis = _make_redis_mock()
    repo = RedisSubnetRepository(redis)
    subnet = Subnet(
        slug="subnet-test-1",
        name="test",
        owner="agent-owner",
        is_private=True,
        join_policy="approval",
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
    """``save`` + ``_dict_to_subnet`` must preserve ``is_private`` value.

    ADR-0004 note: the ``is_private=True`` case is constructed with
    ``join_policy='approval'`` because the entity invariant rejects
    the legacy ``private + open`` combination. The test still pins
    the bool round-trip — that's its purpose; ``join_policy`` is
    incidental here and exhaustively tested in
    ``test_subnet_repository_redis_join_policy.py``.
    """
    redis = _make_redis_mock()
    repo = RedisSubnetRepository(redis)

    cases: list[tuple[bool, str]] = [(True, "approval"), (False, "open")]
    for is_private, join_policy in cases:
        subnet = Subnet(
            slug=f"subnet-rt-{is_private}",
            name="rt",
            owner="agent-owner",
            is_private=is_private,
            join_policy=join_policy,
        )
        await repo.save(subnet)
        _, kwargs = redis.hset.await_args
        loaded = repo._dict_to_subnet(kwargs["mapping"])
        assert loaded.is_private is is_private


@pytest.mark.asyncio
async def test_save_normalises_none_fields_to_empty_string() -> None:
    """Existing contract: ``None`` for nullable str fields must serialise as ''.

    Locks in current behaviour so the bool-fix patch does not regress the
    surrounding ``if ... is None`` normalisation block.
    """
    redis = _make_redis_mock()
    repo = RedisSubnetRepository(redis)
    subnet = Subnet(
        slug="subnet-none",
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


class TestDictToSubnetLegacyKeyCompat:
    """``_dict_to_subnet`` must hydrate Redis HASHes that pre-date the
    ``subnet_id`` → ``slug`` rename. Without legacy-key fall-through,
    the first read after a rolling deploy raises ``KeyError: 'slug'``
    on every existing subnet — a hard failure for Redis-only
    deployments because the entity-layer ``from_dict`` translation is
    bypassed by the manually-constructed dict path here.

    These tests run against the real :class:`RedisSubnetRepository`
    and bypass the network entirely (``_dict_to_subnet`` is a pure
    function on a HASH dict), so they reliably catch a regression
    that mock-based round-trip tests cannot.
    """

    def _make_repo(self) -> RedisSubnetRepository:
        return RedisSubnetRepository(redis_client=AsyncMock())

    def test_legacy_subnet_id_key_is_accepted(self):
        repo = self._make_repo()
        legacy_hash = {
            "subnet_id": "legacy-net",
            "name": "Legacy",
            "owner": "alice",
            "is_private": "False",
            "description": "",
            "created_at": "2024-01-01T00:00:00+00:00",
            "lifecycle": "persistent",
            "linked_task_id": "",
            "security_config": "{}",
            "metadata": "{}",
            "member_agent_ids": "[]",
            "harness_url": "",
            "harness_secret": "",
        }

        subnet = repo._dict_to_subnet(legacy_hash)

        assert subnet.slug == "legacy-net"

    def test_legacy_parent_subnet_id_key_is_accepted(self):
        repo = self._make_repo()
        legacy_hash = {
            "subnet_id": "child-net",
            "parent_subnet_id": "parent-net",
            "name": "Child",
            "owner": "alice",
            "is_private": "False",
            "description": "",
            "created_at": "2024-01-01T00:00:00+00:00",
            "lifecycle": "persistent",
            "linked_task_id": "",
            "security_config": "{}",
            "metadata": "{}",
            "member_agent_ids": "[]",
            "harness_url": "",
            "harness_secret": "",
        }

        subnet = repo._dict_to_subnet(legacy_hash)

        assert subnet.slug == "child-net"
        assert subnet.parent_slug == "parent-net"

    def test_new_keys_take_precedence_when_both_present(self):
        # Mid-rollout shape: a HASH that's been re-saved (has ``slug``)
        # but still carries the legacy ``subnet_id`` field from a
        # previous reader that round-tripped through an older repo.
        # The new key wins so the entity reflects the canonical name.
        repo = self._make_repo()
        mixed_hash = {
            "slug": "new-name",
            "subnet_id": "old-name",
            "parent_slug": "new-parent",
            "parent_subnet_id": "old-parent",
            "name": "Mixed",
            "owner": "alice",
            "is_private": "False",
            "description": "",
            "created_at": "2024-01-01T00:00:00+00:00",
            "lifecycle": "persistent",
            "linked_task_id": "",
            "security_config": "{}",
            "metadata": "{}",
            "member_agent_ids": "[]",
            "harness_url": "",
            "harness_secret": "",
        }

        subnet = repo._dict_to_subnet(mixed_hash)

        assert subnet.slug == "new-name"
        assert subnet.parent_slug == "new-parent"

    def test_completely_missing_slug_keys_raises_key_error(self):
        # If neither key is present the HASH is corrupt; failing fast
        # with a descriptive ``KeyError`` is preferable to letting an
        # empty string flow into the entity (which would then trip the
        # ``slug cannot be empty`` invariant with a less obvious trace).
        repo = self._make_repo()
        broken_hash = {
            "name": "Broken",
            "owner": "alice",
            "is_private": "False",
            "description": "",
            "created_at": "2024-01-01T00:00:00+00:00",
            "lifecycle": "persistent",
            "linked_task_id": "",
            "security_config": "{}",
            "metadata": "{}",
            "member_agent_ids": "[]",
            "harness_url": "",
            "harness_secret": "",
        }

        with pytest.raises(KeyError, match="slug.*subnet_id"):
            repo._dict_to_subnet(broken_hash)
