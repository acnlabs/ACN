"""Unit tests for ManifestService — Phase 2 PR #1.

Pins the manifest queue contract: atomic write across the three
keys (ZSET index + HASH detail + STRING content), TTL bounds,
summary truncation, cross-tenant isolation, and capacity trimming.

Backed by ``manifest_redis`` (fakeredis or real Redis via
``REDIS_URL``). AsyncMock can't reproduce ZRANGEBYSCORE / pipeline
semantics, so we use a real Redis-shaped client.

See docs/features/acn-communication-economic-model.md
"Phase 2 原型 PR 验收清单 — 原型 PR #1" for the assertion list this
file enforces.
"""

from __future__ import annotations

import re

import pytest

from acn.services.manifest_service import (
    DEFAULT_TTL_SECONDS,
    MAX_CONTENT_BYTES,
    MAX_SUMMARY_LEN,
    MAX_TTL_SECONDS,
    MIN_TTL_SECONDS,
    QUEUE_CAPACITY,
    ManifestService,
    _content_key,
    _detail_key,
    _zset_key,
)

OWNER = "agent-bob"
SENDER = "agent-alice"


@pytest.fixture
async def manifest_service(manifest_redis):
    return ManifestService(manifest_redis)


# ---------------------------------------------------------------------------
# Atomic write contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_returns_entry_with_uuid_mid(manifest_service):
    """``mid`` must be an unguessable hex string — Group A #4 requires
    UUIDs because the content endpoint is owner-scoped but a sequential
    ``mid`` would still let attackers probe other owners' queue depth."""
    entry = await manifest_service.write(
        owner_id=OWNER,
        sender_id=SENDER,
        summary="hello",
        content={"text": "hi"},
    )
    # 32 hex chars (UUID4 hex form), no dashes.
    assert re.fullmatch(r"[0-9a-f]{32}", entry.mid)
    assert entry.sender_id == SENDER
    assert entry.summary == "hello"
    assert entry.content_size == len('{"text":"hi"}')
    assert entry.ts_ms > 0


@pytest.mark.asyncio
async def test_write_persists_three_keys_with_owner_hash_tag(
    manifest_service, manifest_redis
):
    """Group A #4: ZSET + HASH + STRING must all share the
    ``{<owner_id>}`` hash tag so a Redis Cluster places them on the
    same slot. The keys themselves must follow the documented naming.
    """
    entry = await manifest_service.write(
        owner_id=OWNER,
        sender_id=SENDER,
        summary="hello",
        content={"text": "hi"},
    )

    expected_zset = f"acn:manifest:{{{OWNER}}}"
    expected_detail = f"acn:manifest:{{{OWNER}}}:{entry.mid}"
    expected_content = f"acn:content:{{{OWNER}}}:{entry.mid}"

    assert _zset_key(OWNER) == expected_zset
    assert _detail_key(OWNER, entry.mid) == expected_detail
    assert _content_key(OWNER, entry.mid) == expected_content

    # All three keys exist after the atomic write.
    assert await manifest_redis.zcard(expected_zset) == 1
    assert await manifest_redis.hget(expected_detail, "sender_id") == SENDER.encode()
    raw_content = await manifest_redis.get(expected_content)
    assert raw_content is not None
    assert b'"text":"hi"' in raw_content


@pytest.mark.asyncio
async def test_write_applies_default_ttl_to_all_three_keys(
    manifest_service, manifest_redis
):
    """TTL must be applied to all three keys atomically — a partial
    expire would leave the queue in an inconsistent state where
    ``read_since`` lists an entry whose content has already been
    evicted (or vice versa)."""
    entry = await manifest_service.write(
        owner_id=OWNER,
        sender_id=SENDER,
        summary="x",
        content={"k": "v"},
    )

    detail_ttl = await manifest_redis.ttl(_detail_key(OWNER, entry.mid))
    content_ttl = await manifest_redis.ttl(_content_key(OWNER, entry.mid))
    zset_ttl = await manifest_redis.ttl(_zset_key(OWNER))

    # All within ~5 s of the default (Redis returns whole seconds;
    # the small slack accounts for time elapsed between SET and TTL).
    assert DEFAULT_TTL_SECONDS - 5 <= detail_ttl <= DEFAULT_TTL_SECONDS
    assert DEFAULT_TTL_SECONDS - 5 <= content_ttl <= DEFAULT_TTL_SECONDS
    assert DEFAULT_TTL_SECONDS - 5 <= zset_ttl <= DEFAULT_TTL_SECONDS


