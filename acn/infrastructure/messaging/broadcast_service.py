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
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import redis.asyncio as redis

# Official A2A SDK
from a2a.compat.v0_3.types import Message  # type: ignore[import-untyped]

from ...core.exceptions import AgentNotFoundException, PolicyRejected
from ...core.interfaces import IAgentRepository
from ...security import safe_external_error
from .message_router import MessageRouter

if TYPE_CHECKING:
    from ...services.agent_service import AgentService

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
        agent_service: "AgentService | None" = None,
        agent_repository: IAgentRepository | None = None,
    ):
        """
        Initialize Broadcast Service.

        Args:
            router: Message Router for delivery.
            redis_client: Redis for broadcast log persistence.
            agent_service: AgentService for discovery (optional — uses
                router's if omitted). Replaces the legacy
                ``AgentRegistry`` injection that drove
                ``send_by_tag`` / ``send_to_project``; see audit
                report §AgentRegistry-parallel-implementation.
            agent_repository: Clean-architecture agent repository
                (PG or Redis impl). Enables the unified
                :py:meth:`broadcast` entry that replaced
                ``MessageService.broadcast_message`` (Phase 2 Group C
                #9 / review v2 P1 #7 — see
                ``docs/features/acn-communication-economic-model.md``
                L608–L614). When ``None``, only the lower-level
                ``send`` / ``send_by_tag`` / ``send_to_project`` APIs
                are usable; calls to :py:meth:`broadcast` will raise.
        """
        self.router = router
        self.redis = redis_client
        self.agent_service = agent_service or router.agent_service
        self.agent_repository = agent_repository

        logger.info("Broadcast Service initialized")

    async def broadcast(
        self,
        *,
        from_agent: str,
        message: Message,
        target_agents: list[str] | None = None,
        subnet_id: str | None = None,
        tags: list[str] | None = None,
        strategy: BroadcastStrategy = BroadcastStrategy.PARALLEL,
    ) -> "BroadcastResult":
        """Unified broadcast entry — used by the HTTP communication routes.

        Phase 2 Group C #9 / review v2 P1 #7 (see
        ``docs/features/acn-communication-economic-model.md``
        L608–L614) collapsed the previous double-track:

        * ``MessageService.broadcast_message`` (HTTP path — sequential,
          no ``broadcast_id``, no Redis log persistence) — DELETED in
          this convergence.
        * ``BroadcastService.send`` / ``send_by_tag`` (A2A path — real
          parallel + ``broadcast_id`` + persistence) — kept as the
          lower-level API, still used directly by
          ``ACNAgentExecutor._handle_broadcast``.

        This method is the high-level entry the HTTP routes now call.
        It performs the same target resolution that
        ``MessageService.broadcast_message`` used to do (so existing
        HTTP clients keep observing the same fan-out semantics) but
        gets the BroadcastResult contract for free — including a
        first-class ``broadcast_id`` exposed to the HTTP caller for
        traceability.

        Selector precedence (matches the old HTTP behaviour exactly):

        1. ``target_agents`` — explicit list, used verbatim;
        2. ``subnet_id`` — resolved via
           ``agent_repository.find_by_subnet``;
        3. ``tags`` — resolved via ``agent_repository.find_by_tags``
           (defaults to ``status="online"``);
        4. none of the above — broadcast to **all** agents
           (``find_all``).

        The sender is auto-filtered out of the resolved target set so
        a ``broadcast`` from an agent that happens to be in its own
        subnet/tag does not echo back to itself.

        Args:
            from_agent: Sender agent id. Existence is verified up
                front; missing senders get ``AgentNotFoundException``
                (mapped to HTTP 404 by the route).
            message: A2A message to deliver.
            target_agents: Explicit recipient list (highest priority).
            subnet_id: Restrict fan-out to one subnet.
            tags: Restrict fan-out to agents matching tags.
            strategy: Delivery strategy (parallel / sequential /
                best_effort). See :py:class:`BroadcastStrategy`.

        Returns:
            :py:class:`BroadcastResult` — with persisted
            ``broadcast_id``, per-target ``results`` dict, and stats.
        """
        if self.agent_repository is None:
            raise RuntimeError(
                "BroadcastService.broadcast() requires agent_repository "
                "to be wired at construction time. Production lifespan "
                "must inject it via init_services(); test fixtures "
                "should pass the same mock used elsewhere."
            )

        sender = await self.agent_repository.find_by_id(from_agent)
        if sender is None:
            raise AgentNotFoundException(f"Sender agent {from_agent} not found")

        if target_agents:
            to_ids = list(target_agents)
        elif subnet_id:
            agents = await self.agent_repository.find_by_subnet(subnet_id)
            to_ids = [a.agent_id for a in agents]
        elif tags:
            agents = await self.agent_repository.find_by_tags(tags)
            to_ids = [a.agent_id for a in agents]
        else:
            agents = await self.agent_repository.find_all()
            to_ids = [a.agent_id for a in agents]

        # Filter sender from the resolved set — same as the legacy
        # MessageService.broadcast_message behaviour. Explicit
        # ``target_agents=[from_agent]`` still gets filtered (caller
        # asked to message themselves; broadcast semantics say no).
        to_ids = [aid for aid in to_ids if aid != from_agent]

        if not to_ids:
            logger.warning(
                "broadcast_no_targets "
                f"from={from_agent} subnet={subnet_id!r} tags={tags!r}"
            )
            # Empty fan-out still produces a BroadcastResult so callers
            # have a stable shape (and a broadcast_id for the audit
            # trail, even though nothing was actually sent).
            return BroadcastResult(
                broadcast_id=uuid4().hex[:12],
                total=0,
                success=0,
                failed=0,
                results={},
            )

        return await self.send(
            from_agent=from_agent,
            to_agents=to_ids,
            message=message,
            strategy=strategy,
        )

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
        # Discover agents with tags.
        # ``AgentService.search_agents`` uses the literal "all" to
        # disable the liveness filter (instead of ``None``), so we
        # translate the legacy ``status_filter=None`` form here.
        agents = await self.agent_service.search_agents(
            tags=tags,
            status=status_filter or "all",
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
        # NOTE: ``send_to_project`` was wired against
        # ``AgentRegistry.search_agents(metadata=...)`` — a parameter
        # AgentRegistry never actually accepted (the call has been a
        # silent ``TypeError`` since introduction; see audit report
        # §dead-call-sites). The successor ``AgentService.search_agents``
        # also has no metadata-based search by design (project
        # membership belongs in a dedicated index, not in the agent
        # blob). Until a real project-membership index ships, fail
        # loudly so callers can't believe this path works.
        del exclude  # parameter retained for ABI compat with stale callers
        raise NotImplementedError(
            "BroadcastService.send_to_project requires a project-membership "
            "index that is not yet implemented; see audit report "
            f"§dead-call-sites (project_id={project_id!r})"
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
