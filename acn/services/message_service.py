"""Message Service

Business logic for agent-to-agent messaging and communication.
Wraps MessageRouter with additional business rules and validation.
"""

from typing import Any

import structlog  # type: ignore[import-untyped]
from a2a.types import Message  # type: ignore[import-untyped]

from ..core.exceptions import AgentNotFoundException
from ..core.interfaces import IAgentRepository
from ..infrastructure.messaging import MessageRouter

logger = structlog.get_logger()


class MessageService:
    """
    Message Service

    Orchestrates agent-to-agent communication.
    Provides business logic layer on top of MessageRouter.
    """

    def __init__(
        self,
        message_router: MessageRouter,
        agent_repository: IAgentRepository,
    ):
        """
        Initialize Message Service

        Args:
            message_router: MessageRouter for A2A communication
            agent_repository: Agent repository for validation
        """
        self.router = message_router
        self.agent_repository = agent_repository

    async def send_message(
        self,
        from_agent_id: str,
        to_agent_id: str,
        message: Message,
        **kwargs: Any,
    ) -> dict:
        """
        Send message from one agent to another

        Args:
            from_agent_id: Sender agent ID
            to_agent_id: Recipient agent ID
            message: A2A Message object
            **kwargs: Additional routing parameters

        Returns:
            Message response dict

        Raises:
            AgentNotFoundException: If sender or recipient not found
        """
        # Verify sender exists
        sender = await self.agent_repository.find_by_id(from_agent_id)
        if not sender:
            raise AgentNotFoundException(f"Sender agent {from_agent_id} not found")

        # Verify recipient exists
        recipient = await self.agent_repository.find_by_id(to_agent_id)
        if not recipient:
            raise AgentNotFoundException(f"Recipient agent {to_agent_id} not found")

        # Verify recipient is online
        if not recipient.is_online():
            logger.warning(
                "message_to_offline_agent",
                from_agent=from_agent_id,
                to_agent=to_agent_id,
                status=recipient.status.value,
            )

        # Route message
        logger.info(
            "routing_message",
            from_agent=from_agent_id,
            to_agent=to_agent_id,
        )

        response = await self.router.route(
            from_agent=from_agent_id,
            to_agent=to_agent_id,
            message=message,
            **kwargs,
        )

        return response

    async def send_message_by_skill(
        self,
        from_agent_id: str,
        skills: list[str],
        message: Message,
        **kwargs: Any,
    ) -> dict:
        """
        Send message to agent with specific skills

        Args:
            from_agent_id: Sender agent ID
            skills: Required skills
            message: A2A Message object
            **kwargs: Additional routing parameters

        Returns:
            Message response dict

        Raises:
            AgentNotFoundException: If sender not found or no matching agent
        """
        # Verify sender exists
        sender = await self.agent_repository.find_by_id(from_agent_id)
        if not sender:
            raise AgentNotFoundException(f"Sender agent {from_agent_id} not found")

        logger.info(
            "routing_message_by_skill",
            from_agent=from_agent_id,
            skills=skills,
        )

        # Route by skill
        response = await self.router.route_by_skill(
            from_agent=from_agent_id,
            skills=skills,
            message=message,
            **kwargs,
        )

        return response

    async def broadcast_message(
        self,
        from_agent_id: str,
        message: Message,
        subnet_id: str | None = None,
        skills: list[str] | None = None,
        strategy: str = "parallel",
        **kwargs: Any,
    ) -> list[dict]:
        """
        Broadcast message to multiple agents

        Args:
            from_agent_id: Sender agent ID
            message: A2A Message object
            subnet_id: Optional subnet filter
            skills: Optional skill filter
            strategy: Broadcast strategy (parallel/sequential/best_effort)
            **kwargs: Additional parameters

        Returns:
            List of responses from agents

        Raises:
            AgentNotFoundException: If sender not found
        """
        # Verify sender exists
        sender = await self.agent_repository.find_by_id(from_agent_id)
        if not sender:
            raise AgentNotFoundException(f"Sender agent {from_agent_id} not found")

        # Find target agents
        if subnet_id:
            agents = await self.agent_repository.find_by_subnet(subnet_id)
        elif skills:
            agents = await self.agent_repository.find_by_skills(skills)
        else:
            agents = await self.agent_repository.find_all()

        # Filter out sender
        target_agents = [a for a in agents if a.agent_id != from_agent_id]

        if not target_agents:
            logger.warning(
                "broadcast_no_targets",
                from_agent=from_agent_id,
                subnet_id=subnet_id,
                skills=skills,
            )
            return []

        logger.info(
            "broadcasting_message",
            from_agent=from_agent_id,
            target_count=len(target_agents),
            strategy=strategy,
        )

        # Broadcast to all targets
        responses = []
        for agent in target_agents:
            try:
                response = await self.router.route(
                    from_agent=from_agent_id,
                    to_agent=agent.agent_id,
                    message=message,
                    **kwargs,
                )
                responses.append({
                    "agent_id": agent.agent_id,
                    "status": "success",
                    "response": response,
                })
            except Exception as e:
                logger.error(
                    "broadcast_failed",
                    target_agent=agent.agent_id,
                    error=str(e),
                )
                if strategy != "best_effort":
                    raise
                responses.append({
                    "agent_id": agent.agent_id,
                    "status": "failed",
                    "error": str(e),
                })

        return responses

    async def get_message_history(
        self,
        agent_id: str,
        limit: int = 100,
    ) -> list[dict]:
        """
        Get message history for an agent

        Args:
            agent_id: Agent ID
            limit: Maximum number of messages

        Returns:
            List of message records
        """
        # Verify agent exists
        agent = await self.agent_repository.find_by_id(agent_id)
        if not agent:
            raise AgentNotFoundException(f"Agent {agent_id} not found")

        # Get message log from MessageRouter
        return await self.router.get_message_log(agent_id, limit)

    async def register_handler(
        self,
        agent_id: str,
        handler: Any,
    ) -> None:
        """
        Register message handler for an agent

        Args:
            agent_id: Agent ID
            handler: Message handler function
        """
        await self.router.register_handler(agent_id, handler)

        logger.info("message_handler_registered", agent_id=agent_id)

