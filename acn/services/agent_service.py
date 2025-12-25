"""Agent Service

Business logic for agent registration, discovery, and management.
"""

from uuid import uuid4

import structlog  # type: ignore[import-untyped]

from ..core.entities import Agent
from ..core.exceptions import AgentNotFoundException
from ..core.interfaces import IAgentRepository

logger = structlog.get_logger()


class AgentService:
    """
    Agent Service

    Orchestrates agent-related business operations.
    Uses Repository pattern for persistence.
    """

    def __init__(self, agent_repository: IAgentRepository):
        """
        Initialize Agent Service

        Args:
            agent_repository: Agent repository implementation
        """
        self.repository = agent_repository

    async def register_agent(
        self,
        owner: str,
        name: str,
        endpoint: str,
        skills: list[str] | None = None,
        subnet_ids: list[str] | None = None,
        description: str | None = None,
        metadata: dict | None = None,
        wallet_address: str | None = None,
        accepts_payment: bool = False,
        payment_methods: list[str] | None = None,
    ) -> Agent:
        """
        Register a new agent or update existing one

        Uses natural key idempotency: if owner + endpoint already exists,
        updates that agent; otherwise creates new one.

        Args:
            owner: Agent owner identifier
            name: Agent name
            endpoint: Agent A2A endpoint URL
            skills: List of skill IDs
            subnet_ids: Subnets to join
            description: Agent description
            metadata: Additional metadata
            wallet_address: Wallet address for payments
            accepts_payment: Whether agent accepts payments
            payment_methods: Accepted payment methods

        Returns:
            Registered agent entity
        """
        # Check for existing agent by owner + endpoint
        existing_agent = await self.repository.find_by_owner_and_endpoint(owner, endpoint)

        if existing_agent:
            # Update existing agent
            logger.info("update_existing_agent", agent_id=existing_agent.agent_id)
            existing_agent.name = name
            existing_agent.description = description
            existing_agent.skills = skills or []
            existing_agent.metadata = metadata or {}

            # Update subnets if provided
            if subnet_ids:
                existing_agent.subnet_ids = subnet_ids

            # Update payment info
            if wallet_address:
                existing_agent.wallet_address = wallet_address
            existing_agent.accepts_payment = accepts_payment
            if payment_methods:
                existing_agent.payment_methods = payment_methods

            existing_agent.update_heartbeat()
            existing_agent.mark_online()

            await self.repository.save(existing_agent)
            return existing_agent

        # Create new agent
        agent_id = str(uuid4())
        agent = Agent(
            agent_id=agent_id,
            owner=owner,
            name=name,
            endpoint=endpoint,
            description=description,
            skills=skills or [],
            subnet_ids=subnet_ids or ["public"],
            metadata=metadata or {},
            wallet_address=wallet_address,
            accepts_payment=accepts_payment,
            payment_methods=payment_methods or [],
        )

        logger.info("register_new_agent", agent_id=agent_id, name=name)
        await self.repository.save(agent)
        return agent

    async def get_agent(self, agent_id: str) -> Agent:
        """
        Get agent by ID

        Args:
            agent_id: Agent identifier

        Returns:
            Agent entity

        Raises:
            AgentNotFoundException: If agent not found
        """
        agent = await self.repository.find_by_id(agent_id)
        if not agent:
            raise AgentNotFoundException(f"Agent {agent_id} not found")
        return agent

    async def search_agents(
        self,
        skills: list[str] | None = None,
        status: str = "online",
        subnet_id: str | None = None,
    ) -> list[Agent]:
        """
        Search for agents

        Args:
            skills: Required skills (filters agents that have ALL skills)
            status: Agent status filter
            subnet_id: Subnet filter

        Returns:
            List of matching agents
        """
        if subnet_id:
            agents = await self.repository.find_by_subnet(subnet_id)
            # Apply additional filters
            if skills:
                agents = [a for a in agents if a.has_all_skills(skills)]
            if status:
                agents = [a for a in agents if a.status.value == status]
            return agents

        if skills:
            return await self.repository.find_by_skills(skills, status)

        # Return all agents matching status
        all_agents = await self.repository.find_all()
        return [a for a in all_agents if a.status.value == status]

    async def unregister_agent(self, agent_id: str, owner: str) -> bool:
        """
        Unregister an agent

        Args:
            agent_id: Agent identifier
            owner: Owner identifier (for authorization check)

        Returns:
            True if unregistered successfully

        Raises:
            AgentNotFoundException: If agent not found
            PermissionError: If owner doesn't match
        """
        agent = await self.get_agent(agent_id)

        # Authorization check
        if agent.owner != owner:
            raise PermissionError(f"Owner mismatch: {owner} != {agent.owner}")

        logger.info("unregister_agent", agent_id=agent_id)
        return await self.repository.delete(agent_id)

    async def update_heartbeat(self, agent_id: str) -> Agent:
        """
        Update agent heartbeat

        Args:
            agent_id: Agent identifier

        Returns:
            Updated agent entity
        """
        agent = await self.get_agent(agent_id)
        agent.update_heartbeat()
        agent.mark_online()
        await self.repository.save(agent)
        return agent

    async def get_agents_by_owner(self, owner: str) -> list[Agent]:
        """
        Get all agents owned by a user

        Args:
            owner: Owner identifier

        Returns:
            List of owned agents
        """
        return await self.repository.find_by_owner(owner)

    async def join_subnet(self, agent_id: str, subnet_id: str) -> Agent:
        """
        Add agent to a subnet

        Args:
            agent_id: Agent identifier
            subnet_id: Subnet identifier

        Returns:
            Updated agent entity
        """
        agent = await self.get_agent(agent_id)
        agent.add_to_subnet(subnet_id)
        await self.repository.save(agent)
        logger.info("agent_joined_subnet", agent_id=agent_id, subnet_id=subnet_id)
        return agent

    async def leave_subnet(self, agent_id: str, subnet_id: str) -> Agent:
        """
        Remove agent from a subnet

        Args:
            agent_id: Agent identifier
            subnet_id: Subnet identifier

        Returns:
            Updated agent entity
        """
        agent = await self.get_agent(agent_id)
        agent.remove_from_subnet(subnet_id)
        await self.repository.save(agent)
        logger.info("agent_left_subnet", agent_id=agent_id, subnet_id=subnet_id)
        return agent
