"""
Broadcast Service

ACN Communication Layer component for multi-agent messaging.
Broadcasts messages to multiple agents simultaneously.

Use cases:
- @mention multiple agents in group chat
- Notify all agents in a project
- Broadcast status updates
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

import redis.asyncio as redis

# Official A2A SDK
from a2a.types import Message  # type: ignore[import-untyped]

from ...core.exceptions import PolicyRejected
from ...security import safe_external_error
from ..persistence.redis.registry import AgentRegistry
from .message_router import MessageRouter

logger = logging.getLogger(__name__)


class BroadcastStrategy(StrEnum):
    """Broadcast delivery strategy"""

    PARALLEL = "parallel"  # Send to all simultaneously
    SEQUENTIAL = "sequential"  # Send one by one
    BEST_EFFORT = "best_effort"  # Continue even if some fail


@dataclass
class BroadcastResult:
    """Result of a broadcast operation"""

    broadcast_id: str
    total: int
    success: int
    failed: int
    results: dict[str, Any]  # agent_id -> result or error

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "broadcast_id": self.broadcast_id,
            "total": self.total,
            "success": self.success,
            "failed": self.failed,
            "success_rate": self.success_rate,
            "results": self.results,
        }


class BroadcastService:
    """
    Broadcast Service

    Sends messages to multiple agents simultaneously.
    Built on top of Message Router.

    Usage:
        broadcast = BroadcastService(router, redis_client)

        # Broadcast to specific agents
        result = await broadcast.send(
            from_agent="chat-service",
            to_agents=["cursor-agent", "figma-agent", "backend-agent"],
            message=A2AMessage.notification(
                notification_type="group_chat_mention",
                content="@all 项目开始了！",
                metadata={"chat_id": "chat-123"}
            )
        )

        # Broadcast to agents by tag
        result = await broadcast.send_by_tag(
            from_agent="taskmaster",
            tags=["frontend"],
            message=A2AMessage.text("前端任务更新")
        )
    """

    def __init__(
        self,
        router: MessageRouter,
        redis_client: redis.Redis,
        registry: AgentRegistry | None = None,
    ):
        """
        Initialize Broadcast Service

        Args:
            router: Message Router for delivery
            redis_client: Redis for logging
            registry: ACN Registry (optional, uses router's if not provided)
        """
        self.router = router
        self.redis = redis_client
        self.registry = registry or router.registry

        logger.info("Broadcast Service initialized")

    async def send(
        self,
        from_agent: str,
        to_agents: list[str],
        message: Message,
        strategy: BroadcastStrategy = BroadcastStrategy.PARALLEL,
    ) -> BroadcastResult:
        """
        Broadcast message to multiple agents

        Args:
            from_agent: Source agent/service ID
            to_agents: List of target agent IDs
            message: A2A message to broadcast
            strategy: Delivery strategy

        Returns:
            BroadcastResult with delivery status
        """
        broadcast_id = uuid4().hex[:12]

        logger.info(
            f"[{broadcast_id}] Broadcasting from {from_agent} "
            f"to {len(to_agents)} agents, strategy={strategy}"
        )

        # Log broadcast start
        await self._log_broadcast(
            broadcast_id=broadcast_id,
            from_agent=from_agent,
            to_agents=to_agents,
            message=message,
            status="started",
        )

        results: dict[str, Any] = {}

        if strategy == BroadcastStrategy.PARALLEL:
            results = await self._send_parallel(from_agent, to_agents, message)
        elif strategy == BroadcastStrategy.SEQUENTIAL:
            results = await self._send_sequential(from_agent, to_agents, message)
        else:  # BEST_EFFORT
            results = await self._send_best_effort(from_agent, to_agents, message)

        # Calculate stats
        # ----------------------------------------------------------------
        # ``router.route()`` returns either:
        #   - a dict like ``{"status": "inbox", ...}`` (offline target)
        #   - an a2a SDK ``SendMessageResponse`` Pydantic model
        #     (online target — the "happy path" return type)
        #   - a dict ``{"error": ...}`` (delivery failure, see
        #     ``send_one`` in this file)
        #   - a dict ``{"status": "rejected", ...}`` (Phase 1 — see
        #     ``PolicyRejected`` branches in this file)
        #
        # The pre-Phase-1 success rule was ``"error" not in r``, which
        # implicitly counted Pydantic models as success because their
        # default ``__contains__`` returns ``False`` for unknown keys.
        # Phase 1 needs to *additionally* exclude policy rejections
        # without breaking the SendMessageResponse case.
        #
        # The contract therefore: a result is "failed" only when it
        # is a dict that *explicitly* signals failure (has ``"error"``
        # OR ``status == "rejected"``). Any other shape — including
        # SendMessageResponse — is treated as success, preserving the
        # historical implicit invariant.
        def _is_failed(r: object) -> bool:
            if not isinstance(r, dict):
                # SendMessageResponse / other Pydantic model — the
                # only way ``router.route`` reaches here is via a
                # successful in-line delivery, so this is success.
                return False
            return "error" in r or r.get("status") == "rejected"

        success = sum(1 for r in results.values() if not _is_failed(r))
        failed = len(results) - success

        # Log broadcast complete
        await self._log_broadcast(
            broadcast_id=broadcast_id,
            from_agent=from_agent,
            to_agents=to_agents,
            message=message,
            status="completed",
            results=results,
        )

        result = BroadcastResult(
            broadcast_id=broadcast_id,
            total=len(to_agents),
            success=success,
            failed=failed,
            results=results,
        )

        logger.info(f"[{broadcast_id}] Broadcast completed: {success}/{len(to_agents)} success")

        return result

    async def send_by_tag(
        self,
        from_agent: str,
        tags: list[str],
        message: Message,
        status_filter: str | None = "online",
        strategy: BroadcastStrategy = BroadcastStrategy.PARALLEL,
    ) -> BroadcastResult:
        """
        Broadcast to all agents with specific tags

        Args:
            from_agent: Source agent/service ID
            tags: Required tags
            message: A2A message to broadcast
            status_filter: Filter by status (None for all)
            strategy: Delivery strategy

        Returns:
            BroadcastResult
        """
        # Discover agents with tags
        agents = await self.registry.search_agents(
            tags=tags,
            status=status_filter,
        )

        if not agents:
            logger.warning(f"No agents found with tags: {tags}")
            return BroadcastResult(
                broadcast_id=uuid4().hex[:12],
                total=0,
                success=0,
                failed=0,
                results={},
            )

        to_agents = [agent.agent_id for agent in agents]

        logger.info(f"Found {len(to_agents)} agents with tags {tags}: {to_agents}")

        return await self.send(
            from_agent=from_agent,
            to_agents=to_agents,
            message=message,
            strategy=strategy,
        )

    async def send_to_project(
        self,
        from_agent: str,
        project_id: str,
        message: Message,
        exclude: list[str] | None = None,
    ) -> BroadcastResult:
        """
        Broadcast to all agents in a project

        Args:
            from_agent: Source agent/service ID
            project_id: Project ID to broadcast to
            message: A2A message
            exclude: Agent IDs to exclude

        Returns:
            BroadcastResult
        """
        # Get all agents in project
        # This requires project metadata in Registry
        # For now, use metadata search
        agents = await self.registry.search_agents(metadata={"project_id": project_id})

        to_agents = [agent.agent_id for agent in agents if agent.agent_id not in (exclude or [])]

        return await self.send(
            from_agent=from_agent,
            to_agents=to_agents,
            message=message,
        )

    async def _send_parallel(
        self,
        from_agent: str,
        to_agents: list[str],
        message: Message,
    ) -> dict[str, Any]:
        """Send to all agents in parallel"""

        async def send_one(agent_id: str) -> tuple:
            try:
                result = await self.router.route(
                    from_agent=from_agent,
                    to_agent=agent_id,
                    message=message,
                )
                return agent_id, result
            except PolicyRejected as e:
                # Policy rejection is a *normal* fan-out outcome — a
                # ``closed`` recipient in the target set is expected
                # to be skipped, not treated as an error. We mirror
                # MessageService.broadcast_message's per-target
                # contract here so any client consuming either
                # broadcast implementation sees the same shape:
                #
                #   {"status": "rejected", "reason": ..., "reject_reason": ...}
                #
                # No ``"error"`` key — that's reserved for actual
                # delivery failures (network / 5xx / etc.) and feeds
                # the BroadcastResult.success counter.
                logger.info(
                    f"Target {agent_id} rejected by policy: "
                    f"reason={e.reason}"
                )
                return agent_id, {
                    "status": "rejected",
                    "reason": e.reason,
                    "reject_reason": e.reject_reason,
                }
            except Exception as e:
                logger.error(f"Failed to send to {agent_id}: {e}")
                # M12: per-target error goes into the broadcast result
                # map that is returned to the API caller. Sanitise so
                # the receiver never sees the target agent's endpoint
                # URL or any raw upstream response body.
                return agent_id, {"error": safe_external_error(e)}

        # Execute all in parallel
        tasks = [send_one(agent_id) for agent_id in to_agents]
        results_list = await asyncio.gather(*tasks)

        return dict(results_list)

    async def _send_sequential(
        self,
        from_agent: str,
        to_agents: list[str],
        message: Message,
    ) -> dict[str, Any]:
        """Send to agents one by one.

        SEQUENTIAL semantics: stop on the first delivery *failure*
        (the historical contract — e.g. a 5xx/timeout that may signal
        a systemic issue and risks amplifying load by continuing).

        Policy rejection is **not** a delivery failure: it's the
        recipient explicitly opting out of inbound traffic, which is
        expected for individual targets and not a signal that the
        rest of the broadcast will also fail. So a PolicyRejected
        target is recorded with ``status: "rejected"`` and the loop
        moves on, matching MessageService.broadcast_message's
        behaviour. Without this carve-out a single closed recipient
        in a SEQUENTIAL set would silently abort delivery to every
        target after it.
        """
        results = {}

        for agent_id in to_agents:
            try:
                result = await self.router.route(
                    from_agent=from_agent,
                    to_agent=agent_id,
                    message=message,
                )
                results[agent_id] = result
            except PolicyRejected as e:
                logger.info(
                    f"Target {agent_id} rejected by policy: "
                    f"reason={e.reason}"
                )
                results[agent_id] = {
                    "status": "rejected",
                    "reason": e.reason,
                    "reject_reason": e.reject_reason,
                }
                # Do NOT break — see method docstring for rationale.
                continue
            except Exception as e:
                logger.error(f"Failed to send to {agent_id}: {e}")
                # M12: see _send_parallel.send_one — sanitise before the
                # error reaches the API response.
                results[agent_id] = {"error": safe_external_error(e)}
                # Stop on first delivery failure in sequential mode.
                break

        return results

    async def _send_best_effort(
        self,
        from_agent: str,
        to_agents: list[str],
        message: Message,
    ) -> dict[str, Any]:
        """Send to all agents, continue even on failures."""
        results = {}

        for agent_id in to_agents:
            try:
                result = await self.router.route(
                    from_agent=from_agent,
                    to_agent=agent_id,
                    message=message,
                )
                results[agent_id] = result
            except PolicyRejected as e:
                logger.info(
                    f"Target {agent_id} rejected by policy: "
                    f"reason={e.reason}"
                )
                results[agent_id] = {
                    "status": "rejected",
                    "reason": e.reason,
                    "reject_reason": e.reject_reason,
                }
            except Exception as e:
                logger.error(f"Failed to send to {agent_id}: {e}")
                # M12: see _send_parallel.send_one — sanitise before the
                # error reaches the API response.
                results[agent_id] = {"error": safe_external_error(e)}
                # Continue despite failure

        return results

    async def _log_broadcast(
        self,
        broadcast_id: str,
        from_agent: str,
        to_agents: list[str],
        message: Message,
        status: str,
        results: dict[str, Any] | None = None,
    ):
        """Log broadcast to Redis"""
        # Serialize message
        if hasattr(message, "model_dump"):
            msg_data = message.model_dump()
        elif hasattr(message, "to_dict"):
            msg_data = message.to_dict()
        else:
            msg_data = str(message)

        log_entry = {
            "broadcast_id": broadcast_id,
            "from_agent": from_agent,
            "to_agents": to_agents,
            "message": msg_data,
            "status": status,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        if results:
            # Serialize results (may contain Message objects)
            serialized_results = {}
            for agent_id, result in results.items():
                if hasattr(result, "model_dump"):
                    serialized_results[agent_id] = result.model_dump()
                elif isinstance(result, dict):
                    serialized_results[agent_id] = result
                else:
                    serialized_results[agent_id] = str(result)
            log_entry["results"] = serialized_results

        await self.redis.setex(
            f"acn:broadcast:{broadcast_id}",
            24 * 60 * 60,  # 24 hours
            __import__("json").dumps(log_entry),
        )

    async def get_broadcast_status(
        self,
        broadcast_id: str,
    ) -> dict[str, Any] | None:
        """
        Get broadcast status by ID

        Args:
            broadcast_id: Broadcast ID

        Returns:
            Broadcast log entry or None
        """
        data = await self.redis.get(f"acn:broadcast:{broadcast_id}")
        if data:
            return __import__("json").loads(data)
        return None
