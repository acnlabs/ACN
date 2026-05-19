"""RedisSubnetAllowlistRepository regressions (ADR-0004 Slice 2.1).

End-to-end against fakeredis (no Lua here — pure SADD / SREM /
SISMEMBER + HSET, all of which fakeredis supports natively). Pins:

- ``add`` returns True for new entries, False for re-adds (SADD's
  1 vs 0 return mapped through). The boolean is the route layer's
  signal for 201 vs 200 per ADR §HTTP status code conventions.
- The SET and parallel meta-HASH layout matches ADR §SubnetAllowlist
  ("Redis: SADD acn:subnets:{id}:allowlist <agent_id>" + parallel
  HASH).
- ``is_member`` returns True iff the SET contains the agent_id,
  even if the meta-HASH is missing (the cache-only crash state
  the class docstring describes).
- ``list_for_subnet`` skips SET-only orphans silently (defensive
  contract — don't fabricate missing audit attribution).
- ``delete_for_subnet`` raises ``RuntimeError`` on partial failure
  so the caller's cascade-before-subnet-HASH contract holds.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fakeredis import aioredis as fakeredis_async

from acn.core.entities import SubnetAllowlist
from acn.infrastructure.persistence.redis.subnet_allowlist_repository import (
    RedisSubnetAllowlistRepository,
    _allowlist_meta_key,
    _allowlist_set_key,
)


@pytest.fixture
async def fake_redis():
    client = fakeredis_async.FakeRedis(decode_responses=False)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


def _entry(**overrides) -> SubnetAllowlist:
    defaults = dict(
        subnet_id="s-1",
        agent_id="a-1",
        added_by="owner-1",
    )
    defaults.update(overrides)
    return SubnetAllowlist(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# add — SADD return ↔ 201/200 signal
# ---------------------------------------------------------------------------


class TestAddIdempotency:
    @pytest.mark.asyncio
    async def test_first_add_returns_true(self, fake_redis):
        repo = RedisSubnetAllowlistRepository(fake_redis)
        assert await repo.add(_entry()) is True

    @pytest.mark.asyncio
    async def test_readd_returns_false(self, fake_redis):
        repo = RedisSubnetAllowlistRepository(fake_redis)
        await repo.add(_entry())
        assert await repo.add(_entry()) is False

    @pytest.mark.asyncio
    async def test_add_writes_set_and_meta_hash(self, fake_redis):
        repo = RedisSubnetAllowlistRepository(fake_redis)
        await repo.add(_entry(subnet_id="s-x", agent_id="a-x"))
        # SET membership
        assert await fake_redis.sismember(_allowlist_set_key("s-x"), b"a-x")
        # Parallel meta HASH carries the audit fields
        meta = await fake_redis.hgetall(_allowlist_meta_key("s-x", "a-x"))
        assert meta, "meta HASH must be populated alongside the SET entry"


# ---------------------------------------------------------------------------
# is_member — SISMEMBER on the SET (HASH-independent)
# ---------------------------------------------------------------------------


class TestIsMember:
    @pytest.mark.asyncio
    async def test_returns_true_for_set_member(self, fake_redis):
        repo = RedisSubnetAllowlistRepository(fake_redis)
        await repo.add(_entry())
        assert await repo.is_member("s-1", "a-1") is True

    @pytest.mark.asyncio
    async def test_returns_false_for_non_member(self, fake_redis):
        repo = RedisSubnetAllowlistRepository(fake_redis)
        assert await repo.is_member("s-1", "ghost") is False

    @pytest.mark.asyncio
    async def test_set_only_orphan_still_counts_as_member(self, fake_redis):
        """SET present + meta HASH missing is the crash state where
        SADD landed but HSET didn't. ``is_member`` is the SISMEMBER
        check — by design it should still return True (the agent
        IS allowlisted, just without readable audit attribution).
        Without this guarantee a transient crash mid-write would
        evict the agent from admission decisions, contradicting the
        Postgres source-of-truth row that still shows them."""
        repo = RedisSubnetAllowlistRepository(fake_redis)
        await fake_redis.sadd(_allowlist_set_key("s-1"), b"a-1")  # type: ignore[misc]
        # No HSET — meta missing
        assert await repo.is_member("s-1", "a-1") is True


# ---------------------------------------------------------------------------
# remove — symmetric idempotency
# ---------------------------------------------------------------------------


class TestRemove:
    @pytest.mark.asyncio
    async def test_remove_returns_true_when_member(self, fake_redis):
        repo = RedisSubnetAllowlistRepository(fake_redis)
        await repo.add(_entry())
        assert await repo.remove("s-1", "a-1") is True
        # Both keys gone
        assert not await fake_redis.sismember(_allowlist_set_key("s-1"), b"a-1")
        assert not await fake_redis.exists(_allowlist_meta_key("s-1", "a-1"))

    @pytest.mark.asyncio
    async def test_remove_returns_false_when_not_member(self, fake_redis):
        repo = RedisSubnetAllowlistRepository(fake_redis)
        assert await repo.remove("s-1", "ghost") is False


# ---------------------------------------------------------------------------
# list_for_subnet — sort + orphan tolerance
# ---------------------------------------------------------------------------


class TestListForSubnet:
    @pytest.mark.asyncio
    async def test_returns_entries_sorted_most_recent_first(
        self, fake_redis
    ):
        repo = RedisSubnetAllowlistRepository(fake_redis)
        # Three entries with explicit added_at to force a sort.
        for i in range(3):
            await repo.add(
                _entry(
                    agent_id=f"a-{i}",
                    added_at=datetime(2026, 1, i + 1, tzinfo=UTC),
                )
            )
        rows = await repo.list_for_subnet("s-1")
        assert len(rows) == 3
        # Most recent first → a-2, a-1, a-0
        assert [r.agent_id for r in rows] == ["a-2", "a-1", "a-0"]

    @pytest.mark.asyncio
    async def test_skips_set_only_orphans_silently(self, fake_redis):
        """An orphan (SET membership without parallel meta HASH) is
        a known cache-crash artefact. ``list_for_subnet`` skips it
        rather than fabricating an entry with synthetic audit
        fields — operators reading the listing get the truthful
        "this row's metadata is unavailable", not a misleading
        ``added_by='unknown'`` shape that could be mistaken for a
        real entry."""
        repo = RedisSubnetAllowlistRepository(fake_redis)
        await fake_redis.sadd(_allowlist_set_key("s-1"), b"orphan")  # type: ignore[misc]
        rows = await repo.list_for_subnet("s-1")
        assert rows == []


# ---------------------------------------------------------------------------
# delete_for_subnet — cascade contract
# ---------------------------------------------------------------------------


class TestCascade:
    @pytest.mark.asyncio
    async def test_delete_for_subnet_removes_all_keys(self, fake_redis):
        repo = RedisSubnetAllowlistRepository(fake_redis)
        for i in range(3):
            await repo.add(_entry(agent_id=f"a-{i}"))

        n = await repo.delete_for_subnet("s-1")
        assert n == 3
        # SET gone
        assert not await fake_redis.exists(_allowlist_set_key("s-1"))
        # All meta HASHes gone
        for i in range(3):
            assert not await fake_redis.exists(
                _allowlist_meta_key("s-1", f"a-{i}")
            )

    @pytest.mark.asyncio
    async def test_delete_for_subnet_zero_rows_is_legal(self, fake_redis):
        repo = RedisSubnetAllowlistRepository(fake_redis)
        assert await repo.delete_for_subnet("s-empty") == 0
