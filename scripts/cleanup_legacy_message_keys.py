#!/usr/bin/env python3
"""One-shot script to reclaim Redis memory occupied by legacy message keys.

Two key families were superseded by the inbox-refactor sprint and are no
longer written by any code path; their data is stale and safe to delete.

  1. acn:messages:agent:*
     Old per-agent message-history sorted sets written by the pre-refactor
     code.  The new path writes to acn:inbox:{agent_id} instead.
     These keys have no TTL, so they persist until manually removed.

  2. acn:messages:log:{route_id}
     Old per-route audit strings (SETEX 7 days) written before the switch
     to the global capped stream (acn:messages:log:stream).  Most will have
     already expired via their 7-day TTL, but any created just before the
     migration cutover may still linger.
     The live key `acn:messages:log:stream` is explicitly excluded.

Usage
-----
    # Preview how many keys exist (default dry-run, no changes made)
    REDIS_URL=redis://localhost:6379 python scripts/cleanup_legacy_message_keys.py

    # Actually delete
    REDIS_URL=redis://localhost:6379 python scripts/cleanup_legacy_message_keys.py --execute

    # Adjust SCAN batch size (default 200)
    REDIS_URL=redis://localhost:6379 python scripts/cleanup_legacy_message_keys.py --execute --batch 500

Environment
-----------
    REDIS_URL   Redis connection URL (required, e.g. redis://:password@host:6379/0)
"""

import argparse
import os
import sys
import time

try:
    import redis as redis_lib
except ImportError:
    print("ERROR: redis-py not installed. Run: pip install redis", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Key patterns and the one live key to preserve
# ---------------------------------------------------------------------------

PATTERNS = [
    "acn:messages:agent:*",   # legacy per-agent sorted sets
    "acn:messages:log:*",     # legacy per-route audit strings
]

# The only key under acn:messages:log:* that must NOT be deleted
PRESERVE = {"acn:messages:log:stream"}


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def scan_and_collect(client: redis_lib.Redis, pattern: str, batch: int) -> list[str]:
    """Return all keys matching *pattern*, excluding those in PRESERVE."""
    found: list[str] = []
    cursor = 0
    while True:
        cursor, keys = client.scan(cursor, match=pattern, count=batch)
        for k in keys:
            key = k.decode() if isinstance(k, bytes) else k
            if key not in PRESERVE:
                found.append(key)
        if cursor == 0:
            break
    return found


def delete_in_batches(
    client: redis_lib.Redis,
    keys: list[str],
    batch: int,
    dry_run: bool,
) -> int:
    """UNLINK keys in batches of *batch*. Returns number of keys processed."""
    total = 0
    for i in range(0, len(keys), batch):
        chunk = keys[i : i + batch]
        if not dry_run:
            client.unlink(*chunk)
        total += len(chunk)
        print(f"  {'[dry-run] would delete' if dry_run else 'deleted'} {total}/{len(keys)} keys")
    return total


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Actually delete keys (default: dry-run, no changes made)",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=200,
        help="Number of keys per SCAN / UNLINK batch (default: 200)",
    )
    args = parser.parse_args()

    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        print("ERROR: REDIS_URL environment variable is required.", file=sys.stderr)
        sys.exit(1)

    dry_run = not args.execute
    mode_label = "DRY-RUN (no changes)" if dry_run else "EXECUTE (keys will be deleted)"
    print(f"\n=== cleanup_legacy_message_keys  mode={mode_label} ===\n")

    client = redis_lib.from_url(redis_url, decode_responses=False)

    grand_total = 0
    for pattern in PATTERNS:
        print(f"Scanning pattern: {pattern!r} (excluding: {PRESERVE})")
        t0 = time.monotonic()
        keys = scan_and_collect(client, pattern, args.batch)
        elapsed = time.monotonic() - t0
        print(f"  Found {len(keys)} keys in {elapsed:.2f}s")

        if keys:
            deleted = delete_in_batches(client, keys, args.batch, dry_run)
            grand_total += deleted
        print()

    action = "would be deleted" if dry_run else "deleted"
    print(f"Done. {grand_total} keys {action} in total.")
    if dry_run and grand_total:
        print("Re-run with --execute to apply.")


if __name__ == "__main__":
    main()
