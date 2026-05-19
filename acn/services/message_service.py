"""Message Service

Business logic for agent-to-agent messaging and communication.
Wraps MessageRouter with additional business rules and validation.
"""

from typing import Any

import structlog  # type: ignore[import-untyped]
from a2a.compat.v0_3.types import Message  # type: ignore[import-untyped]

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
        # Internal channel (``POST /communication/internal/send``) uses
        # ``from_agent`` values in the reserved ``system:<slug>`` namespace
        # (``assert_system_caller`` at the HTTP layer). These IDs are
        # **not** registered agents — the registry can never issue a UUID
        # colliding with ``system:`` (see 14.5-1 / 14.6 design). Skip the
        # sender table lookup; recipient validation + routing still apply.
        sender_subnet_ids: set[str] | None = None
        if not from_agent_id.startswith("system:"):
            sender = await self.agent_repository.find_by_id(from_agent_id)
            if not sender:
                raise AgentNotFoundException(f"Sender agent {from_agent_id} not found")
            sender_subnet_ids = set(getattr(sender, "subnet_ids", None) or [])

        # Verify recipient exists
        recipient = await self.agent_repository.find_by_id(to_agent_id)
        if not recipient:
            raise AgentNotFoundException(f"Recipient agent {to_agent_id} not found")

        # Verify recipient is online. The single source of truth is the
        # Redis alive key (see ``AgentService.is_alive``). Calling
        # ``filter_alive`` directly here avoids constructing a service
        # instance just to ask one question, and keeps ``MessageService``'s
        # constructor stable.
        alive_ids = await self.agent_repository.filter_alive([to_agent_id])
        if to_agent_id not in alive_ids:
            logger.warning(
                "message_to_offline_agent",
                from_agent=from_agent_id,
                to_agent=to_agent_id,
                status="offline",
            )

        # Route message.
        # ``priority`` is a request-level hint used by the HTTP layer
        # (rate-limit buckets, audit tagging) but ``MessageRouter.route``
        # does not accept it — passing it via **kwargs raises TypeError.
        # Strip it here so callers can pass it freely without caring about
        # the router's signature.
        router_kwargs = {k: v for k, v in kwargs.items() if k != "priority"}

        logger.info(
            "routing_message",
            from_agent=from_agent_id,
            to_agent=to_agent_id,
            priority=kwargs.get("priority", "normal"),
        )

        response = await self.router.route(
            from_agent=from_agent_id,
            to_agent=to_agent_id,
            message=message,
            sender_subnet_ids=sender_subnet_ids,
            **router_kwargs,
        )

        return response

    async def send_message_by_tag(
        self,
        from_agent_id: str,
        tags: list[str],
        message: Message,
        **kwargs: Any,
    ) -> dict:
        """
        Send message to agent with specific skills

        Args:
            from_agent_id: Sender agent ID
            tags: Required tags
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
            "routing_message_by_tag",
            from_agent=from_agent_id,
            tags=tags,
        )

        # Route by tag
        response = await self.router.route_by_tag(
            from_agent=from_agent_id,
            tags=tags,
            message=message,
            **kwargs,
        )

        return response

    # NOTE: ``broadcast_message`` was deleted in the Phase 2 Group C #9 /
    # review v2 P1 #7 convergence (see
    # ``docs/features/acn-communication-economic-model.md`` L608–L614).
    # All HTTP broadcast traffic now flows through
    # ``BroadcastService.broadcast`` — same path the A2A protocol
    # entry was already using. The reverse-collapse direction was
    # chosen because ``BroadcastService`` is the more complete
    # implementation (real ``asyncio.gather`` parallelism +
    # Redis-persisted ``broadcast_id`` + aggregated stats), and
    # rewriting the HTTP routes was ~1/3 the cost of upgrading
    # ``MessageService.broadcast_message`` to match.

    async def ack_message_history(
        self,
        agent_id: str,
        route_ids: list[str],
    ) -> int:
        """Precisely acknowledge (remove) specific messages from an agent's inbox.

        Args:
            agent_id: Agent whose inbox to update.
            route_ids: List of route_ids to remove.

        Returns:
            Number of messages removed.

        Raises:
            AgentNotFoundException: If the agent does not exist.
        """
        agent = await self.agent_repository.find_by_id(agent_id)
        if not agent:
            raise AgentNotFoundException(f"Agent {agent_id} not found")

        return await self.router.ack_inbox(agent_id, route_ids)

    async def get_message_history(
        self,
        agent_id: str,
        limit: int = 100,
        consume: bool = False,
    ) -> list[dict]:
        """
        Get offline inbox for an agent (messages that failed delivery while offline).

        Args:
            agent_id: Agent ID
            limit: Maximum number of messages to return
            consume: If True, clear the inbox after retrieval

        Returns:
            List of pending message records, newest first
        """
        agent = await self.agent_repository.find_by_id(agent_id)
        if not agent:
            raise AgentNotFoundException(f"Agent {agent_id} not found")

        return await self.router.get_inbox(agent_id, limit, consume)

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
