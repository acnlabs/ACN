"""A2A Protocol Integration for ACN

ACN acts as an "Infrastructure Agent" providing coordination services
through standard A2A protocol endpoints.

ACN is NOT an AI Agent - it doesn't execute AI tasks.
ACN IS an Infrastructure Service - providing registry, routing, broadcast, etc.

By exposing A2A Server endpoints, ACN allows agents to use a unified
protocol for both peer-to-peer communication and infrastructure services.
"""

import uuid
from typing import Any

import structlog  # type: ignore[import-untyped]
from a2a.server.agent_execution import (  # type: ignore[import-untyped]
    AgentExecutor,
    RequestContext,
)
from a2a.server.apps import A2AFastAPIApplication  # type: ignore[import-untyped]
from a2a.server.events import EventQueue  # type: ignore[import-untyped]
from a2a.server.request_handlers import (  # type: ignore[import-untyped]
    DefaultRequestHandler,
)
from a2a.types import (  # type: ignore[import-untyped]
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Artifact,
    DataPart,
    Message,
    Role,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)
from fastapi import FastAPI
from redis.asyncio import Redis

from .a2a import RedisTaskStore
from .communication import (
    BroadcastService,
    BroadcastStrategy,
    MessageRouter,
    SubnetManager,
)
from .config import get_settings
from .registry import AgentRegistry

settings = get_settings()

logger = structlog.get_logger()


