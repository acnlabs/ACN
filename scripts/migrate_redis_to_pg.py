#!/usr/bin/env python3
"""Redis → PostgreSQL one-time data migration script.

Migrates all existing business data from Redis into the new PostgreSQL tables.
Safe to run multiple times (idempotent upsert via ON CONFLICT DO NOTHING).

Migration order (respects FK constraints):
  1. agents      (no FK)
  2. subnets     (no FK)
  3. tasks       (no FK)
  4. participations  (FK → tasks)
  5. activities  (no FK, but references task/agent IDs)

Usage:
    REDIS_URL=redis://... DATABASE_URL=postgresql+asyncpg://... python scripts/migrate_redis_to_pg.py
"""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime

# Make sure the acn package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import redis.asyncio as aioredis
import structlog
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from acn.infrastructure.persistence.postgres.database import get_engine, get_session_factory
from acn.infrastructure.persistence.postgres.models import (
    ActivityModel,
    AgentModel,
    ParticipationModel,
    SubnetModel,
    TaskModel,
)

logger = structlog.get_logger()

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

if not DATABASE_URL:
    print("ERROR: DATABASE_URL env var is required", file=sys.stderr)
    sys.exit(1)


# =============================================================================
# Redis helpers
# =============================================================================


def _bytes(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def _parse_json(v: str | None, default=None):
    if not v:
        return default
    try:
        return json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return default


def _parse_bool(v: str | None) -> bool:
    return str(v).lower() in ("true", "1", "yes") if v else False


# =============================================================================
# Agent migration
# =============================================================================


async def migrate_agents(redis: aioredis.Redis, session_factory) -> int:
    """Migrate all acn:agents:* hashes → agents table."""
    keys = await redis.keys("acn:agents:*")
    # Only real agent records (not alive/index keys)
    agent_keys = [
        k for k in keys
        if not any(s in _bytes(k) for s in [":alive", ":by_", ":unclaimed"])
        and _bytes(k).count(":") == 2  # acn:agents:{uuid}
    ]

    count = 0
    async with session_factory() as session:
        for key in agent_keys:
            raw = await redis.hgetall(key)
            if not raw:
                continue
            d = {_bytes(k): _bytes(v) for k, v in raw.items()}
            agent_id = d.get("agent_id")
            if not agent_id:
                continue

            reg_at = _parse_dt(d.get("registered_at")) or datetime.now(UTC)
            stmt = (
                pg_insert(AgentModel)
                .values(
                    agent_id=agent_id,
                    name=d.get("name", ""),
                    owner=d.get("owner") or None,
                    endpoint=d.get("endpoint") or None,
                    status=d.get("status", "offline"),
                    description=d.get("description") or None,
                    skills=_parse_json(d.get("skills"), []) or None,
                    subnet_ids=_parse_json(d.get("subnet_ids"), ["public"]) or None,
                    api_key=d.get("api_key") or None,
                    auth0_client_id=d.get("auth0_client_id") or None,
                    auth0_token_endpoint=d.get("auth0_token_endpoint") or None,
                    claim_status=d.get("claim_status") or None,
                    verification_code=d.get("verification_code") or None,
                    referrer_id=d.get("referrer_id") or None,
                    wallet_address=d.get("wallet_address") or None,
                    accepts_payment=_parse_bool(d.get("accepts_payment")),
                    payment_methods=_parse_json(d.get("payment_methods"), []) or None,
                    agent_card=_parse_json(d.get("agent_card")) or None,
                    agent_metadata={
                        "description": d.get("description"),
                        "token_pricing": _parse_json(d.get("token_pricing")),
                        "extra_metadata": _parse_json(d.get("metadata"), {}),
                        "erc8004_agent_id": d.get("erc8004_agent_id"),
                        "erc8004_chain": d.get("erc8004_chain"),
                        "erc8004_tx_hash": d.get("erc8004_tx_hash"),
                        "erc8004_registered_at": d.get("erc8004_registered_at"),
                    },
                    registered_at=reg_at,
                    last_heartbeat=_parse_dt(d.get("last_heartbeat")),
                    owner_changed_at=_parse_dt(d.get("owner_changed_at")),
                )
                .on_conflict_do_nothing(index_elements=["agent_id"])
            )
            await session.execute(stmt)
            count += 1

        await session.commit()

    logger.info("migrate_agents_done", count=count)
    return count


# =============================================================================
# Subnet migration
# =============================================================================


async def migrate_subnets(redis: aioredis.Redis, session_factory) -> int:
    keys = await redis.keys("acn:subnets:*")
    subnet_keys = [
        k for k in keys
        if ":agents" not in _bytes(k) and _bytes(k).count(":") == 2
    ]

    count = 0
    async with session_factory() as session:
        for key in subnet_keys:
            raw = await redis.hgetall(key)
            if not raw:
                continue
            d = {_bytes(k): _bytes(v) for k, v in raw.items()}
            subnet_id = d.get("subnet_id")
            if not subnet_id:
                continue

            created = _parse_dt(d.get("created_at")) or datetime.now(UTC)
            member_ids = []
            members_raw = await redis.smembers(f"acn:subnets:{subnet_id}:agents")
            if members_raw:
                member_ids = [_bytes(m) for m in members_raw]

            stmt = (
                pg_insert(SubnetModel)
                .values(
                    subnet_id=subnet_id,
                    name=d.get("name", ""),
                    owner=d.get("owner", "system"),
                    description=d.get("description") or None,
                    is_private=_parse_bool(d.get("is_private")),
                    security_config=_parse_json(d.get("security_config")) or None,
                    member_agent_ids=member_ids or None,
                    subnet_metadata=_parse_json(d.get("metadata"), {}) or None,
                    created_at=created,
                )
                .on_conflict_do_nothing(index_elements=["subnet_id"])
            )
            await session.execute(stmt)
            count += 1

        await session.commit()

    logger.info("migrate_subnets_done", count=count)
    return count


# =============================================================================
# Task migration
# =============================================================================


async def migrate_tasks(redis: aioredis.Redis, session_factory) -> int:
    # Collect all task hashes — pattern acn:task:{uuid} (exactly 3 segments)
    keys = await redis.keys("acn:task:*")
    task_keys = [
        k for k in keys
        if _bytes(k).count(":") == 2
        and "completions" not in _bytes(k)
        and "active_count" not in _bytes(k)
        and "participations" not in _bytes(k)
    ]

    count = 0
    async with session_factory() as session:
        for key in task_keys:
            raw = await redis.hgetall(key)
            if not raw:
                continue
            d = {_bytes(k): _bytes(v) for k, v in raw.items()}
            task_id = d.get("task_id")
            if not task_id:
                continue

            created = _parse_dt(d.get("created_at")) or datetime.now(UTC)
            skills = _parse_json(d.get("required_skills"), [])

            stmt = (
                pg_insert(TaskModel)
                .values(
                    task_id=task_id,
                    mode=d.get("mode", "open"),
                    status=d.get("status", "open"),
                    creator_id=d.get("creator_id", ""),
                    creator_type=d.get("creator_type", "human"),
                    title=d.get("title", ""),
                    description=d.get("description") or None,
                    reward_amount=d.get("reward_amount", "0"),
                    reward_currency=d.get("reward_currency", "points"),
                    assignee_id=d.get("assignee_id") or None,
                    is_multi_participant=_parse_bool(d.get("is_multi_participant")),
                    max_completions=int(d["max_completions"]) if d.get("max_completions") else None,
                    completed_count=int(d.get("completed_count", 0)),
                    required_skills=skills or None,
                    created_at=created,
                    deadline=_parse_dt(d.get("deadline")),
                    task_metadata={
                        "creator_name": d.get("creator_name", ""),
                        "task_type": d.get("task_type", "general"),
                        "submission": d.get("submission"),
                        "submission_artifacts": _parse_json(d.get("submission_artifacts"), []),
                        "submitted_at": d.get("submitted_at"),
                        "review_notes": d.get("review_notes"),
                        "reviewed_by": d.get("reviewed_by"),
                        "payment_task_id": d.get("payment_task_id"),
                        "reward_unit": d.get("reward_unit", "completion"),
                        "total_budget": d.get("total_budget", "0"),
                        "released_amount": d.get("released_amount", "0"),
                        "allow_repeat_by_same": _parse_bool(d.get("allow_repeat_by_same")),
                        "assignee_name": d.get("assignee_name"),
                        "assigned_at": d.get("assigned_at"),
                        "completed_at": d.get("completed_at"),
                        "approval_type": d.get("approval_type", "manual"),
                        "validator_id": d.get("validator_id"),
                        "extra_metadata": _parse_json(d.get("metadata"), {}),
                    },
                )
                .on_conflict_do_nothing(index_elements=["task_id"])
            )
            await session.execute(stmt)
            count += 1

        await session.commit()

    logger.info("migrate_tasks_done", count=count)
    return count


# =============================================================================
# Participation migration
# =============================================================================


async def migrate_participations(redis: aioredis.Redis, session_factory) -> int:
    keys = await redis.keys("acn:participation:*")

    count = 0
    async with session_factory() as session:
        for key in keys:
            raw = await redis.hgetall(key)
            if not raw:
                continue
            d = {_bytes(k): _bytes(v) for k, v in raw.items()}
            pid = d.get("participation_id")
            if not pid:
                continue

            joined = _parse_dt(d.get("joined_at")) or datetime.now(UTC)
            task_id_val = d.get("task_id", "")
            # Skip participations whose parent task wasn't migrated (FK would fail)
            if not task_id_val:
                logger.warning("participation_missing_task_id", participation_id=pid)
                continue

            stmt = (
                pg_insert(ParticipationModel)
                .values(
                    participation_id=pid,
                    task_id=task_id_val,
                    participant_id=d.get("participant_id", ""),
                    participant_name=d.get("participant_name", ""),
                    participant_type=d.get("participant_type", "agent"),
                    status=d.get("status", "active"),
                    joined_at=joined,
                    submission=d.get("submission") or None,
                    submission_artifacts=_parse_json(d.get("submission_artifacts"), []) or None,
                    submitted_at=_parse_dt(d.get("submitted_at")),
                    rejection_reason=d.get("rejection_reason") or None,
                    rejected_at=_parse_dt(d.get("rejected_at")),
                    reject_response_deadline=_parse_dt(d.get("reject_response_deadline")),
                    review_request_id=d.get("review_request_id") or None,
                    review_notes=d.get("review_notes") or None,
                    reviewed_by=d.get("reviewed_by") or None,
                    cancelled_at=_parse_dt(d.get("cancelled_at")),
                    completed_at=_parse_dt(d.get("completed_at")),
                )
                .on_conflict_do_nothing(index_elements=["participation_id"])
            )
            await session.execute(stmt)
            count += 1

        await session.commit()

    logger.info("migrate_participations_done", count=count)
    return count


# =============================================================================
# Activity migration
# =============================================================================


async def migrate_activities(redis: aioredis.Redis, session_factory) -> int:
    keys = await redis.keys("labs_activity:*")

    count = 0
    async with session_factory() as session:
        for key in keys:
            raw = await redis.hgetall(key)
            if not raw:
                continue
            d = {_bytes(k): _bytes(v) for k, v in raw.items()}
            event_id = d.get("event_id")
            if not event_id:
                continue

            ts = _parse_dt(d.get("timestamp")) or datetime.now(UTC)
            points_raw = d.get("points")
            points = int(points_raw) if points_raw else None

            stmt = (
                pg_insert(ActivityModel)
                .values(
                    event_id=event_id,
                    type=d.get("type", ""),
                    actor_type=d.get("actor_type", "human"),
                    actor_id=d.get("actor_id", ""),
                    actor_name=d.get("actor_name", ""),
                    description=d.get("description", ""),
                    points=points,
                    task_id=d.get("task_id") or None,
                    event_metadata=None,
                    timestamp=ts,
                )
                .on_conflict_do_nothing(index_elements=["event_id"])
            )
            await session.execute(stmt)
            count += 1

        await session.commit()

    logger.info("migrate_activities_done", count=count)
    return count


# =============================================================================
# Verification
# =============================================================================


async def verify(session_factory) -> None:
    async with session_factory() as session:
        for table in ["agents", "subnets", "tasks", "participations", "activities"]:
            result = await session.execute(text(f"SELECT COUNT(*) FROM {table}"))
            n = result.scalar()
            print(f"  {table:20s}: {n} rows")


# =============================================================================
# Main
# =============================================================================


async def main():
    print(f"Connecting to Redis: {REDIS_URL[:30]}...")
    redis = aioredis.from_url(REDIS_URL, decode_responses=False)

    print(f"Connecting to PostgreSQL: {DATABASE_URL[:40]}...")
    engine = get_engine(DATABASE_URL)
    session_factory = get_session_factory(engine)

    print("\nStarting migration...\n")

    agents = await migrate_agents(redis, session_factory)
    subnets = await migrate_subnets(redis, session_factory)
    tasks = await migrate_tasks(redis, session_factory)
    participations = await migrate_participations(redis, session_factory)
    activities = await migrate_activities(redis, session_factory)

    print("\nMigration complete:")
    print(f"  agents:          {agents}")
    print(f"  subnets:         {subnets}")
    print(f"  tasks:           {tasks}")
    print(f"  participations:  {participations}")
    print(f"  activities:      {activities}")

    print("\nVerifying row counts in PostgreSQL:")
    await verify(session_factory)

    await redis.aclose()
    await engine.dispose()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
