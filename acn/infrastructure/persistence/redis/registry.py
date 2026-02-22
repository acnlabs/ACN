"""
ACN Agent Registry

Core registry service for Agent registration, discovery, and management
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import redis.asyncio as redis

from a2a.types import (  # type: ignore[import-untyped]
    AgentCapabilities,
    AgentCard,
    AgentSkill,
)

from ....models import AgentInfo


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
        owner: str,
        name: str,
        endpoint: str,
        skills: list[str],
        agent_card: dict | None = None,
        subnet_ids: list[str] | None = None,
        description: str = "",
        metadata: dict | None = None,
    ) -> str:
        """
        Register an Agent (Idempotent) - ACN fully manages IDs

        ACN automatically handles agent IDs:
        - If owner + endpoint already exists: Updates existing agent (ID unchanged)
        - If new agent: Generates new UUID

        This ensures:
        - Callers never need to manage IDs
        - Automatic idempotency (no duplicate agents)
        - ACN has full control of identity

        Args:
            owner: Agent owner (system/user-{id}/provider-{id})
            name: Agent name
            endpoint: Agent A2A endpoint URL
            skills: List of skill IDs
            agent_card: Optional A2A Agent Card (auto-generated if not provided)
            subnet_ids: Subnets to join (default: ["public"]). Agent can belong to multiple subnets.
            description: Agent description
            metadata: Additional metadata

        Returns:
            agent_id (UUID string) - Generated or reused by ACN
        """
        # ACN-managed ID strategy (Natural Key Idempotency):
        # 1. Check by endpoint (natural key): If same owner + endpoint exists, reuse its ID
        # 2. Otherwise: Generate new random UUID

        # Try to find existing agent by owner + endpoint (natural key)
        existing_id = await self._find_agent_by_endpoint(owner, endpoint)
        if existing_id:
            # Found existing agent: reuse its ID (UPDATE semantics)
            agent_id = existing_id
        else:
            # New agent: generate random UUID (CREATE semantics)
            agent_id = str(uuid4())

        # Check if agent already exists (for idempotency)
        existing_agent = await self.redis.hgetall(f"acn:agents:{agent_id}")
        is_update = bool(existing_agent)

        # Default to public subnet
        if not subnet_ids:
            subnet_ids = ["public"]

        # Generate Agent Card if not provided
        if not agent_card:
            agent_card = self._generate_agent_card(name, endpoint, skills, description)

        # Validate Agent Card
        self._validate_agent_card(agent_card)

        # Prepare agent data
        agent_data = {
            "agent_id": agent_id,
            "owner": owner,
            "name": name,
            "description": description,
            "endpoint": endpoint,
            "skills": json.dumps(skills),
            "status": "online",
            "subnet_ids": json.dumps(subnet_ids),
            "metadata": json.dumps(metadata or {}),
            "agent_card": json.dumps(agent_card),
        }

        # Preserve registered_at for updates, set it for new registrations
        if is_update and existing_agent.get("registered_at"):
            agent_data["registered_at"] = existing_agent["registered_at"]
        else:
            agent_data["registered_at"] = datetime.now(UTC).isoformat()

        # Store in Redis (overwrites if exists - idempotent)
        await self.redis.hset(f"acn:agents:{agent_id}", mapping=agent_data)

        # Clean up old indexes if updating
        if is_update:
            # Remove from old skill indexes
            old_skills = json.loads(existing_agent.get("skills", "[]"))
            for skill in old_skills:
                if skill not in skills:
                    await self.redis.srem(f"acn:skills:{skill}", agent_id)

            # Remove from old subnet indexes
            old_subnets = json.loads(existing_agent.get("subnet_ids", '["public"]'))
            for subnet_id in old_subnets:
                if subnet_id not in subnet_ids:
                    await self.redis.srem(f"acn:subnets:{subnet_id}:agents", agent_id)

        # Update indexes (idempotent - sadd won't duplicate)
        await self.redis.sadd(f"acn:owners:{owner}:agents", agent_id)

        for skill in skills:
            await self.redis.sadd(f"acn:skills:{skill}", agent_id)

        for subnet_id in subnet_ids:
            await self.redis.sadd(f"acn:subnets:{subnet_id}:agents", agent_id)

        await self.redis.sadd(f"acn:status:{agent_data['status']}", agent_id)
        await self.redis.sadd("acn:agents:all", agent_id)

        return agent_id

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

        # Parse Agent Card (stored as model_dump dict, parse back to SDK type)
        agent_card = None
        if data.get("agent_card"):
            try:
                agent_card = AgentCard(**json.loads(data["agent_card"]))
            except Exception:
                agent_card = None

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
            owner=data.get("owner", "unknown"),
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

        try:
            return AgentCard(**json.loads(data["agent_card"]))
        except Exception:
            return None

    async def search_agents(
        self,
        skills: list[str] | None = None,
        status: str = "online",
        owner: str | None = None,
        name: str | None = None,
    ) -> list[AgentInfo]:
        """
        Search Agents by skills, status, owner, and name

        Args:
            skills: Optional list of required skills
            status: Agent status filter (default: "online")
            owner: Optional owner filter (e.g., "system", "user-123")
            name: Optional name filter (partial match)

        Returns:
            List of matching AgentInfo objects
        """
        # Build candidate set based on filters
        candidate_sets = []

        if skills:
            # Get agents with all required skills (intersection)
            skill_agents = await self.redis.sinter(*[f"acn:skills:{skill}" for skill in skills])
            candidate_sets.append(skill_agents)

        if owner:
            # Get agents by owner
            owner_agents = await self.redis.smembers(f"acn:owners:{owner}:agents")
            candidate_sets.append(owner_agents)

        # If we have filters, intersect them; otherwise get all agents
        if candidate_sets:
            agent_ids = set.intersection(*[set(s) for s in candidate_sets])
        else:
            agent_ids = await self.redis.smembers("acn:agents:all")

        # Filter by status and name
        results = []
        for agent_id in agent_ids:
            agent_info = await self.get_agent(agent_id)
            if not agent_info:
                continue

            # Status filter
            if agent_info.status != status:
                continue

            # Name filter (partial match, case-insensitive)
            if name and name.lower() not in agent_info.name.lower():
                continue

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
                "last_heartbeat": datetime.now(UTC).isoformat(),
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

    async def _find_agent_by_endpoint(self, owner: str, endpoint: str) -> str | None:
        """
        Find existing agent by owner + endpoint (natural key)

        Args:
            owner: Agent owner
            endpoint: Agent endpoint URL

        Returns:
            Agent ID if found, None otherwise
        """
        # Get all agents for this owner
        agent_ids = await self.redis.smembers(f"acn:owners:{owner}:agents")

        # Check each agent's endpoint
        for agent_id in agent_ids:
            agent_data = await self.redis.hgetall(f"acn:agents:{agent_id}")
            if agent_data and agent_data.get("endpoint") == endpoint:
                return agent_id

        return None

    def _generate_agent_card(
        self, name: str, endpoint: str, skills: list[str], description: str = ""
    ) -> dict:
        """Generate an A2A v0.3.0 compliant Agent Card"""
        card = AgentCard(
            name=name,
            version="0.1.0",
            description=description or f"{name} - Registered via ACN",
            url=endpoint,
            capabilities=AgentCapabilities(streaming=False),
            default_input_modes=["text", "application/json"],
            default_output_modes=["text", "application/json"],
            skills=[
                AgentSkill(
                    id=skill,
                    name=skill.replace("-", " ").replace("_", " ").title(),
                    description=f"Capability: {skill}",
                    tags=[skill],
                )
                for skill in skills
            ],
        )
        return card.model_dump(exclude_none=True)

    def _validate_agent_card(self, agent_card: dict) -> bool:
        """Validate Agent Card by parsing with SDK type (raises on invalid)"""
        AgentCard(**agent_card)
        return True