class ACNAgentExecutor(AgentExecutor):
    """ACN Infrastructure Agent Executor

    Exposes ACN's infrastructure services through A2A protocol:
    - Broadcast: Multi-agent message broadcasting
    - Discovery: Skill-based agent discovery
    - Subnet Routing: Route messages through subnets
    - Point-to-Point: Direct agent-to-agent routing

    Usage:
        Send A2A message to ACN with action in metadata:

        Message(
            role="user",
            parts=[DataPart(data={
                "action": "broadcast",
                "target_agents": ["agent-a", "agent-b"],
                "message": "Hello"
            })],
            metadata={
                "acn_action": "broadcast"  # or "discover", "route", etc.
            }
        )
    """

    def __init__(
        self,
        registry: AgentRegistry,
        router: MessageRouter,
        broadcast: BroadcastService,
        subnet_manager: SubnetManager,
    ):
        """Initialize ACN Agent Executor

        Args:
            registry: ACN Agent Registry for agent discovery
            router: Message Router for point-to-point routing
            broadcast: Broadcast Service for multi-agent messaging
            subnet_manager: Subnet Manager for subnet gateway routing
        """
        self.registry = registry
        self.router = router
        self.broadcast = broadcast
        self.subnet_manager = subnet_manager

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Execute ACN infrastructure action

        Args:
            context: Request context containing message and metadata
            event_queue: Event queue to enqueue status updates and artifacts
        """
        try:
            # Get message from context
            message = context.message
            if not message:
                await self._send_status(
                    event_queue,
                    context,
                    TaskState.failed,
                    "No message provided",
                    final=True,
                )
                return

            # Determine action
            action = self._extract_action(message, context)

            logger.info(
                "acn_action_received",
                action=action,
                task_id=context.task_id,
                context_id=context.context_id,
            )

            # Route to appropriate handler
            if action == "broadcast":
                await self._handle_broadcast(message, context, event_queue)

            elif action == "discover":
                await self._handle_discovery(message, context, event_queue)

            elif action == "route":
                await self._handle_routing(message, context, event_queue)

            elif action == "subnet_route":
                await self._handle_subnet_routing(message, context, event_queue)

            else:
                await self._send_status(
                    event_queue,
                    context,
                    TaskState.failed,
                    f"Unknown ACN action: {action}. "
                    f"Supported: broadcast, discover, route, subnet_route",
                    final=True,
                )

        except Exception as e:
            logger.error("acn_execution_failed", error=str(e), exc_info=True)
            await self._send_status(
                event_queue,
                context,
                TaskState.failed,
                str(e),
                final=True,
            )

    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Cancel ACN infrastructure task

        ACN tasks are typically short-lived and complete immediately,
        so cancellation is a no-op.

        Args:
            context: Request context
            event_queue: Event queue
        """
        logger.info(
            "acn_task_cancel_requested",
            task_id=context.task_id,
            context_id=context.context_id,
        )
        # ACN infrastructure tasks are atomic and complete immediately
        # There's no long-running process to cancel

    async def _send_status(
        self,
        event_queue: EventQueue,
        context: RequestContext,
        state: TaskState,
        status_message: str = "",
        final: bool = False,
    ) -> None:
        """Send task status update event

        Args:
            event_queue: Event queue
            context: Request context
            state: Task state
            status_message: Human-readable status message (for logging)
            final: Whether this is the final status
        """
        # Create status update event with optional message
        if status_message:
            # Wrap message in Message object
            message_obj = Message(
                role=Role.agent,
                message_id=str(uuid.uuid4()),
                parts=[TextPart(text=status_message)],
            )
            status = TaskStatus(state=state, message=message_obj)
        else:
            status = TaskStatus(state=state)

        event = TaskStatusUpdateEvent(
            task_id=context.task_id,
            context_id=context.context_id,
            status=status,
            final=final,
        )
        await event_queue.enqueue_event(event)

    async def _send_artifact(
        self,
        event_queue: EventQueue,
        context: RequestContext,
        artifact: Artifact,
        last_chunk: bool = False,
    ) -> None:
        """Send artifact update event

        Args:
            event_queue: Event queue
            context: Request context
            artifact: Artifact to send
            last_chunk: Whether this is the last chunk
        """
        event = TaskArtifactUpdateEvent(
            task_id=context.task_id,
            context_id=context.context_id,
            artifact=artifact,
            last_chunk=last_chunk,
        )
        await event_queue.enqueue_event(event)

    def _extract_action(self, message: Message, context: RequestContext) -> str:
        """Extract ACN action from message

        Checks:
        1. context.metadata["acn_action"]
        2. message.metadata["acn_action"]
        3. DataPart data["action"]
        4. Default: "route"
        """
        # Check context metadata
        if "acn_action" in context.metadata:
            return context.metadata["acn_action"]

        # Check message metadata
        if message.metadata and "acn_action" in message.metadata:
            return message.metadata["acn_action"]

        # Check message parts (DataPart)
        # Note: Part is a RootModel, actual DataPart/TextPart is in part.root
        for part in message.parts:
            # Extract the actual part from Part.root
            actual_part = part.root if hasattr(part, "root") else part

            if isinstance(actual_part, DataPart) and "action" in actual_part.data:
                action = actual_part.data["action"]
                logger.debug("extracted_action", action=action, source="DataPart")
                return action

        # Default: point-to-point routing
        return "route"

    async def _handle_broadcast(
        self, message: Message, context: RequestContext, event_queue: EventQueue
    ) -> None:
        """Handle broadcast action"""
        await self._send_status(
            event_queue, context, TaskState.working, "Broadcasting message"
        )

        # Extract broadcast parameters
        params = self._extract_data_from_message(message)
        target_agents = params.get("target_agents", [])
        target_skills = params.get("target_skills")
        strategy = params.get("strategy", "parallel")
        broadcast_message_text = params.get("message", "")

        from_agent = context.metadata.get("from_agent", "unknown")

        # Build broadcast message
        broadcast_msg = Message(
            role=Role.user,
            message_id=str(uuid.uuid4()),
            parts=[TextPart(text=broadcast_message_text)],
        )

        try:
            # Execute broadcast
            if target_skills:
                # Broadcast by skill
                result = await self.broadcast.send_by_skill(
                    from_agent=from_agent,
                    skills=target_skills,
                    message=broadcast_msg,
                    strategy=BroadcastStrategy(strategy),
                )
            else:
                # Broadcast to specific agents
                result = await self.broadcast.send(
                    from_agent=from_agent,
                    to_agents=target_agents,
                    message=broadcast_msg,
                    strategy=BroadcastStrategy(strategy),
                )

            # Return results as artifact
            artifact = Artifact(
                artifact_id=str(uuid.uuid4()),
                name="broadcast_result",
                parts=[
                    DataPart(
                        data={
                            "status": "completed",
                            "results": result,
                            "target_count": len(target_agents)
                            if target_agents
                            else len(result),
                        }
                    )
                ],
            )
            await self._send_artifact(event_queue, context, artifact, last_chunk=True)

            await self._send_status(
                event_queue,
                context,
                TaskState.completed,
                "Broadcast completed",
                final=True,
            )

        except Exception as e:
            logger.error("broadcast_failed", error=str(e))
            await self._send_status(
                event_queue,
                context,
                TaskState.failed,
                f"Broadcast failed: {e}",
                final=True,
            )

    async def _handle_discovery(
        self, message: Message, context: RequestContext, event_queue: EventQueue
    ) -> None:
        """Handle agent discovery action"""
        await self._send_status(
            event_queue, context, TaskState.working, "Discovering agents"
        )

        params = self._extract_data_from_message(message)
        skills = params.get("skills", [])
        status = params.get("status", "online")

        try:
            # Search agents
            agents = await self.registry.search_agents(
                skills=skills,
                status=status,
            )

            # Return results
            artifact = Artifact(
                artifact_id=str(uuid.uuid4()),
                name="discovered_agents",
                parts=[
                    DataPart(
                        data={
                            "agents": [
                                {
                                    "agent_id": agent.agent_id,
                                    "name": agent.name,
                                    "endpoint": agent.endpoint,
                                    "skills": agent.skills,
                                    "status": agent.status,
                                }
                                for agent in agents
                            ],
                            "total": len(agents),
                        }
                    )
                ],
            )
            await self._send_artifact(event_queue, context, artifact, last_chunk=True)

            await self._send_status(
                event_queue,
                context,
                TaskState.completed,
                f"Found {len(agents)} agents",
                final=True,
            )

        except Exception as e:
            logger.error("discovery_failed", error=str(e))
            await self._send_status(
                event_queue,
                context,
                TaskState.failed,
                f"Discovery failed: {e}",
                final=True,
            )

    async def _handle_routing(
        self, message: Message, context: RequestContext, event_queue: EventQueue
    ) -> None:
        """Handle point-to-point routing action"""
        await self._send_status(
            event_queue, context, TaskState.working, "Routing message"
        )

        params = self._extract_data_from_message(message)
        target_agent = params.get("target_agent")
        message_content = params.get("message", "")

        if not target_agent:
            await self._send_status(
                event_queue,
                context,
                TaskState.failed,
                "target_agent not specified",
                final=True,
            )
            return

        from_agent = context.metadata.get("from_agent", "unknown")

        # Build message to route
        route_msg = Message(
            role=Role.user,
            message_id=str(uuid.uuid4()),
            parts=[TextPart(text=message_content)],
        )

        try:
            # Route message
            response = await self.router.route(
                from_agent=from_agent,
                to_agent=target_agent,
                message=route_msg,
            )

            # Return response as artifact
            artifact = Artifact(
                artifact_id=str(uuid.uuid4()),
                name="routing_result",
                parts=[
                    DataPart(
                        data={
                            "response": response,
                            "target_agent": target_agent,
                        }
                    )
                ],
            )
            await self._send_artifact(event_queue, context, artifact, last_chunk=True)

            await self._send_status(
                event_queue, context, TaskState.completed, "Message routed", final=True
            )

        except Exception as e:
            logger.error("routing_failed", error=str(e), target=target_agent)
            await self._send_status(
                event_queue,
                context,
                TaskState.failed,
                f"Routing failed: {e}",
                final=True,
            )

    async def _handle_subnet_routing(
        self, message: Message, context: RequestContext, event_queue: EventQueue
    ) -> None:
        """Handle subnet routing action"""
        await self._send_status(
            event_queue, context, TaskState.working, "Routing through subnet"
        )

        params = self._extract_data_from_message(message)
        subnet_id = params.get("subnet_id")
        agent_id = params.get("agent_id")
        message_content = params.get("message", {})

        if not subnet_id or not agent_id:
            await self._send_status(
                event_queue,
                context,
                TaskState.failed,
                "subnet_id and agent_id required",
                final=True,
            )
            return

        try:
            # Forward through subnet
            response = await self.subnet_manager.forward_request(
                subnet_id, agent_id, message_content
            )

            artifact = Artifact(
                artifact_id=str(uuid.uuid4()),
                name="subnet_routing_result",
                parts=[
                    DataPart(
                        data={
                            "response": response,
                            "subnet_id": subnet_id,
                            "agent_id": agent_id,
                        }
                    )
                ],
            )
            await self._send_artifact(event_queue, context, artifact, last_chunk=True)

            await self._send_status(
                event_queue,
                context,
                TaskState.completed,
                "Subnet routing completed",
                final=True,
            )

        except Exception as e:
            logger.error("subnet_routing_failed", error=str(e))
            await self._send_status(
                event_queue,
                context,
                TaskState.failed,
                f"Subnet routing failed: {e}",
                final=True,
            )

    def _extract_data_from_message(self, message: Message) -> dict[str, Any]:
        """Extract data from message parts

        Returns:
            Combined data from all DataParts
        """
        data = {}

        for part in message.parts:
            # Extract the actual part from Part.root
            actual_part = part.root if hasattr(part, "root") else part

            if isinstance(actual_part, DataPart):
                data.update(actual_part.data)
            elif isinstance(actual_part, TextPart):
                # For simple text messages, store in "message" field
                if "message" not in data:
                    data["message"] = actual_part.text

        return data


