"""Tests for the manifest TTL refund worker (Phase 3 Module B follow-up).

Covers:
* Happy path: expired unacked entry with escrow_id → refund_v2 called,
  ``refunded_at`` stamped.
* Already-acked entry → skipped (funds already released).
* Already-refunded entry → skipped (idempotency guard).
* Entry without attention_fee → skipped.
* Entry without ``expires_at`` (legacy row) → skipped.
* Entry not yet expired → skipped.
* Entry within grace period → skipped.
* ``refund_v2`` failure → error count incremented, no stamp written.
* ``refund_v2`` stamp failure (HSET blows up) → still counted as success
  (refund itself succeeded; stamp is best-effort).
* dry_run=True → logs intent, does NOT call refund_v2.
* SCAN key parsing: ZSET key and content key are not mistaken for detail.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.interfaces.escrow_provider import EscrowDetailResult
from acn.services.manifest_ttl_refund_worker import _extract_owner_mid, run_once

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _make_entry(
    *,
    mid: str = "aabbcc",
    owner_id: str = "agent-alice",
    escrow_id: str | None = "esc_abc",
    acked: bool = False,
    refunded: bool = False,
    expired: bool = True,
    grace_seconds: int = 300,
    has_expires_at: bool = True,
) -> dict[bytes, bytes]:
    """Build a Redis HGETALL-style bytes dict for a manifest detail HASH."""
    now = _now_ms()
    if expired:
        # expired + grace elapsed
        expires_at_ms = now - (grace_seconds + 60) * 1000
    else:
        # still alive
        expires_at_ms = now + 60_000

    attn: dict = {}
    if escrow_id:
        attn["escrow_id"] = escrow_id
        attn["task_id"] = "acn:attn:cafebabe"
        attn["amount"] = 50
        attn["currency"] = "credits"
        if refunded:
            attn["refunded_at"] = now - 1000

    extra: dict = {}
    if attn:
        extra["attention_fee"] = attn

    row: dict[str, str] = {
        "mid": mid,
        "sender_id": "agent-bob",
        "summary": "hello",
        "ts": str(now - 10_000),
        "content_size": "42",
        "extra": json.dumps(extra) if extra else "",
    }
    if has_expires_at:
        row["expires_at"] = str(expires_at_ms)
    if acked:
        row["acked_at"] = str(now - 5_000)

    return {k.encode(): v.encode() for k, v in row.items()}


def _make_redis(entries: dict[str, dict[bytes, bytes]]) -> MagicMock:
    """Build a minimal async Redis stub.

    ``entries`` maps ``detail_key → HGETALL result``.
    SCAN iterates the keys.
    """
    redis = MagicMock()

    async def _scan_iter(match=None, count=None):
        for k in entries:
            yield k.encode()

    redis.scan_iter = _scan_iter

    async def _hgetall(key):
        raw = key.decode() if isinstance(key, bytes) else key
        return entries.get(raw, {})

    redis.hgetall = AsyncMock(side_effect=_hgetall)

    async def _hget(key, field):
        raw = key.decode() if isinstance(key, bytes) else key
        row = entries.get(raw, {})
        field_b = field.encode() if isinstance(field, str) else field
        val = row.get(field_b) or row.get(field.encode() if isinstance(field, str) else field)
        if val is None:
            return None
        return val

    redis.hget = AsyncMock(side_effect=_hget)
    redis.hset = AsyncMock(return_value=1)
    return redis


def _ok_escrow() -> MagicMock:
    ep = MagicMock()
    ep.refund_v2 = AsyncMock(
        return_value=EscrowDetailResult(
            success=True,
            escrow_id="esc_abc",
            status="REFUNDED",
        )
    )
    return ep


# ---------------------------------------------------------------------------
# _extract_owner_mid unit tests
# ---------------------------------------------------------------------------


class TestExtractOwnerMid:
    def test_valid_detail_key(self):
        key = "acn:manifest:{agent-alice}:deadbeef001122334455667788990011"
        assert _extract_owner_mid(key) == ("agent-alice", "deadbeef001122334455667788990011")

    def test_zset_key_returns_none(self):
        # ZSET index key has no mid suffix
        assert _extract_owner_mid("acn:manifest:{agent-alice}") is None

    def test_content_key_returns_none(self):
        assert _extract_owner_mid("acn:content:{agent-alice}:deadbeef") is None

    def test_malformed_key_returns_none(self):
        assert _extract_owner_mid("acn:manifest:agent-alice:mid") is None

    def test_empty_mid_returns_none(self):
        assert _extract_owner_mid("acn:manifest:{agent-alice}:") is None


# ---------------------------------------------------------------------------
# run_once happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_expired_unacked_entry_is_refunded(self):
        mid = "aabbcc001122334455667788"
        owner = "agent-alice"
        key = f"acn:manifest:{{{owner}}}:{mid}"
        entries = {key: _make_entry(mid=mid, owner_id=owner)}
        redis = _make_redis(entries)
        ep = _ok_escrow()

        counts = await run_once(redis, ep)

        assert counts["refunded"] == 1
        assert counts["errors"] == 0
        assert counts["skipped"] == 0
        ep.refund_v2.assert_awaited_once_with(
            escrow_id="esc_abc",
            reason="attention_fee_ttl_expired",
        )
        # ``refunded_at`` was stamped into the HASH
        redis.hset.assert_awaited_once()
        call_kwargs = redis.hset.await_args
        assert "extra" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_multiple_entries_all_refunded(self):
        entries = {}
        for i in range(3):
            mid = f"mid{i:032x}"
            owner = "agent-alice"
            key = f"acn:manifest:{{{owner}}}:{mid}"
            entries[key] = _make_entry(mid=mid, owner_id=owner, escrow_id=f"esc_{i}")
        redis = _make_redis(entries)
        ep = _ok_escrow()
        ep.refund_v2 = AsyncMock(
            return_value=EscrowDetailResult(success=True, status="REFUNDED")
        )

        counts = await run_once(redis, ep)

        assert counts["refunded"] == 3
        assert ep.refund_v2.await_count == 3


# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------


class TestSkipConditions:
    @pytest.mark.asyncio
    async def test_acked_entry_is_skipped(self):
        mid = "aabbcc001122334455667788"
        owner = "agent-alice"
        key = f"acn:manifest:{{{owner}}}:{mid}"
        entries = {key: _make_entry(mid=mid, owner_id=owner, acked=True)}
        ep = _ok_escrow()

        counts = await run_once(_make_redis(entries), ep)

        assert counts["skipped"] == 1
        assert counts["refunded"] == 0
        ep.refund_v2.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_already_refunded_entry_is_skipped(self):
        mid = "aabbcc001122334455667788"
        owner = "agent-alice"
        key = f"acn:manifest:{{{owner}}}:{mid}"
        entries = {key: _make_entry(mid=mid, owner_id=owner, refunded=True)}
        ep = _ok_escrow()

        counts = await run_once(_make_redis(entries), ep)

        assert counts["skipped"] == 1
        ep.refund_v2.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_attention_fee_is_skipped(self):
        mid = "aabbcc001122334455667788"
        owner = "agent-alice"
        key = f"acn:manifest:{{{owner}}}:{mid}"
        entries = {key: _make_entry(mid=mid, owner_id=owner, escrow_id=None)}
        ep = _ok_escrow()

        counts = await run_once(_make_redis(entries), ep)

        assert counts["skipped"] == 1
        ep.refund_v2.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_legacy_row_no_expires_at_is_skipped(self):
        mid = "aabbcc001122334455667788"
        owner = "agent-alice"
        key = f"acn:manifest:{{{owner}}}:{mid}"
        entries = {key: _make_entry(mid=mid, owner_id=owner, has_expires_at=False)}
        ep = _ok_escrow()

        counts = await run_once(_make_redis(entries), ep)

        assert counts["skipped"] == 1
        ep.refund_v2.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_not_yet_expired_is_skipped(self):
        mid = "aabbcc001122334455667788"
        owner = "agent-alice"
        key = f"acn:manifest:{{{owner}}}:{mid}"
        entries = {key: _make_entry(mid=mid, owner_id=owner, expired=False)}
        ep = _ok_escrow()

        counts = await run_once(_make_redis(entries), ep)

        assert counts["skipped"] == 1
        ep.refund_v2.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_within_grace_period_is_skipped(self):
        """Entry expired 10 s ago but grace_seconds=300 → skip."""
        mid = "aabbcc001122334455667788"
        owner = "agent-alice"
        key = f"acn:manifest:{{{owner}}}:{mid}"
        now = _now_ms()
        # expires_at = 10 s ago, but grace = 300 s
        row = _make_entry(mid=mid, owner_id=owner, expired=True, grace_seconds=300)
        # override expires_at to be only 10s in the past
        row[b"expires_at"] = str(now - 10_000).encode()
        entries = {key: row}
        ep = _ok_escrow()

        counts = await run_once(_make_redis(entries), ep, grace_seconds=300)

        assert counts["skipped"] == 1
        ep.refund_v2.assert_not_awaited()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_refund_failure_increments_errors(self):
        mid = "aabbcc001122334455667788"
        owner = "agent-alice"
        key = f"acn:manifest:{{{owner}}}:{mid}"
        entries = {key: _make_entry(mid=mid, owner_id=owner)}
        ep = MagicMock()
        ep.refund_v2 = AsyncMock(
            return_value=EscrowDetailResult(
                success=False,
                error="escrow already cancelled",
            )
        )
        redis = _make_redis(entries)

        counts = await run_once(redis, ep)

        assert counts["errors"] == 1
        assert counts["refunded"] == 0
        # No stamp should have been written
        redis.hset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refund_exception_increments_errors(self):
        mid = "aabbcc001122334455667788"
        owner = "agent-alice"
        key = f"acn:manifest:{{{owner}}}:{mid}"
        entries = {key: _make_entry(mid=mid, owner_id=owner)}
        ep = MagicMock()
        ep.refund_v2 = AsyncMock(side_effect=RuntimeError("network timeout"))
        redis = _make_redis(entries)

        counts = await run_once(redis, ep)

        assert counts["errors"] == 1
        assert counts["refunded"] == 0

    @pytest.mark.asyncio
    async def test_stamp_failure_still_counts_as_refunded(self):
        """``refund_v2`` succeeded but HSET for ``refunded_at`` raises.
        The entry is still counted as refunded — the stamp is best-effort.
        """
        mid = "aabbcc001122334455667788"
        owner = "agent-alice"
        key = f"acn:manifest:{{{owner}}}:{mid}"
        entries = {key: _make_entry(mid=mid, owner_id=owner)}
        ep = _ok_escrow()
        redis = _make_redis(entries)
        redis.hset = AsyncMock(side_effect=RuntimeError("Redis hiccup"))

        counts = await run_once(redis, ep)

        assert counts["refunded"] == 1
        assert counts["errors"] == 0
        ep.refund_v2.assert_awaited_once()


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_does_not_call_refund(self):
        mid = "aabbcc001122334455667788"
        owner = "agent-alice"
        key = f"acn:manifest:{{{owner}}}:{mid}"
        entries = {key: _make_entry(mid=mid, owner_id=owner)}
        ep = _ok_escrow()
        redis = _make_redis(entries)

        counts = await run_once(redis, ep, dry_run=True)

        assert counts["refunded"] == 1
        ep.refund_v2.assert_not_awaited()
        redis.hset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dry_run_empty_keyspace(self):
        redis = _make_redis({})
        ep = _ok_escrow()

        counts = await run_once(redis, ep, dry_run=True)

        assert counts == {"scanned": 0, "skipped": 0, "refunded": 0, "errors": 0}
