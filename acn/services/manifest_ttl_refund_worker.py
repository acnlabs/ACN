"""Manifest TTL Refund Worker — Phase 3 Module B follow-up.

Scans Redis for manifest entries that:
  1. Carry a locked ``attention_fee`` (``extra.attention_fee.escrow_id`` set)
  2. Have NOT been acknowledged yet (``acked_at`` absent)
  3. Have expired (``expires_at_ms < now``)

For each matching entry the worker calls the backend Escrow
``refund_v2`` API to return the locked Credits to the sender, then
marks ``extra.attention_fee.refunded_at`` on the HASH so a restart
cannot double-refund.

Design decisions
----------------
* **SCAN-based discovery** — the worker iterates ``acn:manifest:{*}:*``
  HASHes using a cursor SCAN so it never blocks the Redis event loop.
  The pattern deliberately matches *detail* HASHes (``{owner}:mid``
  form) and not ZSET index keys (``{owner}`` only) or content keys
  (``acn:content:...``). This works on both standalone and cluster
  Redis (cluster SCAN iterates the node that owns the slot; the hash
  tag ``{owner_id}`` keeps all three keys for a given owner on the
  same slot, so no cross-slot mis-scan is possible).

* **Idempotency on ACN side** — before calling ``refund_v2`` the
  worker reads the HASH again (``HGET extra``) and aborts if
  ``extra.attention_fee.refunded_at`` is already set. This guards
  against a worker crash that completed the backend call but died
  before writing the flag. Backend ``refund_v2`` itself also guards
  against double-refund by checking escrow status; the ACN-side flag
  is extra safety and makes the audit log honest.

* **Grace period** — the worker only refunds entries that have been
  expired for at least ``REFUND_GRACE_SECONDS`` (default 300 s).
  This avoids a race between "Redis TTL fires, entry evicted, Redis
  key gone" and "worker scanned the key, read it, entry evicted".
  The grace period is applied to ``expires_at_ms``; entries with
  ``expires_at_ms`` in the future are always skipped.

* **Run cadence** — the caller (``api.py`` lifespan) runs this every
  ``RUN_INTERVAL_SECONDS`` (default 300 s). A full Redis SCAN over
  millions of keys takes seconds on a typical deployment; 5-minute
  intervals are well within budget. Operators can tune via env.

* **Legacy rows** — entries written before Phase 3 have no
  ``expires_at`` field (returns ``None`` from ``_decode_entry``).
  These are silently skipped to avoid confusing old data with
  genuinely expired-and-stuck entries. Legacy escrows must be
  refunded manually if needed.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import structlog  # type: ignore[import-untyped]

if TYPE_CHECKING:
    import redis.asyncio as redis

    from ..core.interfaces.escrow_provider import IEscrowProvider

logger = structlog.get_logger()

# Seconds after manifest expiry before the worker issues the refund.
# Acts as a safety buffer against scan-read-then-eviction races.
REFUND_GRACE_SECONDS: int = 300  # 5 minutes

# How many HASH entries the SCAN cursor fetches per round-trip.
# Larger values are faster on large keyspaces but slightly block
# the Redis event loop per call; 100 is a safe default.
_SCAN_COUNT: int = 100

# SCAN pattern for manifest detail HASHes.  The ``{*}`` is a literal
# glob pattern that matches any owner hash tag, and ``*`` at the end
# matches any mid.  This matches:
#   acn:manifest:{alice123}:deadbeef00000000000000000000001a
# but NOT:
#   acn:manifest:{alice123}          ← ZSET index key (no mid suffix)
#   acn:content:{alice123}:deadbeef  ← content STRING key
_DETAIL_KEY_PATTERN = "acn:manifest:{*}:*"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _extract_owner_mid(key: str) -> tuple[str, str] | None:
    """Parse ``acn:manifest:{<owner>}:<mid>`` → ``(owner, mid)``.

    Returns ``None`` when the key doesn't match the expected format
    (e.g. the ZSET index key ``acn:manifest:{owner}`` without a mid
    suffix slips through the glob on some Redis versions).
    """
    # key format: acn:manifest:{owner_id}:mid_hex
    try:
        prefix, rest = key.split(":{", 1)
    except ValueError:
        return None
    if prefix != "acn:manifest":
        return None
    try:
        owner_part, mid = rest.split("}:", 1)
    except ValueError:
        return None
    if not owner_part or not mid:
        return None
    return owner_part, mid


async def run_once(
    redis_client: redis.Redis,
    escrow_provider: IEscrowProvider,
    *,
    grace_seconds: int = REFUND_GRACE_SECONDS,
    dry_run: bool = False,
) -> dict[str, int]:
    """Scan all manifest detail HASHes and refund expired-unacked fees.

    Args:
        redis_client: Async Redis client (can be standalone or cluster).
        escrow_provider: Escrow backend client for ``refund_v2``.
        grace_seconds: Only refund entries whose ``expires_at_ms`` is
            at least this many seconds in the past.  Default 300 s.
        dry_run: If ``True``, log what would be done but don't call
            ``refund_v2`` or write ``refunded_at``.  Useful for
            monitoring without side effects.

    Returns:
        A dict with counts:
          * ``scanned``  — total HASH keys examined
          * ``skipped``  — entries without a fee or already
                           acked/refunded or not-yet-expired
          * ``refunded`` — entries successfully refunded
          * ``errors``   — entries where ``refund_v2`` failed
    """
    now_ms = _now_ms()
    deadline_ms = now_ms - grace_seconds * 1000

    counts: dict[str, int] = {"scanned": 0, "skipped": 0, "refunded": 0, "errors": 0}

    async for raw_key in redis_client.scan_iter(
        match=_DETAIL_KEY_PATTERN, count=_SCAN_COUNT
    ):
        key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
        parsed = _extract_owner_mid(key)
        if parsed is None:
            continue  # ZSET or unexpected format — skip silently

        owner_id, mid = parsed
        counts["scanned"] += 1

        try:
            should_refund, escrow_id = await _check_entry(
                redis_client, key, deadline_ms
            )
        except Exception as exc:
            logger.warning(
                "manifest_ttl_worker_check_error",
                key=key,
                error=str(exc),
            )
            counts["errors"] += 1
            continue

        if not should_refund or escrow_id is None:
            counts["skipped"] += 1
            continue

        if dry_run:
            logger.info(
                "manifest_ttl_worker_would_refund [dry_run]",
                owner_id=owner_id,
                mid=mid,
                escrow_id=escrow_id,
            )
            counts["refunded"] += 1
            continue

        success = await _do_refund(redis_client, escrow_provider, key, owner_id, mid, escrow_id)
        if success:
            counts["refunded"] += 1
        else:
            counts["errors"] += 1

    return counts


async def _check_entry(
    redis_client: redis.Redis,
    detail_key: str,
    deadline_ms: int,
) -> tuple[bool, str | None]:
    """Read the HASH and decide whether a refund is warranted.

    Returns ``(True, escrow_id)`` when all conditions are met,
    ``(False, None)`` otherwise.
    """
    raw = await redis_client.hgetall(detail_key)
    if not raw:
        return False, None  # evicted between SCAN and HGETALL

    def _u(v: Any) -> str:
        return v.decode() if isinstance(v, bytes) else str(v)

    decoded = {_u(k): _u(v) for k, v in raw.items()}

    # 1. Must have an expires_at (Phase 3+ rows only).
    expires_raw = decoded.get("expires_at")
    if not expires_raw:
        return False, None  # legacy row — skip

    try:
        expires_at_ms = int(expires_raw)
    except ValueError:
        return False, None

    # 2. Must have passed the grace-period deadline.
    if expires_at_ms > deadline_ms:
        return False, None  # not expired yet (or within grace period)

    # 3. Must have an attention_fee with an escrow_id.
    extra_blob = decoded.get("extra")
    if not extra_blob:
        return False, None

    try:
        extra: dict[str, Any] = json.loads(extra_blob)
    except json.JSONDecodeError:
        return False, None

    attn = extra.get("attention_fee")
    if not isinstance(attn, dict):
        return False, None

    escrow_id = attn.get("escrow_id")
    if not escrow_id:
        return False, None

    # 4. Must NOT already be acked.
    if decoded.get("acked_at"):
        return False, None  # funds already released to recipient

    # 5. Must NOT already be refunded (idempotency guard).
    if attn.get("refunded_at"):
        return False, None  # worker already processed this in a prior run

    return True, str(escrow_id)


async def _do_refund(
    redis_client: redis.Redis,
    escrow_provider: IEscrowProvider,
    detail_key: str,
    owner_id: str,
    mid: str,
    escrow_id: str,
) -> bool:
    """Call ``refund_v2`` and stamp ``refunded_at`` on success.

    Returns ``True`` on success, ``False`` on any failure.
    """
    try:
        result = await escrow_provider.refund_v2(
            escrow_id=escrow_id,
            reason="attention_fee_ttl_expired",
        )
    except Exception as exc:
        logger.error(
            "manifest_ttl_worker_refund_exception",
            owner_id=owner_id,
            mid=mid,
            escrow_id=escrow_id,
            error=str(exc),
        )
        return False

    if not result.success:
        logger.warning(
            "manifest_ttl_worker_refund_failed",
            owner_id=owner_id,
            mid=mid,
            escrow_id=escrow_id,
            error=result.error,
        )
        return False

    # Stamp ``refunded_at`` to prevent double-refund on restart.
    # Best-effort: if this HSET fails (Redis hiccup) the worker will
    # attempt the refund again on the next run. Backend's own
    # idempotency guard on the REFUNDED escrow state will reject the
    # second call cleanly.
    try:
        raw = await redis_client.hget(detail_key, "extra")
        extra_blob = raw.decode() if isinstance(raw, bytes) else (raw or "")
        try:
            extra: dict[str, Any] = json.loads(extra_blob) if extra_blob else {}
        except json.JSONDecodeError:
            extra = {}

        attn = extra.get("attention_fee")
        if isinstance(attn, dict):
            attn["refunded_at"] = int(time.time() * 1000)
            extra["attention_fee"] = attn
            await redis_client.hset(
                detail_key,
                "extra",
                json.dumps(extra, ensure_ascii=False, separators=(",", ":")),
            )
    except Exception as exc:
        logger.warning(
            "manifest_ttl_worker_stamp_failed",
            owner_id=owner_id,
            mid=mid,
            escrow_id=escrow_id,
            error=str(exc),
        )
        # Don't return False — the refund itself succeeded; the stamp
        # failure just means a possible redundant backend call on next
        # run, which backend will safely reject.

    logger.info(
        "manifest_ttl_worker_refunded",
        owner_id=owner_id,
        mid=mid,
        escrow_id=escrow_id,
    )
    return True
