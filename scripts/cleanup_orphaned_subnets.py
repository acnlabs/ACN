#!/usr/bin/env python3
"""
Clean up orphaned subnets: private subnets with 0 members whose owner agent
has been deleted (e.g. leftover from test/smoke agent cleanup).

Uses the acn/ service layer so the full cascade (children, allowlist,
join_requests) runs correctly. Calls delete_subnet(..., "system") to bypass
ownership checks.

Runs against production DB directly — no new API surface needed.

Usage:
    # Dry run (preview only, default)
    DATABASE_URL=... REDIS_URL=... uv run python scripts/cleanup_orphaned_subnets.py

    # Actually delete
    DATABASE_URL=... REDIS_URL=... uv run python scripts/cleanup_orphaned_subnets.py --execute

    # Relax minimum age guard (default: 7 days)
    ... uv run python scripts/cleanup_orphaned_subnets.py --execute --min-age-days 0

Local run against production (public proxy URLs from Railway dashboard):
    DATABASE_URL="postgresql://postgres:<pw>@ballast.proxy.rlwy.net:<port>/railway" \\
    REDIS_URL="redis://default:<pw>@caboose.proxy.rlwy.net:<port>" \\
    INTERNAL_API_TOKEN=... AUTH0_DOMAIN=... AUTH0_AUDIENCE=... \\
    CORS_ORIGINS='["https://agenticplanet.space"]' DEV_MODE=false \\
    uv run python scripts/cleanup_orphaned_subnets.py
"""

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


async def build_subnet_service():
    """Wire SubnetService the same way api.py does (PG or Redis fallback)."""
    import redis.asyncio as aioredis

    from acn.config import get_settings
    from acn.services.subnet_service import SubnetService

    settings = get_settings()
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    if settings.database_url:
        from acn.infrastructure.persistence.postgres import (
            PostgresAgentRepository,
            PostgresSubnetRepository,
        )
        from acn.infrastructure.persistence.postgres.database import (
            get_engine,
            get_session_factory,
        )
        from acn.infrastructure.persistence.postgres.unit_of_work import PostgresUnitOfWork

        try:
            from acn.infrastructure.persistence.postgres import (
                PostgresSubnetAllowlistRepository,
                PostgresSubnetJoinRequestRepository,
                PostgresTaskRepository,
            )
        except ImportError:
            PostgresSubnetJoinRequestRepository = None  # type: ignore[assignment]
            PostgresSubnetAllowlistRepository = None  # type: ignore[assignment]
            PostgresTaskRepository = None  # type: ignore[assignment]

        engine = get_engine(settings.database_url)
        sf = get_session_factory(engine)
        subnet_repo = PostgresSubnetRepository(sf)
        agent_repo = PostgresAgentRepository(sf, redis_client)
        uow = PostgresUnitOfWork(sf)

        kwargs: dict = {"agent_repository": agent_repo, "unit_of_work": uow}
        if PostgresTaskRepository:
            kwargs["task_repository"] = PostgresTaskRepository(sf, redis_client)
        if PostgresSubnetJoinRequestRepository:
            kwargs["subnet_join_request_repository"] = PostgresSubnetJoinRequestRepository(sf)
        if PostgresSubnetAllowlistRepository:
            kwargs["subnet_allowlist_repository"] = PostgresSubnetAllowlistRepository(sf)

        service = SubnetService(subnet_repo, **kwargs)
        return service, redis_client, "postgres", sf
    else:
        from acn.infrastructure.persistence.redis import (
            RedisAgentRepository,
            RedisSubnetRepository,
        )

        subnet_repo = RedisSubnetRepository(redis_client)
        agent_repo = RedisAgentRepository(redis_client)
        service = SubnetService(subnet_repo, agent_repository=agent_repo)
        return service, redis_client, "redis", None


