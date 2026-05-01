"""Tests for RedisAllowlistRepository (Phase 2 PR #2).

Pins the cache contract that ``AllowlistService.is_member``
depends on:

* Cache hit returns True without touching the PG loader.
* Cache hit (negative) returns False without touching the PG loader.
* Cache MISS triggers a read-through: PG loader called, SET
  rebuilt, TTL applied, EXISTS=1 afterwards.
* Empty allowlist materialises an EXISTS=1 SET (so subsequent
  ``is_member`` calls don't slam PG re-reading the empty list
  every time).
* TTL is applied on rebuild and on add.
* ``list_targets`` raises NotImplementedError (cache lacks
  created_at / reason — service routes listing to PG).

Uses the existing ``manifest_redis`` fakeredis fixture from
conftest.py — fakeredis supports SET / EXISTS / SISMEMBER / SADD /
SREM / EXPIRE / TTL with realistic semantics, which is what we
need to verify the read-through path end-to-end.
"""

from __future__ import annotations

import pytest

from acn.infrastructure.persistence.redis.allowlist_repository import (
    DEFAULT_CACHE_TTL_SECONDS,
    RedisAllowlistRepository,
    _allowlist_key,
)

OWNER = "owner-1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingLoader:
    """Stub PG loader that records its calls so tests can assert
    the read-through path actually fires.

    Returns a configured list of target ids per owner; falls back
    to ``[]`` (empty allowlist) for unknown owners.
    """

    def __init__(self, members: dict[str, list[str]] | None = None):
        self.members = members or {}
        self.calls: list[str] = []

    async def __call__(self, owner_id: str) -> list[str]:
        self.calls.append(owner_id)
        return list(self.members.get(owner_id, []))


# ---------------------------------------------------------------------------
# Cache hit paths
# ---------------------------------------------------------------------------


async def test_is_member_hit_positive_does_not_call_pg(manifest_redis):
    """Once the SET exists with the member in it, subsequent
    ``is_member`` calls must not touch the loader. This is the hot
    path under load."""
    loader = _RecordingLoader()
    repo = RedisAllowlistRepository(manifest_redis, pg_loader=loader)

    # Pre-populate the cache (the repo's own ``add`` does this).
    await repo.add(OWNER, "alice")

    result = await repo.is_member(OWNER, "alice")

    assert result is True
    assert loader.calls == []  # No PG fallback fired.


async def test_is_member_hit_negative_does_not_call_pg(manifest_redis):
    """Cache materialised + member NOT in it → False without
    falling back to PG. Critical for empty-allowlist owners
    otherwise every check would slam PG."""
    loader = _RecordingLoader()
    repo = RedisAllowlistRepository(manifest_redis, pg_loader=loader)

    # Materialise an empty SET via a no-op rebuild.
    await repo._rebuild(OWNER, [])  # type: ignore[attr-defined]
    assert loader.calls == []

    result = await repo.is_member(OWNER, "alice")

    assert result is False
    assert loader.calls == []


# ---------------------------------------------------------------------------
# Cache miss / read-through
# ---------------------------------------------------------------------------


async def test_is_member_miss_triggers_pg_loader_and_rebuilds_cache(manifest_redis):
    """Cache miss must:
    1. call ``pg_loader(owner_id)``;
    2. populate the SET with the returned members;
    3. apply the TTL;
    4. answer the membership question against the rebuilt SET.
    """
    loader = _RecordingLoader({OWNER: ["alice", "bob"]})
    repo = RedisAllowlistRepository(manifest_redis, pg_loader=loader)

    # Cache is cold.
    assert await manifest_redis.exists(_allowlist_key(OWNER)) == 0

    result = await repo.is_member(OWNER, "alice")

    assert result is True
    assert loader.calls == [OWNER]  # rebuild fired exactly once.
    # Cache materialised + populated.
    assert await manifest_redis.exists(_allowlist_key(OWNER)) == 1
    assert (
        await manifest_redis.sismember(_allowlist_key(OWNER), "alice")
    )
    # TTL applied within the configured window (allow ±2s drift in fakeredis).
    ttl = await manifest_redis.ttl(_allowlist_key(OWNER))
    assert 0 < ttl <= DEFAULT_CACHE_TTL_SECONDS


