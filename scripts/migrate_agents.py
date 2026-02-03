#!/usr/bin/env python3
"""
Agent Data Migration Script

Migrates agents from old Labs onboarding format (onboarded_agent:*)
to unified ACN format (acn:agents:*).

Old format (onboarding.py):
- onboarded_agent:{agent_id} - Hash with: agent_id, name, description, skills (comma-separated),
  mode, endpoint, source, referrer, status, verification_code, created_at, last_heartbeat,
  claimed_by, claimed_at
- onboarded_api_key:{api_key} - String → agent_id
- onboarded_points:{agent_id} - String (points value)

New format (unified Agent entity):
- acn:agents:{agent_id} - Hash with all Agent entity fields
- acn:agents:by_api_key:{api_key} - String → agent_id
- acn:agents:by_owner:{owner} - Set of agent_ids
- acn:agents:unclaimed - Set of agent_ids

Usage:
    cd acn
    uv run python scripts/migrate_agents.py [--dry-run] [--delete-old]
"""

import argparse
import asyncio
import json
from datetime import datetime

import redis.asyncio as redis

# Old key prefixes
OLD_AGENT_PREFIX = "onboarded_agent:"
OLD_API_KEY_PREFIX = "onboarded_api_key:"
OLD_POINTS_PREFIX = "onboarded_points:"

# New key prefixes
NEW_AGENT_PREFIX = "acn:agents:"
NEW_API_KEY_INDEX = "acn:agents:by_api_key:"
NEW_OWNER_INDEX = "acn:agents:by_owner:"
NEW_UNCLAIMED_SET = "acn:agents:unclaimed"
NEW_SUBNET_AGENTS = "acn:subnets:public:agents"


def convert_old_to_new(old_data: dict, api_key: str | None = None) -> dict:
    """Convert old agent data format to new unified format"""

    # Parse skills from comma-separated string
    skills_str = old_data.get("skills", "")
    skills = [s.strip() for s in skills_str.split(",") if s.strip()] if skills_str else []

    # Determine claim status
    claimed_by = old_data.get("claimed_by", "")
    if claimed_by:
        claim_status = "claimed"
        owner = claimed_by
    else:
        claim_status = "unclaimed"
        owner = None

    # Parse dates
    created_at = old_data.get("created_at") or old_data.get("joined_at")
    if not created_at:
        created_at = datetime.now().isoformat()

    last_heartbeat = old_data.get("last_heartbeat")

    owner_changed_at = old_data.get("claimed_at")

    # Build new agent data
    new_data = {
        "agent_id": old_data["agent_id"],
        "name": old_data.get("name", "Unknown Agent"),
        "status": "online",  # Default to online
        "description": old_data.get("description", ""),
        "skills": json.dumps(skills),
        "subnet_ids": json.dumps(["public"]),
        "metadata": json.dumps(
            {
                "source": old_data.get("source", "unknown"),
                "mode": old_data.get("mode", "pull"),
                "migrated_from": "onboarded_agent",
                "migrated_at": datetime.now().isoformat(),
            }
        ),
        "registered_at": created_at,
        "payment_methods": json.dumps([]),
        "accepts_payment": "false",
    }

    # Add optional fields (skip None values for Redis)
    if owner:
        new_data["owner"] = owner

    endpoint = old_data.get("endpoint", "")
    if endpoint:
        new_data["endpoint"] = endpoint

    if api_key:
        new_data["api_key"] = api_key

    new_data["claim_status"] = claim_status

    verification_code = old_data.get("verification_code", "")
    if verification_code:
        new_data["verification_code"] = verification_code

    referrer = old_data.get("referrer", "")
    if referrer:
        new_data["referrer_id"] = referrer

    if last_heartbeat:
        new_data["last_heartbeat"] = last_heartbeat

    if owner_changed_at:
        new_data["owner_changed_at"] = owner_changed_at

    return new_data