@pytest.mark.asyncio
async def test_write_clamps_ttl_below_min(manifest_service, manifest_redis):
    entry = await manifest_service.write(
        owner_id=OWNER,
        sender_id=SENDER,
        summary="x",
        content={"k": "v"},
        ttl_seconds=10,  # below MIN_TTL_SECONDS
    )

    ttl = await manifest_redis.ttl(_detail_key(OWNER, entry.mid))
    assert MIN_TTL_SECONDS - 5 <= ttl <= MIN_TTL_SECONDS


@pytest.mark.asyncio
async def test_write_clamps_ttl_above_max(manifest_service, manifest_redis):
    entry = await manifest_service.write(
        owner_id=OWNER,
        sender_id=SENDER,
        summary="x",
        content={"k": "v"},
        ttl_seconds=MAX_TTL_SECONDS * 100,  # outrageously large
    )

    ttl = await manifest_redis.ttl(_detail_key(OWNER, entry.mid))
    assert MAX_TTL_SECONDS - 5 <= ttl <= MAX_TTL_SECONDS


@pytest.mark.asyncio
async def test_write_truncates_summary_with_ellipsis(manifest_service):
    """Summary > MAX_SUMMARY_LEN gets truncated with a single ``…``
    marker so consumers can detect the clip."""
    overlong = "A" * (MAX_SUMMARY_LEN + 50)
    entry = await manifest_service.write(
        owner_id=OWNER,
        sender_id=SENDER,
        summary=overlong,
        content={"k": "v"},
    )
    assert len(entry.summary) == MAX_SUMMARY_LEN
    assert entry.summary.endswith("…")


@pytest.mark.asyncio
async def test_write_rejects_oversize_content(manifest_service):
    """Content exceeding MAX_CONTENT_BYTES raises ``ValueError`` so
    the route layer can surface 422 (route layer maps ValueError →
    422 by convention)."""
    huge_blob = "x" * (MAX_CONTENT_BYTES + 1)
    with pytest.raises(ValueError, match="exceeds"):
        await manifest_service.write(
            owner_id=OWNER,
            sender_id=SENDER,
            summary="x",
            content={"blob": huge_blob},
        )


# ---------------------------------------------------------------------------
# Capacity trimming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_trims_queue_at_capacity(manifest_service, manifest_redis):
    """Per-owner ZSET retains at most ``QUEUE_CAPACITY`` entries —
    older entries must be evicted from the index. We don't assert
    the orphaned content/detail keys are deleted (they are intentionally
    left to TTL — see the comment in ManifestService.write)."""
    # Write QUEUE_CAPACITY + 5 entries, all to the same owner.
    for i in range(QUEUE_CAPACITY + 5):
        await manifest_service.write(
            owner_id=OWNER,
            sender_id=SENDER,
            summary=f"msg-{i}",
            content={"i": i},
        )

    zcard = await manifest_redis.zcard(_zset_key(OWNER))
    assert zcard == QUEUE_CAPACITY


