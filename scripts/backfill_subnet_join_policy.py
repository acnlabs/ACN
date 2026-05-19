#!/usr/bin/env python3
"""Redis backfill: subnet ``join_policy`` (ADR-0004 Phase 1).

Mirrors the Alembic migration ``f0a1b2c3d4e5_add_subnet_join_policy_field``
on the Redis side of dual-store deployments. Every ``acn:subnets:info:{id}``
HASH gains a ``join_policy`` field if it is missing; rows with
``is_private == 'True'`` are set to ``'approval'`` (matching the
``UPDATE ... WHERE is_private = true`` clause in the migration),
everything else gets ``'open'`` (the entity default).

Why this script must run
------------------------
Per ``acn/AGENTS.md``, Redis is the canonical store; Postgres is an
optional cache / fallback. A Redis-only deployment receives **no
benefit** from running the Alembic migration alone — the
``join_policy`` field never reaches Redis until either (a) this
script runs, or (b) every subnet is naturally re-saved through
``RedisSubnetRepository.save`` (which writes the field as part of
the standard payload).

Reads are already safe before this script runs: the repo's
``_dict_to_subnet`` auto-upgrades missing ``join_policy`` on
``is_private == 'True'`` rows to ``'approval'``, matching the
Alembic backfill rule and the entity invariant. Running this script
makes the **stored** representation match the read-side semantics,
removing the asymmetry.

Idempotency
-----------
The script tracks per-row completion via a ``backfill_v0004=done``
sentinel field on each subnet HASH. Re-runs short-circuit any row
that already carries the sentinel, so the script is safe to invoke
repeatedly (operator retries after a network blip, ops runbook
re-runs the migration step, etc.). The sentinel is preserved on
subsequent ``save()`` calls because ``RedisSubnetRepository.save``
issues ``HSET mapping=...`` which only writes the keys it knows
about — the sentinel field sits untouched on every later subnet
mutation.

Usage
-----
::

    REDIS_URL=redis://localhost:6379 python scripts/backfill_subnet_join_policy.py

Exit codes
----------
- ``0``  — backfill completed (counts logged below).
- ``1``  — ``REDIS_URL`` missing or unreachable.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Final

import redis.asyncio as aioredis  # type: ignore[import-untyped]
import structlog  # type: ignore[import-untyped]

REDIS_URL: Final[str] = os.environ.get("REDIS_URL", "redis://localhost:6379")
SUBNET_KEY_PREFIX: Final[str] = "acn:subnets:info:"
SENTINEL_FIELD: Final[str] = "backfill_v0004"
SENTINEL_VALUE: Final[str] = "done"

logger = structlog.get_logger()


def _decode(value: bytes | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode()
    return value


async def _backfill_one(
    redis: aioredis.Redis, subnet_key: str
) -> tuple[str, str | None]:
    """Backfill one ``acn:subnets:info:{id}`` HASH.

    Returns a ``(status, applied_value)`` tuple:

    - ``("already_done", None)``   — sentinel present, no change made.
    - ``("already_set", existing)`` — ``join_policy`` already populated by
      a save-through-entity path; sentinel written, no value change.
    - ``("updated", value)``        — ``join_policy`` written for the
      first time; sentinel written.
    - ``("missing", None)``         — HASH disappeared between SCAN and
      HGETALL (concurrent delete). Skipped.
    """
    raw = await redis.hgetall(subnet_key)
    if not raw:
        return ("missing", None)

    # ``hgetall`` returns ``dict[bytes, bytes]`` on async client; decode
    # selectively for the fields we care about. Other fields stay
    # untouched.
    sentinel = _decode(raw.get(b"backfill_v0004")) or _decode(
        raw.get("backfill_v0004")
    )
    existing = _decode(raw.get(b"join_policy")) or _decode(
        raw.get("join_policy")
    )
    is_private = (
        _decode(raw.get(b"is_private")) or _decode(raw.get("is_private")) or ""
    ) == "True"

    if sentinel == SENTINEL_VALUE:
        return ("already_done", None)

    if existing:
        # Field is already populated (e.g. by a normal save() call after
        # the entity field landed). Write only the sentinel so future
        # passes short-circuit.
        await redis.hset(
            subnet_key, mapping={SENTINEL_FIELD: SENTINEL_VALUE}
        )
        return ("already_set", existing)

    value = "approval" if is_private else "open"
    await redis.hset(
        subnet_key,
        mapping={"join_policy": value, SENTINEL_FIELD: SENTINEL_VALUE},
    )
    return ("updated", value)


async def main() -> int:
    if not REDIS_URL:
        print("ERROR: REDIS_URL env var is required", file=sys.stderr)
        return 1

    redis = aioredis.from_url(REDIS_URL)
    try:
        await redis.ping()
    except Exception as exc:  # noqa: BLE001 — surface any connect failure
        print(f"ERROR: cannot reach REDIS_URL={REDIS_URL}: {exc}", file=sys.stderr)
        return 1

    counts: dict[str, int] = {
        "scanned": 0,
        "already_done": 0,
        "already_set": 0,
        "updated_approval": 0,
        "updated_open": 0,
        "missing": 0,
    }

    async for key in redis.scan_iter(match=f"{SUBNET_KEY_PREFIX}*"):
        subnet_key = _decode(key)
        if subnet_key is None:
            continue
        counts["scanned"] += 1
        status, value = await _backfill_one(redis, subnet_key)
        if status == "already_done":
            counts["already_done"] += 1
        elif status == "already_set":
            counts["already_set"] += 1
        elif status == "missing":
            counts["missing"] += 1
        elif status == "updated":
            if value == "approval":
                counts["updated_approval"] += 1
            else:
                counts["updated_open"] += 1
        logger.debug(
            "subnet_join_policy_backfill_row",
            subnet_key=subnet_key,
            status=status,
            value=value,
        )

    logger.info("subnet_join_policy_backfill_done", **counts)
    print(
        "subnet_join_policy backfill complete: "
        + ", ".join(f"{k}={v}" for k, v in counts.items())
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
