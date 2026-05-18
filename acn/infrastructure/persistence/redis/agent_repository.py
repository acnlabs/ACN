"""Redis Implementation of Agent Repository

Concrete implementation using Redis for agent persistence.
"""

import json
import re
from datetime import datetime

import redis.asyncio as redis  # type: ignore[import-untyped]

from ....core.entities import Agent, AgentStatus, ClaimStatus
from ....core.interfaces import IAgentRepository


class RedisAgentRepository(IAgentRepository):
    """
    Redis-based Agent Repository

    Implements IAgentRepository using Redis as storage backend.

    Index Keys:
    - acn:agents:{agent_id}        → Agent hash (permanent)
    - acn:agents:{agent_id}:alive  → Alive signal key with TTL (ephemeral)
    - acn:agents:by_endpoint:{owner}:{endpoint} → agent_id
    - acn:agents:by_api_key:{sha256(api_key)} → agent_id
    - acn:agents:by_owner:{owner}  → Set of agent_ids
    - acn:agents:unclaimed         → Set of agent_ids
    - acn:subnets:{subnet_id}:agents → Set of agent_ids
    """

    def __init__(self, redis_client: redis.Redis):
        """
        Initialize Redis Agent Repository

        Args:
            redis_client: Redis async client instance
        """
        self.redis = redis_client

    async def save(self, agent: Agent) -> None:
        """Save or update an agent in Redis"""
        agent_key = f"acn:agents:{agent.agent_id}"

        # Check for existing agent to clean up old indices
        existing = await self.find_by_id(agent.agent_id)

        # Serialize agent to dict
        agent_dict = agent.to_dict()

        # Convert lists/dicts to JSON strings for Redis
        agent_dict["tags"] = json.dumps(agent_dict.get("tags", []))
        agent_dict["subnet_ids"] = json.dumps(agent_dict.get("subnet_ids", ["public"]))
        agent_dict["payment_methods"] = json.dumps(agent_dict.get("payment_methods", []))
        agent_dict["wallet_addresses"] = json.dumps(agent_dict.get("wallet_addresses", {}))
        agent_dict["metadata"] = json.dumps(agent_dict.get("metadata", {}))
        if agent_dict.get("token_pricing"):
            agent_dict["token_pricing"] = json.dumps(agent_dict["token_pricing"])
        if agent_dict.get("agent_card"):
            agent_dict["agent_card"] = json.dumps(agent_dict["agent_card"])
        # communication_policy is materialized to {"mode": "open"} in
        # Agent.__post_init__ even when callers pass None, so it should
        # always serialize. Use json.dumps so the gateway can round-trip
        # nested fields (allowlist, rate_limit, ...) introduced in later phases.
        if agent_dict.get("communication_policy") is not None:
            agent_dict["communication_policy"] = json.dumps(
                agent_dict["communication_policy"]
            )

        # Filter out None values (Redis doesn't accept None)
        # Also convert booleans to strings for Redis compatibility
        # Track which keys were explicitly set to None — these must be deleted from the hash
        nullable_fields = {
            "verification_code",
            "owner",
            "endpoint",
            "a2a_endpoint",
            "referrer_id",
            "agent_card_url",
            "social_card_url",
        }
        clean_dict = {}
        fields_to_delete = []
        for k, v in agent_dict.items():
            if v is None:
                if k in nullable_fields:
                    fields_to_delete.append(k)
                # else: skip (never set, no need to delete)
            elif isinstance(v, bool):
                clean_dict[k] = "true" if v else "false"
            else:
                clean_dict[k] = v

        # Save to Redis hash
        await self.redis.hset(agent_key, mapping=clean_dict)  # type: ignore[arg-type]

        # Explicitly delete fields that were cleared (set to None) to avoid stale cache
        if fields_to_delete:
            await self.redis.hdel(agent_key, *fields_to_delete)

        # ===== Update Indices =====

        # 1. Endpoint index (only for agents with owner and endpoint)
        if agent.owner and agent.endpoint:
            endpoint_key = f"acn:agents:by_endpoint:{agent.owner}:{agent.endpoint}"
            await self.redis.set(endpoint_key, agent.agent_id)

        # Clean up old endpoint index if owner changed
        if existing and existing.owner and existing.endpoint:
            if existing.owner != agent.owner or existing.endpoint != agent.endpoint:
                old_endpoint_key = f"acn:agents:by_endpoint:{existing.owner}:{existing.endpoint}"
                await self.redis.delete(old_endpoint_key)

        # 2. API key index (for autonomous agents)
        if agent.api_key:
            api_key_index = f"acn:agents:by_api_key:{agent.api_key}"
            await self.redis.set(api_key_index, agent.agent_id)

        # Clean up the previous API-key index if the key changed (H1
        # rotation). Without this the stale index entry would keep
        # pointing at this agent_id, and the rotated-away key would
        # still authenticate via find_by_api_key — exactly the leak
        # the rotation was meant to plug.
        if existing and existing.api_key and existing.api_key != agent.api_key:
            stale_api_key_index = f"acn:agents:by_api_key:{existing.api_key}"
            await self.redis.delete(stale_api_key_index)

        # 3. Owner index
        if agent.owner:
            await self.redis.sadd(f"acn:agents:by_owner:{agent.owner}", agent.agent_id)

        # Clean up old owner index if owner changed
        if existing and existing.owner and existing.owner != agent.owner:
            await self.redis.srem(f"acn:agents:by_owner:{existing.owner}", agent.agent_id)

        # 4. Unclaimed index
        if agent.claim_status == ClaimStatus.UNCLAIMED:
            await self.redis.sadd("acn:agents:unclaimed", agent.agent_id)
        else:
            # Remove from unclaimed if claimed
            await self.redis.srem("acn:agents:unclaimed", agent.agent_id)

        # 5. ERC-8004 token_id reverse index (for duplicate-bind prevention)
        if agent.erc8004_agent_id:
            await self.redis.set(
                f"acn:agents:by_erc8004_id:{agent.erc8004_agent_id}", agent.agent_id
            )

        # 6. Subnet indices
        for subnet_id in agent.subnet_ids:
            await self.redis.sadd(f"acn:subnets:{subnet_id}:agents", agent.agent_id)

        # Clean up old subnet indices
        if existing:
            for old_subnet in existing.subnet_ids:
                if old_subnet not in agent.subnet_ids:
                    await self.redis.srem(f"acn:subnets:{old_subnet}:agents", agent.agent_id)

    async def find_by_id(self, agent_id: str) -> Agent | None:
        """Find agent by ID"""
        agent_key = f"acn:agents:{agent_id}"
        agent_dict = await self.redis.hgetall(agent_key)

        if not agent_dict:
            return None

        return self._dict_to_agent(agent_dict)

    async def find_by_owner_and_endpoint(self, owner: str, endpoint: str) -> Agent | None:
        """Find agent by owner and endpoint"""
        endpoint_key = f"acn:agents:by_endpoint:{owner}:{endpoint}"
        agent_id = await self.redis.get(endpoint_key)

        if not agent_id:
            return None

        return await self.find_by_id(agent_id)

    # Only keys acn:agents:{uuid} are agent hashes; others are indexes or sets
    _AGENT_KEY_RE = re.compile(r"^acn:agents:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

    async def find_all(self) -> list[Agent]:
        """Find all agents by scanning agent hash keys (acn:agents:{uuid})."""
        agents = []
        async for key in self.redis.scan_iter("acn:agents:*"):
            # Skip index/set keys: only process agent hash keys acn:agents:{uuid}
            if not self._AGENT_KEY_RE.match(key):
                continue
            try:
                agent_dict = await self.redis.hgetall(key)
            except redis.ResponseError:
                # Wrong key type (e.g. SET acn:agents:all)
                continue
            if agent_dict:
                agents.append(self._dict_to_agent(agent_dict))
        return agents

    async def find_by_subnet(self, subnet_id: str) -> list[Agent]:
        """Find all agents in a subnet"""
        agent_ids = await self.redis.smembers(f"acn:subnets:{subnet_id}:agents")
        agents = []
        for agent_id in agent_ids:
            agent = await self.find_by_id(agent_id)
            if agent:
                agents.append(agent)
        return agents

    async def find_by_tags(self, tags: list[str], status: str = "all") -> list[Agent]:
        """Find agents with ALL of *tags*.

        The ``status`` parameter is kept for ABI compatibility but is
        intentionally ignored: "online" is now defined as
        "Redis alive key present" and is applied uniformly at the service
        layer (``AgentService._filter_by_status``). Pre-filtering here
        based on the legacy DB column would drop agents that are alive
        in Redis but stale in DB — the exact drift this refactor
        eliminates. See ``AgentService.search_agents`` for the read-time
        contract.
        """
        del status  # see docstring — deliberately unused
        all_agents = await self.find_all()
        return [a for a in all_agents if a.has_all_tags(tags)]

    async def find_by_owner(self, owner: str) -> list[Agent]:
        """Find all agents owned by a user"""
        agent_ids = await self.redis.smembers(f"acn:agents:by_owner:{owner}")
        agents = []
        for agent_id in agent_ids:
            agent = await self.find_by_id(agent_id)
            if agent:
                agents.append(agent)
        return agents

    async def delete(self, agent_id: str) -> bool:
        """Delete an agent"""
        agent = await self.find_by_id(agent_id)
        if not agent:
            return False

        # Remove from Redis
        agent_key = f"acn:agents:{agent_id}"
        await self.redis.delete(agent_key)

        # Remove from endpoint index
        if agent.owner and agent.endpoint:
            endpoint_key = f"acn:agents:by_endpoint:{agent.owner}:{agent.endpoint}"
            await self.redis.delete(endpoint_key)

        # Remove from API key index
        if agent.api_key:
            api_key_index = f"acn:agents:by_api_key:{agent.api_key}"
            await self.redis.delete(api_key_index)

        # Remove from subnet indices
        for subnet_id in agent.subnet_ids:
            await self.redis.srem(f"acn:subnets:{subnet_id}:agents", agent_id)

        # Remove from owner index
        if agent.owner:
            await self.redis.srem(f"acn:agents:by_owner:{agent.owner}", agent_id)

        # Remove from unclaimed index
        await self.redis.srem("acn:agents:unclaimed", agent_id)

        # Remove ERC-8004 reverse index. Without this, re-binding the same
        # on-chain token_id to a replacement agent stays permanently
        # blocked by the duplicate-bind check on save().
        if agent.erc8004_agent_id:
            await self.redis.delete(
                f"acn:agents:by_erc8004_id:{agent.erc8004_agent_id}"
            )

        # Clear the alive signal immediately rather than waiting for the
        # TTL to expire, so ``filter_alive()`` cannot resurrect a deleted
        # agent's online status in the meantime.
        await self.redis.delete(f"acn:agents:{agent_id}:alive")

        # Remove offline inbox so deleted agents don't occupy Redis memory
        await self.redis.delete(f"acn:inbox:{agent_id}")

        return True

    async def exists(self, agent_id: str) -> bool:
        """Check if agent exists"""
        return await self.redis.exists(f"acn:agents:{agent_id}") > 0

    async def count_by_subnet(self, subnet_id: str) -> int:
        """Count agents in a subnet"""
        return await self.redis.scard(f"acn:subnets:{subnet_id}:agents")

    async def find_by_api_key(self, key_hash: str) -> Agent | None:
        """Find agent by SHA-256 hash of their API key."""
        agent_id = await self.redis.get(f"acn:agents:by_api_key:{key_hash}")
        if not agent_id:
            return None
        return await self.find_by_id(agent_id)

    async def find_unclaimed(self, limit: int = 100) -> list[Agent]:
        """Find all unclaimed agents"""
        agent_ids = await self.redis.smembers("acn:agents:unclaimed")
        agents = []
        count = 0

        for agent_id in agent_ids:
            if count >= limit:
                break
            agent = await self.find_by_id(agent_id)
            if agent and agent.claim_status == ClaimStatus.UNCLAIMED:
                agents.append(agent)
                count += 1

        return agents

    async def set_alive(self, agent_id: str, ttl: int) -> None:
        """Set or renew the alive signal key for an agent."""
        await self.redis.set(f"acn:agents:{agent_id}:alive", "1", ex=ttl)

    async def filter_alive(self, agent_ids: list[str]) -> set[str]:
        """Return subset of agent_ids whose alive key exists (PIPELINE)."""
        if not agent_ids:
            return set()
        pipe = self.redis.pipeline()
        for agent_id in agent_ids:
            pipe.exists(f"acn:agents:{agent_id}:alive")
        results = await pipe.execute()
        return {aid for aid, alive in zip(agent_ids, results, strict=True) if alive}

    # ``mark_offline_stale`` deliberately removed — see
    # ``PostgresAgentRepository`` for the rationale. Redis ``alive`` TTL
    # is the single source of truth for online-ness; column reconciliation
    # is no longer needed.

    def _dict_to_agent(self, agent_dict: dict) -> Agent:
        """Convert Redis dict to Agent entity"""
        # Parse JSON fields
        data = {
            "agent_id": agent_dict["agent_id"],
            "name": agent_dict["name"],
            # owner is now optional
            "owner": agent_dict.get("owner"),
            # endpoint is now optional
            "endpoint": agent_dict.get("endpoint"),
            "a2a_endpoint": agent_dict.get("a2a_endpoint") or agent_dict.get("endpoint"),
            "status": AgentStatus(agent_dict["status"]),
            "description": agent_dict.get("description"),
            # Read: support both new "tags" and legacy "skills" key
            "tags": json.loads(agent_dict.get("tags") or agent_dict.get("skills", "[]")),
            "subnet_ids": json.loads(agent_dict.get("subnet_ids", '["public"]')),
            "metadata": json.loads(agent_dict.get("metadata", "{}")),
            "registered_at": datetime.fromisoformat(agent_dict["registered_at"]),
            "last_heartbeat": (
                datetime.fromisoformat(agent_dict["last_heartbeat"])
                if agent_dict.get("last_heartbeat")
                else None
            ),
            # Authentication
            "api_key": agent_dict.get("api_key"),
            # Claim status
            "claim_status": (
                ClaimStatus(agent_dict["claim_status"]) if agent_dict.get("claim_status") else None
            ),
            "verification_code": agent_dict.get("verification_code"),
            # Referral
            "referrer_id": agent_dict.get("referrer_id"),
            # Owner change tracking
            "owner_changed_at": (
                datetime.fromisoformat(agent_dict["owner_changed_at"])
                if agent_dict.get("owner_changed_at")
                else None
            ),
            # Payment
            "wallet_address": agent_dict.get("wallet_address"),
            "wallet_addresses": json.loads(agent_dict.get("wallet_addresses", "{}")),
            "accepts_payment": agent_dict.get("accepts_payment", "false").lower() == "true",
            "payment_methods": json.loads(agent_dict.get("payment_methods", "[]")),
            "token_pricing": (
                json.loads(agent_dict["token_pricing"]) if agent_dict.get("token_pricing") else None
            ),
            "agent_card": (
                json.loads(agent_dict["agent_card"]) if agent_dict.get("agent_card") else None
            ),
            "agent_card_url": agent_dict.get("agent_card_url") or None,
            # Communication policy — older rows predate this field, so fall
            # back to None and let Agent.__post_init__ default it to open.
            "communication_policy": (
                json.loads(agent_dict["communication_policy"])
                if agent_dict.get("communication_policy")
                else None
            ),
            # Auth0 M2M 凭证（client_secret 不持久化）
            "auth0_client_id": agent_dict.get("auth0_client_id"),
            "auth0_token_endpoint": agent_dict.get("auth0_token_endpoint"),
            # [REMOVED] Agent Wallet fields - 由 Backend 管理
            # ERC-8004 On-Chain Identity
            "erc8004_agent_id": agent_dict.get("erc8004_agent_id"),
            "erc8004_chain": agent_dict.get("erc8004_chain"),
            "erc8004_tx_hash": agent_dict.get("erc8004_tx_hash"),
            "erc8004_registered_at": (
                datetime.fromisoformat(agent_dict["erc8004_registered_at"])
                if agent_dict.get("erc8004_registered_at")
                else None
            ),
            # SOCIAL.md pointer (older rows predate this field).
            "social_card_url": agent_dict.get("social_card_url") or None,
        }

        return Agent(**data)
