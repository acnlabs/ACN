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
from a2a.compat.v0_3.conversions import (  # type: ignore[import-untyped]
    to_compat_message,
    to_core_agent_card,
    to_core_task_artifact_update_event,
    to_core_task_status_update_event,
)
from a2a.compat.v0_3.types import (  # type: ignore[import-untyped]
    AgentCapabilities,
    AgentCard,
    AgentProvider,
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
from a2a.server.agent_execution import (  # type: ignore[import-untyped]
    AgentExecutor,
    RequestContext,
)
from a2a.server.events import EventQueue  # type: ignore[import-untyped]
from a2a.server.request_handlers import (  # type: ignore[import-untyped]
    DefaultRequestHandler,
)
from a2a.server.routes import (  # type: ignore[import-untyped]
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from fastapi import FastAPI
from redis.asyncio import Redis

from ...config import get_settings
from ...core.exceptions import PolicyRejected
from ...infrastructure.messaging import (
    BroadcastService,
    BroadcastStrategy,
    MessageRouter,
    SubnetManager,
)
from ...infrastructure.persistence.redis.a2a_task_store import RedisTaskStore
from ...monitoring import MetricsCollector
from ...services.agent_service import AgentService

settings = get_settings()

logger = structlog.get_logger()


# A2A protocol entry: anti-spoofing for the ``system:`` exemption
# ----------------------------------------------------------------
#
# PolicyCheckService grants an unconditional bypass to any sender whose
# id starts with ``system:`` (Phase 1 唯一豁免规则). The exemption
# assumes the caller has already proven they belong to the ACN
# control plane via ``X-Internal-Token`` + ``assert_system_caller``
# at ``POST /communication/internal/send``.
#
# The A2A protocol entry has neither of those gates — ``context.metadata``
# is just a free-form dict the client sets. Without sanitization, any
# external agent could put ``"from_agent": "system:fake"`` in metadata
# and bypass every closed recipient on the network. That would defeat
# the entire policy gate, so we collapse any ``system:`` value here
# back to a non-privileged sentinel before it reaches PolicyCheckService.
#
# Why "unknown" instead of e.g. caller's real agent id:
# - The A2A entry doesn't currently authenticate the caller (Phase 2
#   待决策 #8). Inventing a real agent id from an unauthenticated
#   field would be a different, subtler form of the same forging
#   problem. ``unknown`` is non-system, so it goes through the normal
#   ``open/closed`` branches — exactly the safe default.
# - The legitimate ACN-internal callers do NOT use the A2A protocol
#   entry — they use ``/communication/internal/send`` (token-gated).
#   Demoting ``system:`` here therefore breaks no real flow.
_A2A_SAFE_FROM_AGENT_FALLBACK = "unknown"


async def _enqueue_status_event(
    event_queue: EventQueue,
    event: TaskStatusUpdateEvent,
) -> None:
    if isinstance(event_queue, EventQueue):
        await event_queue.enqueue_event(to_core_task_status_update_event(event))
        return
    await event_queue.enqueue_event(event)


async def _enqueue_artifact_event(
    event_queue: EventQueue,
    event: TaskArtifactUpdateEvent,
) -> None:
    if isinstance(event_queue, EventQueue):
        await event_queue.enqueue_event(to_core_task_artifact_update_event(event))
        return
    await event_queue.enqueue_event(event)


def _safe_a2a_from_agent(context: RequestContext) -> str:
    """Return a sender id safe to hand to PolicyCheckService.

    Strips any client-supplied ``system:*`` value so the protocol
    entry cannot be used to forge the policy exemption. See the
    module-level note for the threat model.
    """
    metadata = getattr(context, "metadata", None) or {}
    raw = metadata.get("from_agent", _A2A_SAFE_FROM_AGENT_FALLBACK)
    if not isinstance(raw, str):
        return _A2A_SAFE_FROM_AGENT_FALLBACK
    if raw.startswith("system:"):
        # Log at WARNING-level so abuse attempts surface even when
        # they're successfully demoted — without this an attacker
        # could probe the gate silently.
        logger.warning(
            "a2a_from_agent_system_namespace_demoted",
            received=raw,
            context_id=getattr(context, "context_id", None),
            task_id=getattr(context, "task_id", None),
        )
        return _A2A_SAFE_FROM_AGENT_FALLBACK
    return raw


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
        agent_service: AgentService,
        router: MessageRouter,
        broadcast: BroadcastService,
        subnet_manager: SubnetManager,
        metrics: MetricsCollector | None = None,
    ):
        """Initialize ACN Agent Executor

        Args:
            agent_service: ACN AgentService for agent discovery
                (replaces the legacy ``AgentRegistry`` — see
                ``docs/agent-registry-removal.md`` for the migration
                record and the ``find_agent`` vs ``get_agent`` contract).
            router: Message Router for point-to-point routing
            broadcast: Broadcast Service for multi-agent messaging
            subnet_manager: Subnet Manager for subnet gateway routing
            metrics: Optional MetricsCollector for the
                ``acn_messages_rejected_by_policy_total`` counter.
                Optional so a partial bring-up (e.g. unit tests
                instantiating the executor in isolation) keeps
                working — production lifespan injects a real
                MetricsCollector via ``create_a2a_app``.
        """
        self.agent_service = agent_service
        self.router = router
        self.broadcast = broadcast
        self.subnet_manager = subnet_manager
        self.metrics = metrics

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
            core_message = context.message
            if not core_message:
                await self._send_status(
                    event_queue,
                    context,
                    TaskState.failed,
                    "No message provided",
                    final=True,
                )
                return
            message = to_compat_message(core_message)

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

    async def _inc_policy_rejected_metric(
        self,
        *,
        reason: str,
        context: RequestContext | None = None,
    ) -> None:
        """Bump ``acn_messages_rejected_by_policy_total`` for an
        A2A-protocol-side rejection.

        Path label is fixed to ``"a2a"`` so all A2A-entry rejections
        (route + subnet_routing + future actions) bucket together —
        operators rarely need to differentiate the sub-handlers and
        keeping label cardinality low matters more than fine-grained
        slicing.

        Failures here are intentionally swallowed: metrics are
        best-effort observability and a Redis blip at counter-write
        time must not turn a clean ``TaskState.rejected`` into an
        upstream-visible error. Mirrors the same tolerance the REST
        path uses (see acn/routes/communication.py and
        acn/routes/registry.py:_proxy_to_agent).

        ``context`` is optional but strongly recommended — when
        passed, the failure-warning carries ``context_id`` /
        ``task_id`` so ops can correlate a metric-inc-failure
        burst with the specific A2A tasks affected. Without it,
        debugging requires cross-referencing log timestamps with
        the route handler logs, which doesn't scale during a
        Redis incident.
        """
        if self.metrics is None:
            return
        try:
            await self.metrics.inc_counter(
                "messages_rejected_by_policy_total",
                labels={"path": "a2a", "reason": reason},
            )
        except Exception as metric_exc:
            logger.warning(
                "a2a_policy_metric_inc_failed",
                reason=reason,
                context_id=getattr(context, "context_id", None),
                task_id=getattr(context, "task_id", None),
                metric_error=str(metric_exc),
            )

    async def _send_policy_rejected_status(
        self,
        event_queue: EventQueue,
        context: RequestContext,
        *,
        reason: str,
        reject_reason: str | None,
        target_id: str | None = None,
    ) -> None:
        """Send a structured ``TaskState.rejected`` event for a
        ``communication_policy`` denial.

        Why a dedicated path instead of ``_send_status(failed, str(e))``:

        Pre-fix, PolicyRejected fell through to a generic
        ``except Exception`` and the rejection details were
        flattened into a free-form ``"Routing failed: <repr>"``
        TextPart message. Clients that wanted to differentiate
        "denied by recipient policy" from "upstream 500" had to
        substring-match — fragile, locale-bound, and the policy
        ``reject_reason`` (which is operator-controlled string)
        was at the mercy of whatever `__str__` PolicyRejected
        produced.

        Post-fix, we emit ``TaskState.rejected`` (an A2A spec
        state, not a string) with a ``DataPart`` containing the
        same shape ``/communication/send`` returns over HTTP:

            {"detail": "communication_rejected",
             "reason": "policy_closed" | "policy_unknown_mode",
             "reject_reason": <operator string or None>,
             "target_id": <recipient agent id>}

        That makes the rejection contract uniform across the
        single-send REST path, the proxy reverse-call path, and
        the A2A protocol path.
        """
        message_obj = Message(
            role=Role.agent,
            message_id=str(uuid.uuid4()),
            parts=[
                DataPart(
                    data={
                        "detail": "communication_rejected",
                        "reason": reason,
                        "reject_reason": reject_reason,
                        **({"target_id": target_id} if target_id else {}),
                    }
                )
            ],
        )
        status = TaskStatus(state=TaskState.rejected, message=message_obj)
        event = TaskStatusUpdateEvent(
            task_id=context.task_id,
            context_id=context.context_id,
            status=status,
            final=True,
        )
        await _enqueue_status_event(event_queue, event)

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
        await _enqueue_status_event(event_queue, event)

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
        await _enqueue_artifact_event(event_queue, event)

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
        await self._send_status(event_queue, context, TaskState.working, "Broadcasting message")

        # Extract broadcast parameters
        params = self._extract_data_from_message(message)
        target_agents = params.get("target_agents", [])
        target_tags = params.get("target_tags")
        strategy = params.get("strategy", "parallel")
        broadcast_message_text = params.get("message", "")

        from_agent = _safe_a2a_from_agent(context)

        # Build broadcast message
        broadcast_msg = Message(
            role=Role.user,
            message_id=str(uuid.uuid4()),
            parts=[TextPart(text=broadcast_message_text)],
        )

        try:
            # Execute broadcast
            if target_tags:
                # Broadcast by skill
                result = await self.broadcast.send_by_tag(
                    from_agent=from_agent,
                    tags=target_tags,
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
                            "target_count": len(target_agents) if target_agents else len(result),
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
        await self._send_status(event_queue, context, TaskState.working, "Discovering agents")

        params = self._extract_data_from_message(message)
        tags = params.get("tags", [])
        status = params.get("status", "online")

        try:
            agents = await self.agent_service.search_agents(
                tags=tags,
                status=status,
            )

            # Agent entities no longer carry a ``status`` field — that's
            # the whole point of the implicit-heartbeat refactor (Redis
            # alive key is the single source of truth). For the A2A
            # discovery wire format we still want a status string per
            # agent, so we batch-fetch liveness from the repository in
            # one Redis round-trip and project it back as the legacy
            # "online" / "offline" literal.
            alive_ids = await self.agent_service.repository.filter_alive(
                [agent.agent_id for agent in agents]
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
                                    "tags": agent.tags,
                                    "status": (
                                        "online"
                                        if agent.agent_id in alive_ids
                                        else "offline"
                                    ),
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
        await self._send_status(event_queue, context, TaskState.working, "Routing message")

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

        from_agent = _safe_a2a_from_agent(context)

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

        except PolicyRejected as e:
            # See _send_policy_rejected_status for why this is its
            # own branch instead of falling through to ``failed``.
            logger.info(
                "routing_rejected_by_policy",
                target=target_agent,
                from_agent=from_agent,
                reason=e.reason,
            )
            await self._inc_policy_rejected_metric(reason=e.reason, context=context)
            await self._send_policy_rejected_status(
                event_queue,
                context,
                reason=e.reason,
                reject_reason=e.reject_reason,
                target_id=target_agent,
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
        await self._send_status(event_queue, context, TaskState.working, "Routing through subnet")

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

        # Plumb the caller agent id from A2A request metadata so the
        # subnet gateway can apply ``communication_policy``. We pass it
        # through ``_safe_a2a_from_agent`` so a client-supplied
        # ``system:`` value is demoted to ``unknown`` rather than
        # forging the global exemption. The exemption rule is
        # documented in PolicyCheckService.SYSTEM_SENDER_PREFIX.
        from_agent = _safe_a2a_from_agent(context)

        try:
            # Forward through subnet
            response = await self.subnet_manager.forward_request(
                subnet_id, agent_id, message_content, from_agent=from_agent
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

        except PolicyRejected as e:
            logger.info(
                "subnet_routing_rejected_by_policy",
                subnet=subnet_id,
                target=agent_id,
                from_agent=from_agent,
                reason=e.reason,
            )
            await self._inc_policy_rejected_metric(reason=e.reason, context=context)
            await self._send_policy_rejected_status(
                event_queue,
                context,
                reason=e.reason,
                reject_reason=e.reject_reason,
                target_id=agent_id,
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
    agent_service: AgentService,
    router: MessageRouter,
    broadcast: BroadcastService,
    subnet_manager: SubnetManager,
    redis: Redis,
    metrics: MetricsCollector | None = None,
) -> FastAPI:
    """Create A2A FastAPI application for ACN

    Args:
        agent_service: ACN AgentService for discovery (replaces the
            legacy ``AgentRegistry`` — see
            ``docs/agent-registry-removal.md`` for the migration record).
        router: Message Router
        broadcast: Broadcast Service
        subnet_manager: Subnet Manager
        redis: Redis client for task persistence
        metrics: Optional MetricsCollector — when set, the executor
            increments ``acn_messages_rejected_by_policy_total`` on
            policy denials, matching the dimension contract used by
            the REST routes.

    Returns:
        FastAPI app with A2A endpoints at /a2a/jsonrpc
    """
    # Create ACN agent executor
    executor = ACNAgentExecutor(
        agent_service=agent_service,
        router=router,
        broadcast=broadcast,
        subnet_manager=subnet_manager,
        metrics=metrics,
    )

    # Use Redis-based task store for persistence
    task_store = RedisTaskStore(redis, key_prefix="a2a:tasks:")

    # Create ACN Agent Card in the v0.3 compatibility model, then convert
    # it to the protobuf shape expected by a2a-sdk 1.x server routes.
    compat_agent_card = AgentCard(
        protocol_version=settings.a2a_protocol_version,
        name="ACN Infrastructure Agent",
        version=settings.service_version,
        description=(
            "Agent Collaboration Network provides infrastructure services: "
            "broadcast, discovery, routing, and subnet gateway"
        ),
        url=f"{settings.gateway_base_url}/a2a/jsonrpc",
        provider=AgentProvider(
            organization="acnlabs",
            url="https://acnlabs.dev",
        ),
        documentation_url=f"{settings.gateway_base_url}/skill.md",
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
            state_transition_history=False,
        ),
        default_input_modes=["text", "application/json"],
        default_output_modes=["text", "application/json"],
        skills=[
            AgentSkill(
                id="acn:broadcast",
                name="Multi-Agent Broadcasting",
                description="Broadcast messages to multiple agents simultaneously",
                tags=["infrastructure", "broadcast", "messaging"],
                input_modes=["application/json"],
                output_modes=["application/json"],
            ),
            AgentSkill(
                id="acn:discovery",
                name="Agent Discovery",
                description="Find agents by skills and status",
                tags=["infrastructure", "discovery", "registry"],
                input_modes=["application/json"],
                output_modes=["application/json"],
            ),
            AgentSkill(
                id="acn:routing",
                name="Point-to-Point Routing",
                description="Route messages between agents with logging and retry",
                tags=["infrastructure", "routing", "messaging"],
                input_modes=["text", "application/json"],
                output_modes=["text", "application/json"],
            ),
            AgentSkill(
                id="acn:subnet_routing",
                name="Subnet Gateway Routing",
                description="Route through subnets for NAT traversal and private network access",
                tags=["infrastructure", "routing", "gateway", "nat"],
                input_modes=["application/json"],
                output_modes=["application/json"],
            ),
        ],
    )
    agent_card = to_core_agent_card(compat_agent_card)

    # Create request handler
    request_handler = DefaultRequestHandler(
        agent_card=agent_card,
        agent_executor=executor,
        task_store=task_store,
    )

    # Build the FastAPI app using the a2a-sdk 1.x Starlette routes.
    a2a_app = FastAPI()
    for route in [
        *create_agent_card_routes(
            agent_card=agent_card,
            card_url="/.well-known/agent-card.json",
        ),
        *create_jsonrpc_routes(
            request_handler=request_handler,
            rpc_url="/jsonrpc",
            enable_v0_3_compat=True,
        ),
    ]:
        a2a_app.router.routes.append(route)

    logger.info(
        "a2a_app_created",
        endpoints=["/a2a/jsonrpc", "/a2a/jsonrpc/stream"],
        actions=["broadcast", "discover", "route", "subnet_route"],
    )

    return a2a_app


__all__ = ["ACNAgentExecutor", "create_a2a_app"]