async def migrate_agents(
    redis_url: str = "redis://localhost:6379",
    dry_run: bool = True,
    delete_old: bool = False,
):
    """Run the migration"""

    print(f"Connecting to Redis: {redis_url}")
    r = redis.from_url(redis_url)

    try:
        # 1. Build API key → agent_id mapping first
        print("\n[1/4] Building API key mapping...")
        api_key_map = {}  # agent_id → api_key

        cursor = 0
        while True:
            cursor, keys = await r.scan(cursor, match=f"{OLD_API_KEY_PREFIX}*", count=100)
            for key in keys:
                key_str = key.decode() if isinstance(key, bytes) else key
                api_key = key_str.replace(OLD_API_KEY_PREFIX, "")
                agent_id = await r.get(key_str)
                if agent_id:
                    agent_id = agent_id.decode() if isinstance(agent_id, bytes) else agent_id
                    api_key_map[agent_id] = api_key

            if cursor == 0:
                break

        print(f"   Found {len(api_key_map)} API keys")

        # 2. Find and migrate all old agents
        print("\n[2/4] Finding old agents...")
        migrated = 0
        skipped = 0
        errors = 0

        cursor = 0
        while True:
            cursor, keys = await r.scan(cursor, match=f"{OLD_AGENT_PREFIX}*", count=100)

            for key in keys:
                key_str = key.decode() if isinstance(key, bytes) else key
                agent_id = key_str.replace(OLD_AGENT_PREFIX, "")

                try:
                    # Get old data
                    old_data = await r.hgetall(key_str)
                    if not old_data:
                        continue

                    # Decode bytes
                    old_data = {
                        k.decode() if isinstance(k, bytes) else k: v.decode()
                        if isinstance(v, bytes)
                        else v
                        for k, v in old_data.items()
                    }

                    # Check if already migrated
                    new_key = f"{NEW_AGENT_PREFIX}{agent_id}"
                    exists = await r.exists(new_key)
                    if exists:
                        print(f"   SKIP: {agent_id} (already exists in new format)")
                        skipped += 1
                        continue

                    # Get API key for this agent
                    api_key = api_key_map.get(agent_id)

                    # Convert to new format
                    new_data = convert_old_to_new(old_data, api_key)

                    print(f"   MIGRATE: {agent_id}")
                    print(f"      Name: {new_data['name']}")
                    print(f"      Owner: {new_data.get('owner', 'None')}")
                    print(f"      Claim: {new_data['claim_status']}")

                    if not dry_run:
                        # Save new agent
                        await r.hset(new_key, mapping=new_data)

                        # Create API key index
                        if api_key:
                            await r.set(f"{NEW_API_KEY_INDEX}{api_key}", agent_id)

                        # Create owner index
                        owner = new_data.get("owner")
                        if owner:
                            await r.sadd(f"{NEW_OWNER_INDEX}{owner}", agent_id)

                        # Add to unclaimed set if unclaimed
                        if new_data["claim_status"] == "unclaimed":
                            await r.sadd(NEW_UNCLAIMED_SET, agent_id)

                        # Add to subnet
                        await r.sadd(NEW_SUBNET_AGENTS, agent_id)

                    migrated += 1

                except Exception as e:
                    print(f"   ERROR: {agent_id} - {e}")
                    errors += 1

            if cursor == 0:
                break

        # 3. Summary
        print("\n[3/4] Migration Summary:")
        print(f"   Migrated: {migrated}")
        print(f"   Skipped:  {skipped}")
        print(f"   Errors:   {errors}")

        if dry_run:
            print("\n   ⚠️  DRY RUN - No changes made")
            print("   Run with --no-dry-run to apply changes")

        # 4. Optionally delete old data
        if delete_old and not dry_run and migrated > 0:
            print("\n[4/4] Deleting old data...")

            # Delete old agent keys
            cursor = 0
            deleted = 0
            while True:
                cursor, keys = await r.scan(cursor, match=f"{OLD_AGENT_PREFIX}*", count=100)
                for key in keys:
                    await r.delete(key)
                    deleted += 1
                if cursor == 0:
                    break

            # Delete old API key keys
            cursor = 0
            while True:
                cursor, keys = await r.scan(cursor, match=f"{OLD_API_KEY_PREFIX}*", count=100)
                for key in keys:
                    await r.delete(key)
                    deleted += 1
                if cursor == 0:
                    break

            print(f"   Deleted {deleted} old keys")
        elif delete_old:
            print("\n[4/4] Skipping deletion (dry run or no migrations)")
        else:
            print("\n[4/4] Old data preserved (use --delete-old to remove)")

        print("\n✅ Migration complete!")

    finally:
        await r.close()


def main():
    parser = argparse.ArgumentParser(description="Migrate agents to unified format")
    parser.add_argument(
        "--redis-url",
        default="redis://localhost:6379",
        help="Redis connection URL",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Dry run (don't write changes)",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Actually perform the migration",
    )
    parser.add_argument(
        "--delete-old",
        action="store_true",
        help="Delete old data after migration",
    )

    args = parser.parse_args()

    dry_run = not args.no_dry_run

    print("=" * 60)
    print("Agent Migration: onboarded_agent → acn:agents")
    print("=" * 60)

    asyncio.run(
        migrate_agents(
            redis_url=args.redis_url,
            dry_run=dry_run,
            delete_old=args.delete_old,
        )
    )


if __name__ == "__main__":
    main()
