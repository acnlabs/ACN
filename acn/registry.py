"""
ACN Agent Registry

Core registry service for Agent registration, discovery, and management
"""

import json
from datetime import datetime

import redis.asyncio as redis

from .models import AgentCard, AgentInfo


class AgentRegistry:
    """
    Agent Registry Service

    Manages Agent registration, storage, and discovery using Redis
    """

    def __init__(self, redis_url: str):
        """
        Initialize Agent Registry

        Args:
            redis_url: Redis connection URL (e.g., "redis://localhost:6379")
        """
        self.redis = redis.from_url(redis_url, decode_responses=True)

    async def register_agent(
        self,
        agent_id: str,
        name: str,
        endpoint: str,
        skills: list[str],
        agent_card: dict | None = None,
        subnet_ids: list[str] | None = None,
        description: str = "",
        metadata: dict | None = None,
    ) -> bool:
        """
        Register an Agent

        Args:
            agent_id: Unique agent identifier
            name: Agent name
            endpoint: Agent A2A endpoint URL
            skills: List of skill IDs
            agent_card: Optional A2A Agent Card (auto-generated if not provided)
            subnet_ids: Subnets to join (default: ["public"]). Agent can belong to multiple subnets.
            description: Agent description
            metadata: Additional metadata

        Returns:
            True if registration successful
        """
        # Default to public subnet
        if not subnet_ids:
            subnet_ids = ["public"]

        # Generate Agent Card if not provided
        if not agent_card:
            agent_card = self._generate_agent_card(name, endpoint, skills, description)

        # Validate Agent Card
        self._validate_agent_card(agent_card)

        # Store in Redis
        agent_data = {
            "agent_id": agent_id,
            "name": name,
            "description": description,
            "endpoint": endpoint,
            "skills": json.dumps(skills),
            "status": "online",
            "subnet_ids": json.dumps(subnet_ids),  # 存储为 JSON 列表
            "metadata": json.dumps(metadata or {}),
            "registered_at": datetime.now().isoformat(),
            "agent_card": json.dumps(agent_card),
        }

        await self.redis.hset(f"acn:agents:{agent_id}", mapping=agent_data)

        # Index by skills
        for skill in skills:
            await self.redis.sadd(f"acn:skills:{skill}", agent_id)

        # Index by all subnets (Agent can belong to multiple subnets)
        for subnet_id in subnet_ids:
            await self.redis.sadd(f"acn:subnets:{subnet_id}:agents", agent_id)

        # Add to agents list
        await self.redis.sadd("acn:agents:all", agent_id)

        return True

    async def add_agent_to_subnet(self, agent_id: str, subnet_id: str) -> bool:
        """
        Add an existing agent to a subnet

        Args:
            agent_id: Agent identifier
            subnet_id: Subnet to join

        Returns:
            True if successful
        """
        data = await self.redis.hgetall(f"acn:agents:{agent_id}")
        if not data:
            return False

        # Get current subnet_ids
        subnet_ids = json.loads(data.get("subnet_ids", '["public"]'))
        if subnet_id not in subnet_ids:
            subnet_ids.append(subnet_id)
            await self.redis.hset(f"acn:agents:{agent_id}", "subnet_ids", json.dumps(subnet_ids))
            await self.redis.sadd(f"acn:subnets:{subnet_id}:agents", agent_id)

        return True

    async def remove_agent_from_subnet(self, agent_id: str, subnet_id: str) -> bool:
        """
        Remove an agent from a subnet

        Args:
            agent_id: Agent identifier
            subnet_id: Subnet to leave

        Returns:
            True if successful
        """
        data = await self.redis.hgetall(f"acn:agents:{agent_id}")
        if not data:
            return False

        # Get current subnet_ids
        subnet_ids = json.loads(data.get("subnet_ids", '["public"]'))
        if subnet_id in subnet_ids:
            subnet_ids.remove(subnet_id)
            # Ensure at least public subnet
            if not subnet_ids:
                subnet_ids = ["public"]
            await self.redis.hset(f"acn:agents:{agent_id}", "subnet_ids", json.dumps(subnet_ids))
            await self.redis.srem(f"acn:subnets:{subnet_id}:agents", agent_id)

        return True

    async def get_agent(self, agent_id: str) -> AgentInfo | None:
        """
        Get Agent information

        Args:
            agent_id: Agent identifier

        Returns:
            AgentInfo if found, None otherwise
        """
        data = await self.redis.hgetall(f"acn:agents:{agent_id}")

        if not data:
            return None

        # Parse Agent Card
        agent_card = None
        if data.get("agent_card"):
            agent_card_dict = json.loads(data["agent_card"])
            agent_card = AgentCard(**agent_card_dict)

        # Parse metadata
        metadata = {}
        if data.get("metadata"):
            metadata = json.loads(data["metadata"])

        # Parse subnet_ids (支持新旧格式)
        if data.get("subnet_ids"):
            subnet_ids = json.loads(data["subnet_ids"])
        elif data.get("subnet_id"):
            # 向后兼容：旧格式 subnet_id 转换为列表
            subnet_ids = [data["subnet_id"]]
        else:
            subnet_ids = ["public"]

        return AgentInfo(
            agent_id=data["agent_id"],
            name=data["name"],
            description=data.get("description", ""),
            endpoint=data["endpoint"],
            skills=json.loads(data["skills"]),
            status=data["status"],
            subnet_ids=subnet_ids,
            agent_card=agent_card,
            metadata=metadata,
            registered_at=datetime.fromisoformat(data["registered_at"]),
            last_heartbeat=(
                datetime.fromisoformat(data["last_heartbeat"])
                if data.get("last_heartbeat")
                else None
            ),
        )

    async def get_agent_card(self, agent_id: str) -> AgentCard | None:
        """
        Get Agent Card (A2A standard)

        Args:
            agent_id: Agent identifier

        Returns:
            AgentCard if found, None otherwise
        """
        data = await self.redis.hgetall(f"acn:agents:{agent_id}")

        if not data or not data.get("agent_card"):
            return None

        agent_card_dict = json.loads(data["agent_card"])
        return AgentCard(**agent_card_dict)

    async def search_agents(
        self, skills: list[str] | None = None, status: str = "online"
    ) -> list[AgentInfo]:
        """
        Search Agents by skills and status

        Args:
            skills: Optional list of required skills
            status: Agent status filter (default: "online")

        Returns:
            List of matching AgentInfo objects
        """
        if skills:
            # Get agents with all required skills (intersection)
            agent_ids = await self.redis.sinter(*[f"acn:skills:{skill}" for skill in skills])
        else:
            # Get all agents
            agent_ids = await self.redis.smembers("acn:agents:all")

        # Filter by status
        results = []
        for agent_id in agent_ids:
            agent_info = await self.get_agent(agent_id)
            if agent_info and agent_info.status == status:
                results.append(agent_info)

        return results

    async def unregister_agent(self, agent_id: str) -> bool:
        """
        Unregister an Agent

        Args:
            agent_id: Agent identifier

        Returns:
            True if unregistered successfully
        """
        # Get agent data first
        data = await self.redis.hgetall(f"acn:agents:{agent_id}")

        if not data:
            return False

        # Remove from skill indexes
        skills = json.loads(data.get("skills", "[]"))
        for skill in skills:
            await self.redis.srem(f"acn:skills:{skill}", agent_id)

        # Remove from all subnet indexes (支持多子网)
        if data.get("subnet_ids"):
            subnet_ids = json.loads(data["subnet_ids"])
        elif data.get("subnet_id"):
            subnet_ids = [data["subnet_id"]]
        else:
            subnet_ids = ["public"]

        for subnet_id in subnet_ids:
            await self.redis.srem(f"acn:subnets:{subnet_id}:agents", agent_id)

        # Remove from agents list
        await self.redis.srem("acn:agents:all", agent_id)

        # Delete agent data
        await self.redis.delete(f"acn:agents:{agent_id}")

        return True

    async def heartbeat(self, agent_id: str) -> bool:
        """
        Update Agent heartbeat

        Args:
            agent_id: Agent identifier

        Returns:
            True if heartbeat updated
        """
        exists = await self.redis.exists(f"acn:agents:{agent_id}")

        if not exists:
            return False

        await self.redis.hset(
            f"acn:agents:{agent_id}",
            mapping={
                "last_heartbeat": datetime.now().isoformat(),
                "status": "online",
            },
        )

        return True

    async def search_agents_by_subnet(
        self, subnet_id: str, status: str = "online"
    ) -> list[AgentInfo]:
        """
        Search Agents in a specific subnet

        Args:
            subnet_id: Subnet identifier
            status: Agent status filter (default: "online")

        Returns:
            List of matching AgentInfo objects
        """
        agent_ids = await self.redis.smembers(f"acn:subnets:{subnet_id}:agents")

        results = []
        for agent_id in agent_ids:
            agent_info = await self.get_agent(agent_id)
            if agent_info and agent_info.status == status:
                results.append(agent_info)

        return results

    def _generate_agent_card(
        self, name: str, endpoint: str, skills: list[str], description: str = ""
    ) -> dict:
        """
        Generate a standard Agent Card (A2A compliant)

        This is called automatically when an agent registers without
        providing their own Agent Card. Supports agents from different
        frameworks (LangChain, AutoGPT, custom, etc.)

        Args:
            name: Agent name
            endpoint: Agent endpoint URL
            skills: List of skill IDs
            description: Agent description

        Returns:
            Agent Card dictionary (A2A 0.3.0 format)
        """
        return {
            "protocolVersion": "0.3.0",
            "name": name,
            "description": description or f"{name} - Registered via ACN",
            "url": endpoint,
            "skills": [
                {
                    "id": skill,
                    "name": skill.replace("-", " ").replace("_", " ").title(),
                    "description": f"Capability: {skill}",
                }
                for skill in skills
            ],
            "authentication": {
                "type": "bearer",
                "description": "OAuth 2.0 Bearer Token",
            },
        }

    def _validate_agent_card(self, agent_card: dict) -> bool:
        """
        Validate Agent Card format

        Args:
            agent_card: Agent Card dictionary

        Raises:
            ValueError: If Agent Card is invalid

        Returns:
            True if valid
        """
        required_fields = ["protocolVersion", "name", "url"]

        for field in required_fields:
            if field not in agent_card:
                raise ValueError(f"Agent Card missing required field: {field}")

        return True
