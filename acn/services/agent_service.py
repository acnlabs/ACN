"""Agent Service

Business logic for agent registration, discovery, and management.
"""

import secrets
from uuid import uuid4

import structlog  # type: ignore[import-untyped]

from ..core.entities import Agent, ClaimStatus
from ..core.exceptions import AgentNotFoundException
from ..core.interfaces import IAgentRepository
from .auth0_client import Auth0CredentialClient

# Heartbeat TTL policy (seconds)
ALIVE_GRACE_TTL = 1800  # 30 min — grace period after join, no heartbeat yet
ALIVE_RENEW_TTL = 3600  # 60 min — renewed on each heartbeat call

logger = structlog.get_logger()


def generate_api_key() -> str:
    """Generate a secure API key"""
    return f"acn_{secrets.token_urlsafe(32)}"


def generate_verification_code() -> str:
    """Generate a short verification code for human claim"""
    return f"acn-{secrets.token_hex(2).upper()}"


class AgentService:
    """
    Agent Service

    Orchestrates agent-related business operations.
    Uses Repository pattern for persistence.
    """

    def __init__(
        self,
        agent_repository: IAgentRepository,
        auth0_client: Auth0CredentialClient | None = None,
    ):
        """
        Initialize Agent Service

        Args:
            agent_repository: Agent repository implementation
            auth0_client: Auth0 credential client for creating Agent M2M credentials
        """
        self.repository = agent_repository
        self.auth0_client = auth0_client

    async def register_agent(
        self,
        owner: str,
        name: str,
        endpoint: str,
        skills: list[str] | None = None,
        subnet_ids: list[str] | None = None,
        description: str | None = None,
        metadata: dict | None = None,
        agent_card: dict | None = None,
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
            if agent_card is not None:
                existing_agent.agent_card = agent_card

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
            await self.repository.set_alive(existing_agent.agent_id, ALIVE_RENEW_TTL)
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
            agent_card=agent_card,
            wallet_address=wallet_address,
            accepts_payment=accepts_payment,
            payment_methods=payment_methods or [],
        )

        logger.info("register_new_agent", agent_id=agent_id, name=name)
        await self.repository.save(agent)
        await self.repository.set_alive(agent_id, ALIVE_GRACE_TTL)

        # 创建 Auth0 M2M 凭证（异步，不阻塞注册）
        if self.auth0_client:
            try:
                cred_result = await self.auth0_client.create_credentials(
                    agent_id=agent_id,
                    agent_name=name,
                )
                if cred_result.success:
                    agent.auth0_client_id = cred_result.client_id
                    agent.auth0_client_secret = cred_result.client_secret
                    agent.auth0_token_endpoint = cred_result.token_endpoint
                    await self.repository.save(agent)
                    logger.info(
                        "agent_auth0_credentials_assigned",
                        agent_id=agent_id,
                        client_id=cred_result.client_id,
                    )
                else:
                    logger.warning(
                        "agent_auth0_credentials_failed",
                        agent_id=agent_id,
                        error=cred_result.error,
                    )
            except Exception as e:
                logger.warning(
                    "agent_auth0_credentials_error",
                    agent_id=agent_id,
                    error=str(e),
                )

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
            if skills:
                agents = [a for a in agents if a.has_all_skills(skills)]
            if status:
                agents = [a for a in agents if a.status.value == status]
            if status == "online":
                alive_ids = await self.repository.filter_alive([a.agent_id for a in agents])
                agents = [a for a in agents if a.agent_id in alive_ids]
            return agents

        if skills:
            candidates = await self.repository.find_by_skills(skills, status)
            if status == "online":
                alive_ids = await self.repository.filter_alive([a.agent_id for a in candidates])
                return [a for a in candidates if a.agent_id in alive_ids]
            return candidates

        # Return all agents matching status, filtered by alive key for online
        all_agents = await self.repository.find_all()
        candidates = [a for a in all_agents if a.status.value == status]
        if status == "online":
            alive_ids = await self.repository.filter_alive([a.agent_id for a in candidates])
            return [a for a in candidates if a.agent_id in alive_ids]
        return candidates

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
        await self.repository.set_alive(agent_id, ALIVE_RENEW_TTL)
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

    # ========== Autonomous Agent Methods ==========

    async def join_agent(
        self,
        name: str,
        description: str | None = None,
        skills: list[str] | None = None,
        endpoint: str | None = None,
        referrer_id: str | None = None,
        metadata: dict | None = None,
        agent_card: dict | None = None,
    ) -> tuple[Agent, str]:
        """
        Autonomous agent joins ACN (self-registration)

        Unlike register_agent (platform-managed), this allows agents
        to self-register without an owner. Returns an API key for auth.

        Args:
            name: Agent name
            description: Agent description
            skills: List of skill IDs
            endpoint: A2A endpoint URL (optional for pull mode)
            referrer_id: ID of agent who referred this one
            metadata: Additional metadata
            agent_card: A2A Agent Card (v0.3.0) to store at registration time

        Returns:
            Tuple of (Agent entity, API key)
        """
        agent_id = str(uuid4())
        api_key = generate_api_key()
        verification_code = generate_verification_code()

        agent = Agent(
            agent_id=agent_id,
            name=name,
            owner=None,  # No owner initially
            endpoint=endpoint,
            description=description,
            skills=skills or [],
            subnet_ids=["public"],
            metadata=metadata or {},
            api_key=api_key,
            claim_status=ClaimStatus.UNCLAIMED,
            verification_code=verification_code,
            referrer_id=referrer_id,
            agent_card=agent_card,
        )

        logger.info("agent_joined", agent_id=agent_id, name=name, referrer_id=referrer_id)
        await self.repository.save(agent)
        await self.repository.set_alive(agent_id, ALIVE_GRACE_TTL)
        return agent, api_key

    async def get_agent_by_api_key(self, api_key: str) -> Agent | None:
        """
        Find agent by API key (for authentication)

        Args:
            api_key: Agent API key

        Returns:
            Agent entity or None
        """
        return await self.repository.find_by_api_key(api_key)

    async def claim_agent(
        self,
        agent_id: str,
        owner: str,
        verification_code: str | None = None,
    ) -> Agent:
        """
        Claim ownership of an unclaimed agent

        Args:
            agent_id: Agent identifier
            owner: New owner identifier
            verification_code: Optional verification code

        Returns:
            Claimed agent entity

        Raises:
            AgentNotFoundException: If agent not found
            ValueError: If agent is already claimed or code doesn't match
        """
        agent = await self.get_agent(agent_id)

        if agent.claim_status == ClaimStatus.CLAIMED:
            raise ValueError(f"Agent {agent_id} is already claimed")

        # Verify code if agent has one
        if agent.verification_code and verification_code:
            if agent.verification_code != verification_code:
                raise ValueError("Invalid verification code")

        agent.claim(owner)
        await self.repository.save(agent)

        logger.info("agent_claimed", agent_id=agent_id, owner=owner)
        return agent

    async def transfer_agent(
        self,
        agent_id: str,
        current_owner: str,
        new_owner: str,
    ) -> Agent:
        """
        Transfer agent ownership to another user

        Args:
            agent_id: Agent identifier
            current_owner: Current owner (for authorization)
            new_owner: New owner identifier

        Returns:
            Updated agent entity

        Raises:
            AgentNotFoundException: If agent not found
            PermissionError: If current_owner doesn't match
        """
        agent = await self.get_agent(agent_id)

        if agent.owner != current_owner:
            raise PermissionError("Only owner can transfer agent")

        agent.transfer(new_owner)
        await self.repository.save(agent)

        logger.info(
            "agent_transferred",
            agent_id=agent_id,
            from_owner=current_owner,
            to_owner=new_owner,
        )
        return agent

    async def release_agent(self, agent_id: str, owner: str) -> Agent:
        """
        Release ownership of an agent (make it unowned)

        Args:
            agent_id: Agent identifier
            owner: Current owner (for authorization)

        Returns:
            Updated agent entity

        Raises:
            AgentNotFoundException: If agent not found
            PermissionError: If owner doesn't match
        """
        agent = await self.get_agent(agent_id)

        if agent.owner != owner:
            raise PermissionError("Only owner can release agent")

        agent.release()
        await self.repository.save(agent)

        logger.info("agent_released", agent_id=agent_id, previous_owner=owner)
        return agent

    async def get_unclaimed_agents(self, limit: int = 100) -> list[Agent]:
        """
        Get all unclaimed agents

        Args:
            limit: Maximum number of agents to return

        Returns:
            List of unclaimed agents
        """
        return await self.repository.find_unclaimed(limit)
