"""
Migration: Backfill new Task fields into JSONB metadata for existing tasks.

Run once after deploying the ACN Task API refactor.
Idempotent — safe to run multiple times (checks if fields already exist).

Usage:
    cd acn
    uv run python scripts/migrate_task_fields.py

    # Dry-run (no commit):
    uv run python scripts/migrate_task_fields.py --dry-run

What it does:
  1. require_join_approval  ← derived from mode='assigned'
  2. auto_approve           ← derived from metadata->>'approval_type' == 'auto'
  3. use_escrow             ← derived from reward_amount > 0
     (historical: assume rewarded tasks used escrow by default)

Only updates rows where require_join_approval key is absent (first-time migration).
"""

import argparse
import os
import sys

import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")


SQL_BACKFILL = """
UPDATE tasks
SET metadata = metadata ||
  jsonb_build_object(
    'require_join_approval', (mode = 'assigned'),
    'auto_approve',          (metadata->>'approval_type' = 'auto'),
    'use_escrow',            (
      reward_amount IS NOT NULL
      AND reward_amount != ''
      AND reward_amount != '0'
      AND CAST(NULLIF(TRIM(reward_amount), '') AS NUMERIC) > 0
    )
  )
WHERE metadata->>'require_join_approval' IS NULL;
"""

SQL_COUNT_PENDING = """
SELECT COUNT(*) FROM tasks
WHERE metadata->>'require_join_approval' IS NULL;
"""

SQL_COUNT_TOTAL = "SELECT COUNT(*) FROM tasks;"


def main():
    parser = argparse.ArgumentParser(description="Backfill Task JSONB fields")
    parser.add_argument("--dry-run", action="store_true", help="Show counts but do not commit")
    args = parser.parse_args()

    if not DATABASE_URL:
        print("ERROR: DATABASE_URL environment variable not set", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(SQL_COUNT_TOTAL)
            total = cur.fetchone()[0]
            cur.execute(SQL_COUNT_PENDING)
            pending = cur.fetchone()[0]

        print(f"Total tasks:          {total}")
        print(f"Needs backfill:       {pending}")

        if pending == 0:
            print("Nothing to migrate — all tasks already have new fields.")
            return

        if args.dry_run:
            print("[DRY RUN] Would update", pending, "rows. Skipping commit.")
            return

        with conn.cursor() as cur:
            cur.execute(SQL_BACKFILL)
            updated = cur.rowcount

        conn.commit()
        print(f"✓ Migrated {updated} tasks successfully.")

    except Exception as e:
        conn.rollback()
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
