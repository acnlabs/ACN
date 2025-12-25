"""
Message Router

ACN Communication Layer core component.
Routes messages between agents using:
- ACN Registry for agent discovery
- Official A2A SDK for protocol communication

Based on: https://github.com/a2aproject/A2A
"""

import json
import logging
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import redis.asyncio as redis

# Official A2A SDK
from a2a.client import A2AClient  # type: ignore[import-untyped]
from a2a.types import (  # type: ignore[import-untyped]
    DataPart,
    Message,
    MessageSendParams,
    Part,
    Role,
    SendMessageRequest,
    TextPart,
)

from ..registry import AgentRegistry

logger = logging.getLogger(__name__)


class MessageRouter:
    """
    ACN Message Router

    Core responsibilities:
    1. Discover agent endpoints via ACN Registry
    2. Send messages using official A2A SDK
    3. Handle message logging and dead letter queue

    Usage:
        router = MessageRouter(registry, redis_client)

        # Route message to single agent
        response = await router.route(
            from_agent="chat-service",
            to_agent="cursor-agent",
            message=Message(role="user", parts=[TextPart(text="Generate login page")])
        )

        # Route with skill-based discovery
        response = await router.route_by_skill(
            from_agent="taskmaster",
            skills=["frontend", "react"],
            message=Message(role="user", parts=[TextPart(text="Implement UI")])
        )
    """

    def __init__(
        self,
        registry: AgentRegistry,
        redis_client: redis.Redis,
    ):
        """
        Initialize Message Router

        Args:
            registry: ACN Registry for agent discovery
            redis_client: Redis for logging and DLQ
        """
        self.registry = registry
        self.redis = redis_client

        # Cache of A2A clients by endpoint
        self._clients: dict[str, A2AClient] = {}

        # Message handlers for incoming messages
        self._handlers: dict[str, list[Callable]] = {}

        logger.info("Message Router initialized (using official A2A SDK)")

    async def _get_client(self, endpoint: str) -> A2AClient:
        """
        Get or create A2A client for endpoint

        Args:
            endpoint: Agent A2A endpoint URL

        Returns:
            A2AClient instance
        """
        if endpoint not in self._clients:
            # Create A2A client with httpx AsyncClient
            httpx_client = httpx.AsyncClient(timeout=30.0)
            self._clients[endpoint] = A2AClient(
                httpx_client=httpx_client,
                url=endpoint,
            )
            logger.debug(f"Created A2A client for {endpoint}")

        return self._clients[endpoint]

    async def route(
        self,
        from_agent: str,
        to_agent: str,
        message: Message,
    ) -> Any:
        """
        Route an A2A message to a specific agent

        Args:
            from_agent: Source agent/service ID
            to_agent: Target agent ID
            message: A2A Message object (from a2a.types)

        Returns:
            A2A response (Message or Task)

        Raises:
            ValueError: If target agent not found
            Exception: On delivery failure
        """
        route_id = uuid4().hex[:8]

        logger.info(f"[{route_id}] Routing: {from_agent} -> {to_agent}")

        # 1. Discover agent endpoint via ACN Registry
        agent_info = await self.registry.get_agent(to_agent)
        if not agent_info:
            raise ValueError(f"Agent not found in ACN Registry: {to_agent}")

        endpoint = agent_info.endpoint
        logger.debug(f"[{route_id}] Discovered endpoint: {endpoint}")

        # 2. Log outbound message
        await self._log_message(
            route_id=route_id,
            from_agent=from_agent,
            to_agent=to_agent,
            message=message,
            direction="outbound",
        )

        try:
            # 3. Get A2A client and send message
            client = await self._get_client(endpoint)

            # Create SendMessageRequest
            request = SendMessageRequest(
                id=route_id,
                params=MessageSendParams(message=message),
            )
            response = await client.send_message(request)

            # 4. Log response
            logger.debug(f"[{route_id}] Received response: {type(response)}")

            logger.info(f"[{route_id}] Message delivered successfully")
            return response

        except Exception as e:
            logger.error(f"[{route_id}] Delivery failed: {e}")

            # Store in dead letter queue for retry
            await self._store_dlq(
                route_id=route_id,
                from_agent=from_agent,
                to_agent=to_agent,
                message=message,
                error=str(e),
            )
            raise

    async def route_by_skill(
        self,
        from_agent: str,
        skills: list[str],
        message: Message,
        prefer_online: bool = True,
    ) -> Any:
        """
        Discover agent by skills and route message

        Args:
            from_agent: Source agent/service ID
            skills: Required skills for target agent
            message: A2A Message object
            prefer_online: Prefer online agents

        Returns:
            A2A response

        Raises:
            ValueError: If no suitable agent found
        """
        # Discover agents with required skills
        status = "online" if prefer_online else None
        agents = await self.registry.search_agents(
            skills=skills,
            status=status,
        )

        if not agents:
            # Fallback: try without status filter
            if prefer_online:
                agents = await self.registry.search_agents(skills=skills)

            if not agents:
                raise ValueError(f"No agents found with skills: {skills}")

        # Select best agent (simple: first match)
        # TODO: Add load balancing, skill scoring
        target_agent = agents[0]

        logger.info(f"Discovered agent {target_agent.agent_id} for skills {skills}")

        return await self.route(
            from_agent=from_agent,
            to_agent=target_agent.agent_id,
            message=message,
        )

    async def route_stream(
        self,
        from_agent: str,
        to_agent: str,
        message: Message,
    ) -> AsyncGenerator[Any, None]:
        """
        Route message with SSE streaming response

        Args:
            from_agent: Source agent/service ID
            to_agent: Target agent ID
            message: A2A Message object

        Yields:
            SSE events from target agent
        """
        # Discover endpoint
        agent_info = await self.registry.get_agent(to_agent)
        if not agent_info:
            raise ValueError(f"Agent not found: {to_agent}")

        endpoint = agent_info.endpoint
        logger.info(f"Starting stream: {from_agent} -> {to_agent}")

        # Get A2A client and stream
        client = await self._get_client(endpoint)

        async for event in client.send_message_streaming(message):
            yield event

    async def register_handler(
        self,
        message_type: str,
        handler: Callable,
    ) -> None:
        """
        Register handler for incoming messages

        Args:
            message_type: Type of message to handle
            handler: Async handler function
        """
        if message_type not in self._handlers:
            self._handlers[message_type] = []

        self._handlers[message_type].append(handler)
        logger.info(f"Registered handler for: {message_type}")

    async def handle_incoming(
        self,
        from_agent: str,
        message: Message,
    ) -> None:
        """
        Handle incoming A2A message

        Args:
            from_agent: Source agent ID
            message: A2A Message object
        """
        # Determine message type from data part
        message_type = "unknown"
        for part in message.parts:
            if isinstance(part, DataPart):
                data = part.data
                if "notification_type" in data:
                    message_type = data["notification_type"]
                elif "type" in data:
                    message_type = data["type"]
                break

        logger.info(f"Handling incoming message type: {message_type}")

        # Call registered handlers
        if message_type in self._handlers:
            for handler in self._handlers[message_type]:
                try:
                    await handler(from_agent, message)
                except Exception as e:
                    logger.error(f"Handler error: {e}")

        # Also call wildcard handlers
        if "*" in self._handlers:
            for handler in self._handlers["*"]:
                try:
                    await handler(from_agent, message)
                except Exception as e:
                    logger.error(f"Wildcard handler error: {e}")

    async def get_message_history(
        self,
        agent_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Get message history for an agent

        Args:
            agent_id: Agent ID
            limit: Max messages to return

        Returns:
            List of message records
        """
        messages = await self.redis.zrevrange(
            f"acn:messages:agent:{agent_id}",
            0,
            limit - 1,
        )

        return [json.loads(m) for m in messages]

    async def _log_message(
        self,
        route_id: str,
        from_agent: str,
        to_agent: str,
        message: Any,
        direction: str,
    ):
        """Log message to Redis"""
        timestamp = datetime.now(UTC).isoformat()

        # Serialize message
        if hasattr(message, "model_dump"):
            msg_data = message.model_dump()
        elif hasattr(message, "to_dict"):
            msg_data = message.to_dict()
        else:
            msg_data = str(message)

        log_entry = {
            "route_id": route_id,
            "from_agent": from_agent,
            "to_agent": to_agent,
            "direction": direction,
            "timestamp": timestamp,
            "message": msg_data,
        }

        score = datetime.now(UTC).timestamp()

        # Store in both agents' history
        await self.redis.zadd(
            f"acn:messages:agent:{from_agent}",
            {json.dumps(log_entry): score},
        )
        await self.redis.zadd(
            f"acn:messages:agent:{to_agent}",
            {json.dumps(log_entry): score},
        )

        # Global log with TTL
        await self.redis.setex(
            f"acn:messages:log:{route_id}",
            7 * 24 * 60 * 60,  # 7 days
            json.dumps(log_entry),
        )

    async def _store_dlq(
        self,
        route_id: str,
        from_agent: str,
        to_agent: str,
        message: Message,
        error: str,
    ):
        """Store failed message in dead letter queue"""
        # Serialize message
        if hasattr(message, "model_dump"):
            msg_data = message.model_dump()
        else:
            msg_data = str(message)

        dlq_entry = {
            "route_id": route_id,
            "from_agent": from_agent,
            "to_agent": to_agent,
            "message": msg_data,
            "error": error,
            "timestamp": datetime.now(UTC).isoformat(),
            "retry_count": 0,
        }

        await self.redis.lpush("acn:dlq", json.dumps(dlq_entry))
        logger.warning(f"Message {route_id} added to DLQ")

    async def retry_dlq(self, max_retries: int = 3) -> int:
        """
        Retry messages in dead letter queue

        Args:
            max_retries: Maximum retry attempts

        Returns:
            Number of successfully retried messages
        """
        success_count = 0

        while True:
            entry_json = await self.redis.rpop("acn:dlq")
            if not entry_json:
                break

            entry = json.loads(entry_json)

            if entry["retry_count"] >= max_retries:
                logger.error(f"Message {entry['route_id']} exceeded max retries")
                continue

            entry["retry_count"] += 1

            try:
                # Reconstruct message from stored data
                msg_data = entry["message"]
                parts = []

                for part in msg_data.get("parts", []):
                    if part.get("kind") == "text":
                        parts.append(TextPart(text=part.get("text", "")))
                    elif part.get("kind") == "data":
                        parts.append(DataPart(data=part.get("data", {})))

                message = Message(
                    role=msg_data.get("role", "user"),
                    parts=parts,
                )

                await self.route(
                    from_agent=entry["from_agent"],
                    to_agent=entry["to_agent"],
                    message=message,
                )

                success_count += 1
                logger.info(f"DLQ message {entry['route_id']} delivered")

            except Exception as e:
                logger.error(f"DLQ retry failed: {e}")
                await self.redis.lpush("acn:dlq", json.dumps(entry))

        return success_count


# =============================================================================
# Helper functions for creating A2A messages
# =============================================================================


def create_text_message(text: str, role: Role = Role.user) -> Message:
    """
    Create a simple text message

    Args:
        text: Message text
        role: Role.user or Role.agent

    Returns:
        A2A Message object
    """
    parts: list[Part] = [TextPart(text=text)]
    return Message(
        role=role,
        parts=parts,
        message_id=f"msg-{uuid4().hex[:12]}",
    )


def create_data_message(
    data: dict[str, Any],
    text: str | None = None,
    role: Role = Role.user,
) -> Message:
    """
    Create a message with structured data

    Args:
        data: Structured data
        text: Optional text description
        role: Role.user or Role.agent

    Returns:
        A2A Message object
    """
    parts: list[Part] = []
    if text:
        parts.append(TextPart(text=text))
    parts.append(DataPart(data=data))
    return Message(
        role=role,
        parts=parts,
        message_id=f"msg-{uuid4().hex[:12]}",
    )


def create_notification_message(
    notification_type: str,
    content: str,
    metadata: dict[str, Any],
) -> Message:
    """
    Create a notification message (for group chat @mention, etc.)

    Args:
        notification_type: Type of notification
        content: Text content
        metadata: Additional metadata

    Returns:
        A2A Message object
    """
    parts: list[Part] = [
        TextPart(text=content),
        DataPart(
            data={
                "notification_type": notification_type,
                **metadata,
            }
        ),
    ]
    return Message(
        role=Role.user,
        parts=parts,
        message_id=f"msg-{uuid4().hex[:12]}",
    )