async def find_orphaned_pg(session_factory, cutoff: datetime) -> list[tuple[str, str | None, datetime | None]]:
    """Return (slug, parent_slug, created_at) for orphaned subnets via raw SQL."""
    from sqlalchemy import text

    async with session_factory() as session:
        # member_agent_ids is JSONB: NULL means the column was never populated
        # (0 members). Empty array [] also means 0 members. Both cases are
        # safe to delete when the subnet is private and the owner agent is gone.
        result = await session.execute(text("""
            SELECT slug, parent_slug, created_at
            FROM subnets
            WHERE is_private = true
              AND (
                member_agent_ids IS NULL
                OR member_agent_ids = 'null'::jsonb
                OR jsonb_array_length(member_agent_ids) = 0
              )
              AND (created_at IS NULL OR created_at < :cutoff)
            ORDER BY created_at
        """), {"cutoff": cutoff})
        return [(r[0], r[1], r[2]) for r in result.fetchall()]


async def find_orphaned_redis(redis_client, cutoff: datetime) -> list[tuple[str, str | None, datetime | None]]:
    """Scan Redis for orphaned subnets."""
    keys = [k async for k in redis_client.scan_iter("subnet:*")]
    orphaned = []
    for key in keys:
        data = await redis_client.hgetall(key)
        if not data:
            continue
        if data.get("is_private") != "true":
            continue
        if data.get("name"):
            continue
        members = data.get("member_agent_ids", "")
        if members and members not in ("", "[]", "null"):
            continue
        sid = data.get("subnet_id", key.split(":", 1)[-1])
        parent = data.get("parent_subnet_id") or None
        created_raw = data.get("created_at")
        created_at = None
        if created_raw:
            try:
                created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except ValueError:
                pass
        if created_at and created_at > cutoff:
            continue
        orphaned.append((sid, parent, created_at))
    return orphaned


async def run(execute: bool, min_age_days: int) -> None:
    service, redis_client, backend, session_factory = await build_subnet_service()
    cutoff = datetime.now(UTC) - timedelta(days=min_age_days)

    print(f"Backend:  {backend}")
    print(f"Mode:     {'EXECUTE (will delete!)' if execute else 'DRY RUN (preview only)'}")
    print(f"Min age:  {min_age_days} days (cutoff: {cutoff.strftime('%Y-%m-%d')})")
    print()

    if backend == "postgres":
        from sqlalchemy import text
        async with session_factory() as session:
            total = (await session.execute(text("SELECT COUNT(*) FROM subnets"))).scalar()
        print(f"Total subnets in DB: {total}")
        orphaned = await find_orphaned_pg(session_factory, cutoff)
    else:
        orphaned = await find_orphaned_redis(redis_client, cutoff)

    print(f"Orphaned (0-member, private, no name): {len(orphaned)}")
    print()

    if not orphaned:
        print("Nothing to delete.")
        await redis_client.aclose()
        return

    print("=== Subnets to DELETE ===")
    for sid, parent, created_at in orphaned:
        age = created_at.strftime("%Y-%m-%d") if created_at else "unknown"
        parent_short = (parent or "")[:8] or "-"
        print(f"  {sid}  parent={parent_short}  created={age}")

    if not execute:
        print("\nDry run complete. Add --execute to actually delete.")
        await redis_client.aclose()
        return

    print("\nDeleting...")
    deleted, failed = 0, 0
    for sid, _, _ in orphaned:
        try:
            await service.delete_subnet(sid, "system")
            print(f"  ✓ {sid}")
            deleted += 1
        except Exception as e:
            print(f"  ✗ {sid}  {e}")
            failed += 1

    await redis_client.aclose()
    print(f"\nDone. Deleted: {deleted}, Failed: {failed}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Cleanup orphaned 0-member private subnets")
    parser.add_argument("--execute", action="store_true", help="Actually delete (default is dry-run)")
    parser.add_argument("--min-age-days", type=int, default=7, help="Only delete subnets older than N days")
    args = parser.parse_args()
    asyncio.run(run(args.execute, args.min_age_days))


if __name__ == "__main__":
    main()
