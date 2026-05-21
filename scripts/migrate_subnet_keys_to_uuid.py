"""Migrate Redis subnet keys from slug-based to UUID-based naming.

After the Alembic migration ``1a2b3c4d5e6f_migrate_subnet_id_to_uuid``
runs on PostgreSQL, existing Redis data still uses the old slug-keyed
format:

    acn:subnets:info:{slug}          → acn:subnets:info:{uuid}
    acn:subnets:by_owner:{owner}     → values slug → uuid
    acn:subnets:children:{parent_slug} → acn:subnets:children:{parent_uuid}
    acn:subnets:by_linked_task:{task_id} → values slug → uuid

This script reads the canonical UUID mapping from Postgres (which was
already migrated) and re-keys the Redis data in-place.

Usage
-----
    python scripts/migrate_subnet_keys_to_uuid.py [--dry-run]

Set environment variables DATABASE_URL and REDIS_URL (or let the
default values from ``config.py`` be used).

Safety
------
- The script is idempotent: re-running on already-migrated keys is a
  no-op (UUID keys already exist; slug keys are absent).
- Reads all slug→UUID mappings from Postgres before touching Redis.
- Uses RENAME for the main HASH key (atomic on single-key ops in Redis).
- Writes a ``acn:subnets:slug:{slug}`` string key pointing to the UUID
  so the application's ``find_by_id(slug)`` resolution continues to
  work during any rolling restart window.
- Dry-run mode prints all operations without executing them.
"""

import argparse
import asyncio
import json
import os
import sys

try:
    import redis.asyncio as aioredis
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
except ImportError as exc:
    print(f"Missing dependency: {exc}. Run: pip install redis sqlalchemy asyncpg")
    sys.exit(1)


async def load_slug_uuid_map(database_url: str) -> dict[str, str]:
    """Return {slug: uuid} for every subnet in Postgres."""
    engine = create_async_engine(database_url)
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT slug, id FROM subnets"))
        rows = result.fetchall()
    await engine.dispose()
    return {row[0]: str(row[1]) for row in rows}


async def migrate(database_url: str, redis_url: str, dry_run: bool) -> None:
    label = "[DRY-RUN] " if dry_run else ""

    print(f"Loading slug→UUID map from Postgres ({database_url[:40]}…)")
    slug_to_uuid = await load_slug_uuid_map(database_url)
    print(f"  Found {len(slug_to_uuid)} subnets in Postgres.")

    r = aioredis.from_url(redis_url, decode_responses=True)

    # ------------------------------------------------------------------ #
    # 1. Rename acn:subnets:info:{slug} → acn:subnets:info:{uuid}
    # ------------------------------------------------------------------ #
    print("\n[1] Migrating main HASH keys …")
    migrated = 0
    skipped = 0
    async for key in r.scan_iter("acn:subnets:info:*"):
        slug = key.removeprefix("acn:subnets:info:")
        uuid = slug_to_uuid.get(slug)
        if uuid is None:
            print(f"  WARN: no UUID found for slug {slug!r} — skipping")
            skipped += 1
            continue
        new_key = f"acn:subnets:info:{uuid}"
        if await r.exists(new_key):
            print(f"  SKIP {slug!r} → already at UUID key")
            skipped += 1
            continue
        print(f"  {label}RENAME {key!r} → {new_key!r}")
        if not dry_run:
            # HSET the slug field into the hash before renaming so the
            # application can reconstruct the Subnet entity with its slug.
            data = await r.hgetall(key)
            if "slug" not in data:
                if not dry_run:
                    await r.hset(key, "slug", slug)
            await r.rename(key, new_key)
            # Write slug → UUID lookup string key
            await r.set(f"acn:subnets:slug:{slug}", uuid)
        migrated += 1
    print(f"  Done: {migrated} migrated, {skipped} skipped.")

    # ------------------------------------------------------------------ #
    # 2. Update acn:subnets:by_owner:{owner} sets (slug values → uuid)
    # ------------------------------------------------------------------ #
    print("\n[2] Updating by_owner sets …")
    async for key in r.scan_iter("acn:subnets:by_owner:*"):
        members = await r.smembers(key)
        slugs = [m for m in members if m in slug_to_uuid]
        if not slugs:
            continue
        print(f"  {label}UPDATE {key!r}: replace {len(slugs)} slug values with UUIDs")
        if not dry_run:
            pipe = r.pipeline(transaction=False)
            for slug in slugs:
                uuid = slug_to_uuid[slug]
                pipe.srem(key, slug)
                pipe.sadd(key, uuid)
            await pipe.execute()

    # ------------------------------------------------------------------ #
    # 3. Rename acn:subnets:children:{parent_slug} → children:{parent_uuid}
    #    and update member values (child slugs → child uuids)
    # ------------------------------------------------------------------ #
    print("\n[3] Migrating children index keys …")
    async for key in r.scan_iter("acn:subnets:children:*"):
        parent_slug = key.removeprefix("acn:subnets:children:")
        parent_uuid = slug_to_uuid.get(parent_slug)
        if parent_uuid is None:
            print(f"  WARN: no UUID for parent slug {parent_slug!r} — skipping key rename")
        else:
            new_key = f"acn:subnets:children:{parent_uuid}"
            if not await r.exists(new_key):
                print(f"  {label}RENAME {key!r} → {new_key!r}")
                if not dry_run:
                    await r.rename(key, new_key)
                key = new_key  # continue to update values on the new key

        members = await r.smembers(key)
        child_slugs = [m for m in members if m in slug_to_uuid]
        if child_slugs:
            print(f"  {label}UPDATE {key!r}: replace {len(child_slugs)} slug values")
            if not dry_run:
                pipe = r.pipeline(transaction=False)
                for slug in child_slugs:
                    uuid = slug_to_uuid[slug]
                    pipe.srem(key, slug)
                    pipe.sadd(key, uuid)
                await pipe.execute()

    # ------------------------------------------------------------------ #
    # 4. Update acn:subnets:by_linked_task:{task_id} values (slug → uuid)
    # ------------------------------------------------------------------ #
    print("\n[4] Updating by_linked_task sets …")
    async for key in r.scan_iter("acn:subnets:by_linked_task:*"):
        members = await r.smembers(key)
        slugs = [m for m in members if m in slug_to_uuid]
        if not slugs:
            continue
        print(f"  {label}UPDATE {key!r}: replace {len(slugs)} slug values with UUIDs")
        if not dry_run:
            pipe = r.pipeline(transaction=False)
            for slug in slugs:
                uuid = slug_to_uuid[slug]
                pipe.srem(key, slug)
                pipe.sadd(key, uuid)
            await pipe.execute()

    await r.aclose()
    print("\nMigration complete." if not dry_run else "\nDry-run complete — no changes made.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print operations without executing")
    args = parser.parse_args()

    database_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/acn",
    )
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    asyncio.run(migrate(database_url, redis_url, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