# ---------------------------------------------------------------------------
# read_since
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_since_returns_chronological_entries(manifest_service):
    """``read_since`` must return entries in oldest→newest order so
    paginating clients can advance their ``since_ms`` cursor without
    re-receiving prior pages.

    We sleep briefly between writes so ts_ms is monotonically
    distinct — without that, two writes within the same millisecond
    share a ZSET score and ZRANGEBYSCORE falls back to lex ordering
    of the (random UUID) mids, which would make this assertion
    flaky. Real production traffic naturally spaces writes by more
    than 1 ms; this is a test-only synchronization concern.
    """
    import asyncio

    written = []
    for i in range(3):
        e = await manifest_service.write(
            owner_id=OWNER,
            sender_id=SENDER,
            summary=f"msg-{i}",
            content={"i": i},
        )
        written.append(e)
        await asyncio.sleep(0.002)

    out = await manifest_service.read_since(OWNER)
    assert [e.mid for e in out] == [e.mid for e in written]
    assert [e.summary for e in out] == ["msg-0", "msg-1", "msg-2"]


@pytest.mark.asyncio
async def test_read_since_filters_by_cursor(manifest_service):
    """``since_ms`` is an inclusive lower bound on entry ts."""
    import asyncio

    e1 = await manifest_service.write(
        owner_id=OWNER, sender_id=SENDER, summary="a", content={}
    )
    await asyncio.sleep(0.002)
    e2 = await manifest_service.write(
        owner_id=OWNER, sender_id=SENDER, summary="b", content={}
    )

    # Cursor at e2.ts must return only e2 (inclusive lower bound).
    out = await manifest_service.read_since(OWNER, since_ms=e2.ts_ms)
    assert [e.mid for e in out] == [e2.mid]

    # Cursor below e1 returns both.
    out = await manifest_service.read_since(OWNER, since_ms=e1.ts_ms - 1000)
    assert [e.mid for e in out] == [e1.mid, e2.mid]


@pytest.mark.asyncio
async def test_read_since_respects_limit(manifest_service):
    for i in range(10):
        await manifest_service.write(
            owner_id=OWNER, sender_id=SENDER, summary=f"m{i}", content={}
        )

    out = await manifest_service.read_since(OWNER, limit=3)
    assert len(out) == 3


@pytest.mark.asyncio
async def test_read_since_empty_queue(manifest_service):
    assert await manifest_service.read_since(OWNER) == []


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_content_cross_tenant_returns_none(manifest_service):
    """``mid`` is owner-scoped: even if Eve guesses Alice's ``mid``,
    fetching as Eve must return ``None`` (route maps to 404)."""
    entry = await manifest_service.write(
        owner_id="alice",
        sender_id="charlie",
        summary="private",
        content={"secret": True},
    )

    # Same mid, different owner — must look like "not found".
    other = await manifest_service.fetch_content(owner_id="eve", mid=entry.mid)
    assert other is None

    # Owner can still read it.
    own = await manifest_service.fetch_content(owner_id="alice", mid=entry.mid)
    assert own == {"secret": True}


@pytest.mark.asyncio
async def test_fetch_content_unknown_mid_returns_none(manifest_service):
    assert (
        await manifest_service.fetch_content(owner_id=OWNER, mid="00" * 16) is None
    )


@pytest.mark.asyncio
async def test_fetch_content_is_repeatable(manifest_service):
    """Group A #4 / P1-9: content fetch is repeatable, no read-once
    flag, no fee debit. Two consecutive reads must yield the same
    payload."""
    entry = await manifest_service.write(
        owner_id=OWNER, sender_id=SENDER, summary="x", content={"v": 1}
    )

    a = await manifest_service.fetch_content(OWNER, entry.mid)
    b = await manifest_service.fetch_content(OWNER, entry.mid)
    assert a == b == {"v": 1}


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_all_three_keys(manifest_service, manifest_redis):
    entry = await manifest_service.write(
        owner_id=OWNER, sender_id=SENDER, summary="x", content={"k": "v"}
    )

    deleted = await manifest_service.delete(OWNER, entry.mid)
    assert deleted is True

    assert await manifest_redis.zcard(_zset_key(OWNER)) == 0
    assert await manifest_redis.exists(_detail_key(OWNER, entry.mid)) == 0
    assert await manifest_redis.exists(_content_key(OWNER, entry.mid)) == 0


@pytest.mark.asyncio
async def test_delete_unknown_mid_returns_false(manifest_service):
    assert await manifest_service.delete(OWNER, "00" * 16) is False