async def test_is_member_miss_then_hit_does_not_double_load(manifest_redis):
    """After a single rebuild, subsequent membership checks must
    serve from the cache. This is the steady-state property — one
    PG hit per 30s TTL window, not per request."""
    loader = _RecordingLoader({OWNER: ["alice"]})
    repo = RedisAllowlistRepository(manifest_redis, pg_loader=loader)

    await repo.is_member(OWNER, "alice")  # warms cache
    await repo.is_member(OWNER, "alice")  # hot path
    await repo.is_member(OWNER, "bob")  # hot, false

    assert loader.calls == [OWNER]  # only the first call loaded.


async def test_empty_allowlist_materialises_persistent_empty_set(manifest_redis):
    """Empty allowlist owners are the worst-case for a naive
    implementation: every ``is_member`` call would re-fire the
    rebuild because EXISTS would stay 0. The repo must materialise
    an EXISTS=1 empty SET so the steady-state is "one PG load per
    TTL window" even for empty lists."""
    loader = _RecordingLoader({OWNER: []})
    repo = RedisAllowlistRepository(manifest_redis, pg_loader=loader)

    # First call — rebuild fires, returns False (not member).
    assert await repo.is_member(OWNER, "alice") is False
    # Cache must now exist.
    assert await manifest_redis.exists(_allowlist_key(OWNER)) == 1
    # Second call — must NOT re-fire the loader.
    assert await repo.is_member(OWNER, "alice") is False
    assert loader.calls == [OWNER]  # still one call.


# ---------------------------------------------------------------------------
# add / remove
# ---------------------------------------------------------------------------


async def test_add_writes_set_and_extends_ttl(manifest_redis):
    """add must SADD the member and EXPIRE the key — the second
    part is critical so an active list doesn't expire mid-day."""
    loader = _RecordingLoader()
    repo = RedisAllowlistRepository(manifest_redis, pg_loader=loader)

    new = await repo.add(OWNER, "alice")

    assert new is True
    assert await manifest_redis.sismember(_allowlist_key(OWNER), "alice")
    ttl = await manifest_redis.ttl(_allowlist_key(OWNER))
    assert 0 < ttl <= DEFAULT_CACHE_TTL_SECONDS


async def test_add_returns_false_when_member_already_present(manifest_redis):
    """SADD returns 0 when the member already exists; the repo
    surfaces this as ``False`` so the service can use it as the
    canonical "newly created?" signal."""
    loader = _RecordingLoader()
    repo = RedisAllowlistRepository(manifest_redis, pg_loader=loader)

    await repo.add(OWNER, "alice")
    again = await repo.add(OWNER, "alice")

    assert again is False


async def test_remove_drops_member(manifest_redis):
    loader = _RecordingLoader()
    repo = RedisAllowlistRepository(manifest_redis, pg_loader=loader)

    await repo.add(OWNER, "alice")
    removed = await repo.remove(OWNER, "alice")

    assert removed is True
    assert not await manifest_redis.sismember(_allowlist_key(OWNER), "alice")


async def test_remove_returns_false_when_member_absent(manifest_redis):
    loader = _RecordingLoader()
    repo = RedisAllowlistRepository(manifest_redis, pg_loader=loader)

    removed = await repo.remove(OWNER, "alice")

    assert removed is False


# ---------------------------------------------------------------------------
# count_for_owner
# ---------------------------------------------------------------------------


async def test_count_uses_cache_when_materialised(manifest_redis):
    """SCARD on a materialised SET returns the correct count
    without falling back to PG. The empty-set dance leaves the
    sentinel removed, so SCARD is accurate."""
    loader = _RecordingLoader()
    repo = RedisAllowlistRepository(manifest_redis, pg_loader=loader)

    await repo.add(OWNER, "alice")
    await repo.add(OWNER, "bob")

    assert await repo.count_for_owner(OWNER) == 2
    assert loader.calls == []


async def test_count_falls_back_to_pg_on_miss(manifest_redis):
    """Cold cache: ``count_for_owner`` rebuilds via PG and returns
    the rebuilt count. Same shape as ``is_member`` miss path."""
    loader = _RecordingLoader({OWNER: ["a", "b", "c"]})
    repo = RedisAllowlistRepository(manifest_redis, pg_loader=loader)

    count = await repo.count_for_owner(OWNER)

    assert count == 3
    assert loader.calls == [OWNER]


# ---------------------------------------------------------------------------
# list_targets
# ---------------------------------------------------------------------------


async def test_list_targets_raises(manifest_redis):
    """The service routes listing to PG (cache lacks
    created_at / reason). Pin the NotImplementedError so a future
    refactor can't accidentally route through here."""
    loader = _RecordingLoader()
    repo = RedisAllowlistRepository(manifest_redis, pg_loader=loader)

    with pytest.raises(NotImplementedError):
        await repo.list_targets(OWNER)
