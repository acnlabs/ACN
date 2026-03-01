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
    - acn:agents:by_api_key:{api_key} → agent_id
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
        agent_dict["skills"] = json.dumps(agent_dict.get("skills", []))
        agent_dict["subnet_ids"] = json.dumps(agent_dict.get("subnet_ids", ["public"]))
        agent_dict["payment_methods"] = json.dumps(agent_dict.get("payment_methods", []))
        agent_dict["wallet_addresses"] = json.dumps(agent_dict.get("wallet_addresses", {}))
        agent_dict["metadata"] = json.dumps(agent_dict.get("metadata", {}))
        if agent_dict.get("token_pricing"):
            agent_dict["token_pricing"] = json.dumps(agent_dict["token_pricing"])
        if agent_dict.get("agent_card"):
            agent_dict["agent_card"] = json.dumps(agent_dict["agent_card"])

        # Filter out None values (Redis doesn't accept None)
        # Also convert booleans to strings for Redis compatibility
        clean_dict = {}
        for k, v in agent_dict.items():
            if v is None:
                continue  # Skip None values
            elif isinstance(v, bool):
                clean_dict[k] = "true" if v else "false"
            else:
                clean_dict[k] = v

        # Save to Redis hash
        await self.redis.hset(agent_key, mapping=clean_dict)  # type: ignore[arg-type]

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

    async def find_by_skills(self, skills: list[str], status: str = "online") -> list[Agent]:
        """Find agents by skills. status='all' returns agents with skills regardless of status."""
        all_agents = await self.find_all()

        matching_agents = []
        for agent in all_agents:
            if not agent.has_all_skills(skills):
                continue
            if status != "all" and agent.status.value != status:
                continue
            matching_agents.append(agent)
        return matching_agents

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

        return True

    async def exists(self, agent_id: str) -> bool:
        """Check if agent exists"""
        return await self.redis.exists(f"acn:agents:{agent_id}") > 0

    async def count_by_subnet(self, subnet_id: str) -> int:
        """Count agents in a subnet"""
        return await self.redis.scard(f"acn:subnets:{subnet_id}:agents")

    async def find_by_api_key(self, api_key: str) -> Agent | None:
        """Find agent by API key (for autonomous agent authentication)"""
        api_key_index = f"acn:agents:by_api_key:{api_key}"
        agent_id = await self.redis.get(api_key_index)

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

    async def mark_offline_stale(self) -> int:
        """Mark agents whose alive key has expired as offline. Returns count."""
        count = 0
        async for key in self.redis.scan_iter("acn:agents:*"):
            # Skip index/alive/subnet keys — only process main agent hashes
            if (":by_" in key or ":subnets:" in key
                    or key.endswith(":unclaimed") or key.endswith(":alive")):
                continue
            agent_id = key.removeprefix("acn:agents:")
            current_status = await self.redis.hget(key, "status")
            if current_status == "online":
                alive = await self.redis.exists(f"acn:agents:{agent_id}:alive")
                if not alive:
                    await self.redis.hset(key, "status", "offline")
                    count += 1
        return count

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
            "status": AgentStatus(agent_dict["status"]),
            "description": agent_dict.get("description"),
            "skills": json.loads(agent_dict.get("skills", "[]")),
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
        }

        return Agent(**data)
