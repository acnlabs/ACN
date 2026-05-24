"""Redis subnet repository ``join_policy`` round-trip regressions (ADR-0004 Phase 1).

Mirrors ``test_subnet_repository_redis_save.py`` and
``test_subnet_repository_redis_nesting.py`` for the new
``join_policy`` field. The contracts pinned here:

1. ``save()`` writes ``join_policy`` into the HSET payload as a
   plain string (never a Python ``bool`` or ``None`` — both would
   crash redis-py or round-trip wrong).
2. ``_dict_to_subnet`` reads back the field unchanged.
3. **Legacy compatibility** — a HASH that predates ADR-0004 (no
   ``join_policy`` key) reconstructs as:
   - ``"open"`` when ``is_private == "False"`` (or absent), matching
     the pre-ADR-0004 default behaviour.
   - ``"approval"`` when ``is_private == "True"``, matching the
     Alembic backfill's ``UPDATE ... WHERE is_private = true``
     semantic so reads remain safe before
     ``scripts/backfill_subnet_join_policy.py`` runs.

Together these guarantee that no Redis read can produce a
``private + open`` entity that would fail ``__post_init__``, even
mid-migration.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.entities import Subnet
from acn.infrastructure.persistence.redis.subnet_repository import (
    RedisSubnetRepository,
)


def _make_redis_mock() -> AsyncMock:
    """Same pipeline-recording mock as
    ``test_subnet_repository_redis_save.py``. Documented there; not
    repeated here."""
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
    redis.pipeline = MagicMock(return_value=pipe)
    redis._pipe = pipe
    return redis


@pytest.mark.asyncio
async def test_save_writes_join_policy_as_string() -> None:
    """``save`` must put ``join_policy`` into the HSET mapping as a
    plain string. No bools, no None — both would either crash
    redis-py or round-trip wrong."""
    redis = _make_redis_mock()
    repo = RedisSubnetRepository(redis)
    subnet = Subnet(
        slug="subnet-jp-1",
        name="JP",
        owner="agent-owner",
        is_private=True,
        join_policy="approval",
    )

    await repo.save(subnet)

    _, kwargs = redis.hset.await_args
    mapping = kwargs["mapping"]
    assert mapping["join_policy"] == "approval"
    assert isinstance(mapping["join_policy"], str)


@pytest.mark.asyncio
async def test_save_round_trip_preserves_join_policy() -> None:
    """``save`` + ``_dict_to_subnet`` must preserve ``join_policy``
    for every legal value."""
    redis = _make_redis_mock()
    repo = RedisSubnetRepository(redis)

    # ``(is_private, join_policy)`` pairs that the entity accepts.
    # ``private + open`` is intentionally absent — the entity rejects
    # it, so it can never reach the repo legitimately.
    legal_pairs = [
        (False, "open"),
        (False, "approval"),
        (True, "approval"),
    ]
    for is_private, join_policy in legal_pairs:
        subnet = Subnet(
            slug=f"subnet-rt-{is_private}-{join_policy}",
            name="rt",
            owner="agent-owner",
            is_private=is_private,
            join_policy=join_policy,
        )
        await repo.save(subnet)
        _, kwargs = redis.hset.await_args
        loaded = repo._dict_to_subnet(kwargs["mapping"])
        assert loaded.is_private is is_private
        assert loaded.join_policy == join_policy


def test_dict_to_subnet_legacy_public_row_defaults_to_open() -> None:
    """A HASH that predates ADR-0004 (no ``join_policy`` key) on a
    public subnet must reconstruct as ``join_policy="open"`` — that
    was the pre-ADR-0004 behaviour for public subnets and the entity
    invariant accepts it."""
    repo = RedisSubnetRepository(AsyncMock())
    legacy_public = {
        "slug": "subnet-legacy-pub",
        "name": "Legacy",
        "owner": "agent-1",
        "is_private": "False",
        "security_config": "{}",
        "member_agent_ids": "[]",
        "metadata": "{}",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    subnet = repo._dict_to_subnet(legacy_public)
    assert subnet.is_private is False
    assert subnet.join_policy == "open"


def test_dict_to_subnet_legacy_private_row_auto_upgrades_to_approval() -> None:
    """**Critical ADR-0004 invariant** — a HASH that predates the
    field on a ``is_private=True`` subnet must reconstruct as
    ``join_policy="approval"``, mirroring the Alembic backfill
    semantic. Without this auto-upgrade the entity invariant would
    reject every read of a legacy private subnet during the
    migration window."""
    repo = RedisSubnetRepository(AsyncMock())
    legacy_private = {
        "slug": "subnet-legacy-priv",
        "name": "LegacyPriv",
        "owner": "agent-1",
        "is_private": "True",
        "security_config": "{}",
        "member_agent_ids": "[]",
        "metadata": "{}",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    subnet = repo._dict_to_subnet(legacy_private)
    assert subnet.is_private is True
    # Auto-upgraded by the repo, matching the Alembic backfill.
    assert subnet.join_policy == "approval"


def test_dict_to_subnet_empty_join_policy_treated_as_missing() -> None:
    """Defensive: a HASH where ``join_policy`` somehow ended up as
    the empty string (corrupted save, manual DBA edit) is treated
    as 'missing' and runs through the auto-upgrade path. Same rule:
    private => approval, otherwise open."""
    repo = RedisSubnetRepository(AsyncMock())
    private_empty = {
        "slug": "subnet-empty-priv",
        "name": "EmptyPriv",
        "owner": "agent-1",
        "is_private": "True",
        "join_policy": "",
        "security_config": "{}",
        "member_agent_ids": "[]",
        "metadata": "{}",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    subnet = repo._dict_to_subnet(private_empty)
    assert subnet.join_policy == "approval"

    public_empty = {**private_empty}
    public_empty["slug"] = "subnet-empty-pub"
    public_empty["is_private"] = "False"
    subnet_pub = repo._dict_to_subnet(public_empty)
    assert subnet_pub.join_policy == "open"