def create_a2a_app(
    registry: AgentRegistry,
    router: MessageRouter,
    broadcast: BroadcastService,
    subnet_manager: SubnetManager,
    redis: Redis,
) -> FastAPI:
    """Create A2A FastAPI application for ACN

    Args:
        registry: ACN Agent Registry
        router: Message Router
        broadcast: Broadcast Service
        subnet_manager: Subnet Manager
        redis: Redis client for task persistence

    Returns:
        FastAPI app with A2A endpoints at /a2a/jsonrpc
    """
    # Create ACN agent executor
    executor = ACNAgentExecutor(
        registry=registry,
        router=router,
        broadcast=broadcast,
        subnet_manager=subnet_manager,
    )

    # Use Redis-based task store for persistence
    task_store = RedisTaskStore(redis, key_prefix="a2a:tasks:")

    # Create request handler
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
    )

    # Create ACN Agent Card
    agent_card = AgentCard(
        protocol_version="0.4.0",
        name="ACN Infrastructure Agent",
        version="0.1.0",
        description=(
            "Agent Collaboration Network provides infrastructure services: "
            "broadcast, discovery, routing, and subnet gateway"
        ),
        url=f"{settings.gateway_base_url}/a2a/jsonrpc",
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
            state_transition_history=False,
        ),
        default_input_modes=["text"],
        default_output_modes=["text"],
        skills=[
            AgentSkill(
                id="acn:broadcast",
                name="Multi-Agent Broadcasting",
                description="Broadcast messages to multiple agents simultaneously",
                tags=["infrastructure", "broadcast", "messaging"],
            ),
            AgentSkill(
                id="acn:discovery",
                name="Agent Discovery",
                description="Find agents by skills and status",
                tags=["infrastructure", "discovery", "registry"],
            ),
            AgentSkill(
                id="acn:routing",
                name="Point-to-Point Routing",
                description="Route messages with logging and retry",
                tags=["infrastructure", "routing", "messaging"],
            ),
            AgentSkill(
                id="acn:subnet_routing",
                name="Subnet Gateway Routing",
                description="Route through subnets for NAT traversal",
                tags=["infrastructure", "routing", "gateway", "nat"],
            ),
        ],
    )

    # Create A2A FastAPI application
    a2a_app_builder = A2AFastAPIApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )

    # Build the FastAPI app
    a2a_app = a2a_app_builder.build(
        agent_card_url="/.well-known/agent-card.json",
        rpc_url="/jsonrpc",
    )

    logger.info(
        "a2a_app_created",
        endpoints=["/a2a/jsonrpc", "/a2a/jsonrpc/stream"],
        actions=["broadcast", "discover", "route", "subnet_route"],
    )

    return a2a_app


__all__ = ["ACNAgentExecutor", "create_a2a_app"]
