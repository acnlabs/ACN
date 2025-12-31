"""Redis Implementation of Agent Repository

Concrete implementation using Redis for agent persistence.
"""

import json
from datetime import datetime

import redis.asyncio as redis  # type: ignore[import-untyped]

from ....core.entities import Agent, AgentStatus
from ....core.interfaces import IAgentRepository


class RedisAgentRepository(IAgentRepository):
    """
    Redis-based Agent Repository

    Implements IAgentRepository using Redis as storage backend.
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

        # Serialize agent to dict
        agent_dict = agent.to_dict()
        
        # Convert lists/dicts to JSON strings for Redis
        agent_dict["skills"] = json.dumps(agent_dict.get("skills", []))
        agent_dict["subnet_ids"] = json.dumps(agent_dict.get("subnet_ids", ["public"]))
        agent_dict["payment_methods"] = json.dumps(agent_dict.get("payment_methods", []))
        agent_dict["metadata"] = json.dumps(agent_dict.get("metadata", {}))
        
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

        # Create index: owner + endpoint → agent_id
        endpoint_key = f"acn:agents:by_endpoint:{agent.owner}:{agent.endpoint}"
        await self.redis.set(endpoint_key, agent.agent_id)

        # Add to subnet indices
        for subnet_id in agent.subnet_ids:
            await self.redis.sadd(f"acn:subnets:{subnet_id}:agents", agent.agent_id)

        # Add to owner index
        await self.redis.sadd(f"acn:agents:by_owner:{agent.owner}", agent.agent_id)

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

    async def find_all(self) -> list[Agent]:
        """Find all agents"""
        # Scan for all agent keys
        agents = []
        async for key in self.redis.scan_iter("acn:agents:*"):
            # Skip index keys
            if ":by_" in key or ":subnets:" in key:
                continue
            agent_dict = await self.redis.hgetall(key)
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
        """Find agents by skills"""
        all_agents = await self.find_all()

        # Filter by skills and status
        matching_agents = []
        for agent in all_agents:
            if agent.status.value == status and agent.has_all_skills(skills):
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
        endpoint_key = f"acn:agents:by_endpoint:{agent.owner}:{agent.endpoint}"
        await self.redis.delete(endpoint_key)

        # Remove from subnet indices
        for subnet_id in agent.subnet_ids:
            await self.redis.srem(f"acn:subnets:{subnet_id}:agents", agent_id)

        # Remove from owner index
        await self.redis.srem(f"acn:agents:by_owner:{agent.owner}", agent_id)

        return True

    async def exists(self, agent_id: str) -> bool:
        """Check if agent exists"""
        return await self.redis.exists(f"acn:agents:{agent_id}") > 0

    async def count_by_subnet(self, subnet_id: str) -> int:
        """Count agents in a subnet"""
        return await self.redis.scard(f"acn:subnets:{subnet_id}:agents")

    def _dict_to_agent(self, agent_dict: dict) -> Agent:
        """Convert Redis dict to Agent entity"""
        # Parse JSON fields
        data = {
            "agent_id": agent_dict["agent_id"],
            "owner": agent_dict["owner"],
            "name": agent_dict["name"],
            "endpoint": agent_dict["endpoint"],
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
            "wallet_address": agent_dict.get("wallet_address"),
            "accepts_payment": agent_dict.get("accepts_payment", "false").lower() == "true",
            "payment_methods": json.loads(agent_dict.get("payment_methods", "[]")),
        }

        return Agent(**data)
