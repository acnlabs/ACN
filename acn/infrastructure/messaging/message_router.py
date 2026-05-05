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
from inspect import isawaitable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
import redis.asyncio as redis

# Official A2A SDK
from a2a.client import Client, ClientConfig, ClientFactory  # type: ignore[import-untyped]
from a2a.compat.v0_3.conversions import (  # type: ignore[import-untyped]
    to_compat_stream_response,
    to_core_send_message_request,
)
from a2a.compat.v0_3.types import (  # type: ignore[import-untyped]
    DataPart,
    Message,
    MessageSendParams,
    Part,
    Role,
    SendMessageRequest,
    TextPart,
)
from a2a.types.a2a_pb2 import (  # type: ignore[import-untyped]
    AgentCapabilities,
    AgentCard,
    AgentInterface,
)

from ...config import get_settings
from ...core.exceptions import PolicyRejected
from ...security import SSRFViolation, safe_resolve_target
from ..persistence.redis.registry import AgentRegistry


class AttentionFeeWrongModeError(Exception):
    """Raised by ``MessageRouter.route`` when ``attention_fee`` was
    supplied but the policy decision routes to inbox / rejection.

    Phase 3 attention_fee semantics only make sense for the manifest
    flow: there is no ack step on inbox traffic, so locking funds
    against an open-mode recipient would be a silent loss. We surface
    this as a typed exception (caught by the route handler and mapped
    to ``ATTENTION_FEE_REQUIRES_MANIFEST_MODE`` 4xx) so the sender
    learns immediately that no escrow lock was created.
    """

    def __init__(self, *, recipient_id: str, actual_route: str) -> None:
        super().__init__(
            f"attention_fee requires manifest mode; "
            f"recipient {recipient_id!r} routes to {actual_route!r}"
        )
        self.recipient_id = recipient_id
        self.actual_route = actual_route

# ``PolicyCheckService`` is only referenced for type hints; importing
# the submodule directly (rather than ``from ...services import ...``)
# avoids triggering ``services/__init__.py`` during this module's
# import, which is what would cause a circular dependency
# (services -> infrastructure -> services).
if TYPE_CHECKING:
    from ...services.allowlist_service import AllowlistService
    from ...services.policy_service import PolicyCheckService
    from .manifest_dispatcher import ManifestDispatcher

logger = logging.getLogger(__name__)


def _agent_delivery_endpoint(agent_info: Any) -> str:
    """Return the direct A2A JSON-RPC delivery URL from new or legacy fields."""
    a2a_endpoint = getattr(agent_info, "a2a_endpoint", None)
    if isinstance(a2a_endpoint, str) and a2a_endpoint:
        return a2a_endpoint
    return agent_info.endpoint


# Global message audit trail. Stored as a Redis stream (not one string
# key per route_id) so memory is bounded by MAXLEN regardless of
# traffic. The previous `acn:messages:log:{route_id}` design grew at
# QPS × 604800 s × ~1 KB and had no consumers — it was pure debug
# trace that nothing ever read, so collapsing it to a single capped
# stream is safe.
MESSAGE_LOG_STREAM_KEY = "acn:messages:log:stream"
_INBOX_CAP = 50
_INBOX_TTL = 30 * 24 * 3600  # 30 days
# ~100 K entries × ~1 KB/entry ≈ 100 MB hard ceiling. Approximate
# trimming (XADD MAXLEN ~) lets Redis keep the stream in whole radix
# nodes so the cost is amortized O(1) per write.
MESSAGE_LOG_STREAM_MAXLEN = 100_000

# Per-(message_type) hard cap on registered handlers. The list is kept
# in memory (not Redis), but the same process might be long-lived and
# callers might re-register on module reload / hot swap. A cap keeps
# the fan-out bounded no matter what the caller does. 32 is well above
# any realistic use (most call sites register one handler per type).
MAX_HANDLERS_PER_TYPE = 32


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

        # Route with tag-based discovery
        response = await router.route_by_tag(
            from_agent="taskmaster",
            tags=["frontend", "react"],
            message=Message(role="user", parts=[TextPart(text="Implement UI")])
        )
    """

    def __init__(
        self,
        registry: AgentRegistry,
        redis_client: redis.Redis,
        policy_service: "PolicyCheckService | None" = None,
        manifest_dispatcher: "ManifestDispatcher | None" = None,
        allowlist_service: "AllowlistService | None" = None,
    ):
        """
        Initialize Message Router

        Args:
            registry: ACN Registry for agent discovery
            redis_client: Redis for logging and DLQ
            policy_service: Optional gateway-level access control. When
                provided, ``route()`` short-circuits inbound delivery
                with ``PolicyRejected`` for recipients whose
                ``communication_policy`` denies the sender. When
                ``None`` (e.g. legacy tests, scripts), the policy gate
                is bypassed and behaviour matches the pre-Phase-1
                rollout — that explicit opt-out is preserved so this
                change can land without rewiring every test fixture in
                a single PR.
            manifest_dispatcher: Phase 2 PR #1 manifest divert handler.
                Required when ``policy_service`` may emit
                ``PolicyDecision.route_to == "manifest"``; otherwise
                manifest-mode messages would surface a clear
                ``RuntimeError`` rather than silently fall through to
                inbox (which would defeat the recipient's opt-in).
                Defaulting to ``None`` keeps legacy tests working that
                don't exercise manifest mode at all. The dispatcher
                bundles ``ManifestService`` + WS + metrics, so this
                router only needs one collaborator reference.
            allowlist_service: Phase 2 PR #2 allowlist trust list.
                Required when ``policy_service`` may emit
                ``mode=allowlist`` decisions (i.e. once any agent has
                set their policy to that mode). The router threads
                ``AllowlistService.is_member`` into
                ``PolicyCheckService.check_inbound`` as the
                ``is_in_allowlist`` callback so the policy service
                stays a pure function (no IO dependencies on its
                own). When ``None`` and a recipient's policy is
                ``allowlist``, the policy service falls back to
                "divert to manifest" (its safety default) so the
                router does NOT crash — same accept-but-divert
                semantics as a normal non-member, just driven by the
                missing-collaborator path. See PR #2 plan P0-2 for
                the design rationale.
        """
        self.registry = registry
        self.redis = redis_client
        self.policy_service = policy_service
        self.manifest_dispatcher = manifest_dispatcher
        self.allowlist_service = allowlist_service

        # Cache of A2A clients by endpoint (capped to prevent unbounded growth)
        self._clients: dict[str, Client] = {}
        self._clients_max: int = 256

        # Message handlers for incoming messages
        self._handlers: dict[str, list[Callable]] = {}

        logger.info(
            "Message Router initialized (using official A2A SDK)",
        )

    async def _get_client(self, endpoint: str) -> Client:
        """
        Get or create A2A client for endpoint

        Args:
            endpoint: Agent A2A endpoint URL

        Returns:
            A2A client instance
        """
        # SSRF guard: resolve and verify the endpoint hostname BEFORE caching
        # the A2A client. Even though clients are cached per-endpoint URL,
        # we re-check on each cache miss; in practice that's once per (rare)
        # new agent + an invalidation when the cache evicts the entry.
        # ``allow_loopback`` follows ``dev_mode`` so local agents can keep
        # registering ``http://127.0.0.1:...`` endpoints in dev.
        settings = get_settings()
        try:
            await safe_resolve_target(endpoint, allow_loopback=settings.dev_mode)
        except SSRFViolation as e:
            logger.warning(
                "router_ssrf_blocked endpoint=%s reason=%s", endpoint, e
            )
            raise

        if endpoint not in self._clients:
            if len(self._clients) >= self._clients_max:
                # Evict the oldest entry to keep memory bounded
                oldest = next(iter(self._clients))
                try:
                    old_client = self._clients.pop(oldest)
                    await old_client.close()
                except Exception:
                    pass
            # ``follow_redirects=False``: a 3xx pointing to a private
            # address would otherwise sneak past the SSRF check above.
            httpx_client = httpx.AsyncClient(
                timeout=30.0,
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
                follow_redirects=False,
            )
            client_config = ClientConfig(
                httpx_client=httpx_client,
                streaming=False,
                supported_protocol_bindings=["JSONRPC"],
            )
            # Registered ACN endpoints are direct JSON-RPC targets, not
            # discovery base URLs. Build a minimal legacy card locally so the
            # a2a-sdk 1.x client uses its v0.3 compatibility transport without
            # first fetching endpoint/.well-known/agent-card.json.
            agent_card = AgentCard(
                supported_interfaces=[
                    AgentInterface(
                        url=endpoint,
                        protocol_binding="JSONRPC",
                        protocol_version="0.3.0",
                    )
                ],
                capabilities=AgentCapabilities(extended_agent_card=False),
                default_input_modes=[],
                default_output_modes=[],
                description="",
                skills=[],
                version="",
                name="",
            )
            self._clients[endpoint] = ClientFactory(client_config).create(
                agent_card,
            )
            logger.debug(f"Created A2A client for {endpoint}")

        return self._clients[endpoint]

    async def _send_message_with_client(
        self,
        client: Client,
        request: SendMessageRequest,
    ) -> Any:
        """Send a compat request through either a 1.x client or legacy test stub."""
        result = client.send_message(to_core_send_message_request(request))
        if hasattr(result, "__aiter__"):
            async for event in result:
                return to_compat_stream_response(event, request_id=request.id)
            return None
        if isawaitable(result):
            return await result
        return result

    async def close(self) -> None:
        """Close all cached A2A clients and their underlying httpx connections"""
        for endpoint, client in self._clients.items():
            try:
                await client.close()
            except Exception as e:
                logger.warning("failed_to_close_a2a_client", endpoint=endpoint, error=str(e))
        self._clients.clear()
        logger.info("message_router_closed", clients_cleared=True)

    async def route(
        self,
        from_agent: str,
        to_agent: str,
        message: Message,
        *,
        attention_fee: dict[str, Any] | None = None,
    ) -> Any:
        """
        Route an A2A message to a specific agent

        Args:
            from_agent: Source agent/service ID
            to_agent: Target agent ID
            message: A2A Message object (from a2a.types)
            attention_fee: Phase 3 economic-model field. When set,
                the sender is paying for the recipient's attention;
                the dispatcher locks the fee in escrow and only
                releases it on an explicit ack call. Only honoured
                when the policy decision routes to manifest — any
                other path raises ``AttentionFeeWrongModeError``
                so the caller cannot accidentally lock funds the
                recipient will never see (open/closed mode never
                triggers the ack flow).

        Returns:
            A2A response (Message or Task)

        Raises:
            ValueError: If target agent not found
            AttentionFeeWrongModeError: ``attention_fee`` was supplied
                but the policy decision routes to inbox / rejection.
            Exception: On delivery failure
        """
        route_id = uuid4().hex[:8]

        logger.info(f"[{route_id}] Routing: {from_agent} -> {to_agent}")

        # 1. Discover agent endpoint via ACN Registry
        agent_info = await self.registry.get_agent(to_agent)
        if not agent_info:
            raise ValueError(f"Agent not found in ACN Registry: {to_agent}")

        # 1.5. Gateway-level access control (Phase 1) + manifest divert (Phase 2 PR #1).
        #
        # We check policy as early as possible — *before* the offline
        # inbox path and *before* any HTTP work — so a recipient with
        # ``communication_policy.mode == "closed"`` cannot have messages
        # parked in their inbox waiting for them to come online. The
        # rejection is a hard short-circuit: no _log_message (kept for
        # delivered traffic), no _store_inbox, no _store_dlq.
        #
        # Phase 2 introduces ``PolicyDecision.route_to``: when set to
        # ``"manifest"`` the router writes a manifest entry and pushes
        # a ``MANIFEST_NOTIFICATION`` WS event instead of going through
        # the inbox/HTTP path. The send is treated as accepted from the
        # sender's perspective (status="manifest", no PolicyRejected),
        # which matches the "accept-but-divert" mental model of the
        # manifest mode.
        #
        # ``policy_service`` is optional during the rollout window so
        # legacy tests / scripts that build a router without one keep
        # working unchanged. Production wiring (acn/api.py) installs it.
        if self.policy_service is not None:
            # Bind the allowlist callback only when the service is
            # wired — keeping ``is_in_allowlist=None`` for the
            # rollout-opt-out path lets ``PolicyCheckService`` apply
            # its safety fallback (divert to manifest on
            # ``mode=allowlist`` without a callback) instead of
            # crashing on a config mismatch.
            is_in_allowlist = (
                self.allowlist_service.is_member
                if self.allowlist_service is not None
                else None
            )
            decision = await self.policy_service.check_inbound(
                sender_id=from_agent,
                recipient_id=to_agent,
                recipient_policy=agent_info.communication_policy,
                is_in_allowlist=is_in_allowlist,
            )
            if not decision.allow:
                # Same surface as the Phase 1 ``check_inbound_or_raise``
                # path; we just inline the raise here so we can reuse
                # the decision object for the route_to branch above.
                assert decision.reason is not None, (
                    "PolicyDecision(allow=False) must always carry a reason"
                )
                raise PolicyRejected(
                    reason=decision.reason,
                    reject_reason=decision.reject_reason,
                    recipient_id=to_agent,
                )
            if decision.route_to == "manifest":
                return await self._route_to_manifest(
                    route_id=route_id,
                    from_agent=from_agent,
                    to_agent=to_agent,
                    message=message,
                    attention_fee=attention_fee,
                )
            # attention_fee is meaningless when the message is going
            # to inbox or being rejected outright — surface a hard
            # error so the sender knows their funds were *not*
            # locked rather than silently discarding the field.
            if attention_fee is not None:
                raise AttentionFeeWrongModeError(
                    recipient_id=to_agent,
                    actual_route=decision.route_to or "inbox",
                )

        endpoint = _agent_delivery_endpoint(agent_info)
        logger.debug(f"[{route_id}] Discovered endpoint: {endpoint}")

        # 2. Offline pre-check — skip the HTTP round-trip when the registry
        #    already knows the agent is not online.
        #
        #    Done before _log_message so the audit stream reflects the real
        #    delivery direction ("inbound" inbox write, not a false "outbound").
        #
        #    Accuracy note: `status` is written on registration/heartbeat and
        #    cleared to "offline" by the background watchdog when the heartbeat
        #    TTL expires (~30 s window).  A false-positive (marked online but
        #    actually down) will fall through to the HTTP path and write to
        #    inbox on failure — unchanged from the old behaviour.  A
        #    false-negative (marked offline but actually responsive) is rare
        #    and self-heals on the next heartbeat; for now we skip the attempt
        #    to avoid a guaranteed timeout.
        if agent_info.status != "online":
            logger.info(
                f"[{route_id}] Agent {to_agent!r} is {agent_info.status!r};"
                " skipping HTTP, delivering directly to inbox"
            )
            log_entry = {
                "route_id": route_id,
                "from_agent": from_agent,
                "to_agent": to_agent,
                "direction": "inbound",
                "timestamp": datetime.now(UTC).isoformat(),
                "message": (
                    message.model_dump()
                    if hasattr(message, "model_dump")
                    else str(message)
                ),
            }
            await self._log_message(
                route_id=route_id,
                from_agent=from_agent,
                to_agent=to_agent,
                message=message,
                direction="inbound",
            )
            await self._store_inbox(to_agent=to_agent, log_entry=log_entry)
            # ``status="inbox"`` predates Phase 2 — kept verbatim for
            # SDK compatibility. ``delivery_mode="inbox"`` mirrors the
            # manifest-mode response shape so clients that switched to
            # the new schema can use a single key (``delivery_mode``)
            # instead of branching on ``status`` enum strings.
            return {
                "status": "inbox",
                "delivery_mode": "inbox",
                "route_id": route_id,
            }

        # 3. Log outbound message (only reached when agent is online)
        await self._log_message(
            route_id=route_id,
            from_agent=from_agent,
            to_agent=to_agent,
            message=message,
            direction="outbound",
        )

        try:
            # 4. Get A2A client and send message
            client = await self._get_client(endpoint)

            # Create SendMessageRequest
            request = SendMessageRequest(
                id=route_id,
                params=MessageSendParams(message=message),
            )
            response = await self._send_message_with_client(client, request)

            # 5. Log response
            logger.debug(f"[{route_id}] Received response: {type(response)}")

            logger.info(f"[{route_id}] Message delivered successfully")
            return response

        except Exception as e:
            logger.error(f"[{route_id}] Delivery failed: {e}")

            # Store in offline inbox so the recipient can pull when back online
            await self._store_inbox(
                to_agent=to_agent,
                log_entry={
                    "route_id": route_id,
                    "from_agent": from_agent,
                    "to_agent": to_agent,
                    "direction": "inbound",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "message": message.model_dump() if hasattr(message, "model_dump") else str(message),
                },
            )

            # Store in dead letter queue for retry
            await self._store_dlq(
                route_id=route_id,
                from_agent=from_agent,
                to_agent=to_agent,
                message=message,
                error=str(e),
            )
            raise

    async def route_by_tag(
        self,
        from_agent: str,
        tags: list[str],
        message: Message,
        prefer_online: bool = True,
    ) -> Any:
        """
        Discover agent by tags and route message

        Args:
            from_agent: Source agent/service ID
            tags: Required tags for target agent
            message: A2A Message object
            prefer_online: Prefer online agents

        Returns:
            A2A response

        Raises:
            ValueError: If no suitable agent found
        """
        # Discover agents with required tags
        status = "online" if prefer_online else None
        agents = await self.registry.search_agents(
            tags=tags,
            status=status,
        )

        if not agents:
            # Fallback: try without status filter
            if prefer_online:
                agents = await self.registry.search_agents(tags=tags)

            if not agents:
                raise ValueError(f"No agents found with tags: {tags}")

        # Select best agent (simple: first match)
        # TODO: Add load balancing, tag scoring
        target_agent = agents[0]

        logger.info(f"Discovered agent {target_agent.agent_id} for tags {tags}")

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

        endpoint = _agent_delivery_endpoint(agent_info)
        logger.info(f"Starting stream: {from_agent} -> {to_agent}")

        # Get A2A client and stream
        client = await self._get_client(endpoint)

        request = SendMessageRequest(
            id=str(uuid4()),
            params=MessageSendParams(message=message),
        )
        result = client.send_message(to_core_send_message_request(request))
        if hasattr(result, "__aiter__"):
            async for event in result:
                yield to_compat_stream_response(event, request_id=request.id)
            return
        if isawaitable(result):
            yield await result
            return
        yield result

    async def register_handler(
        self,
        message_type: str,
        handler: Callable,
    ) -> None:
        """
        Register handler for incoming messages.

        Idempotent: registering the same (type, handler) pair twice is a
        no-op, which matters for long-running processes that re-enter
        bootstrap paths (module reload, hot-reconnect on a flaky link,
        retry loops around startup). A hard cap of
        `MAX_HANDLERS_PER_TYPE` protects against a misbehaving caller
        fan-out-ing unique closures per retry.

        Args:
            message_type: Type of message to handle
            handler: Async handler function

        Raises:
            ValueError: if the per-type cap is reached.
        """
        bucket = self._handlers.setdefault(message_type, [])

        if handler in bucket:
            # Silent dedupe — callers doing idempotent startup get what
            # they want (no-op on re-register) without extra bookkeeping.
            return

        if len(bucket) >= MAX_HANDLERS_PER_TYPE:
            raise ValueError(
                f"handler cap reached for message_type={message_type!r}: "
                f"already {len(bucket)} registered (max "
                f"{MAX_HANDLERS_PER_TYPE}); refusing to register more"
            )

        bucket.append(handler)
        logger.info(
            f"Registered handler for: {message_type} "
            f"(now {len(bucket)}/{MAX_HANDLERS_PER_TYPE})"
        )

    async def unregister_handler(
        self,
        message_type: str,
        handler: Callable,
    ) -> bool:
        """Remove a previously registered (type, handler) pair.

        Returns:
            True if the handler was found and removed; False otherwise.
            Empty buckets are cleaned up to keep `_handlers` from
            accumulating keys for long-gone types.
        """
        bucket = self._handlers.get(message_type)
        if not bucket or handler not in bucket:
            return False
        bucket.remove(handler)
        if not bucket:
            del self._handlers[message_type]
        return True

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

    async def ack_inbox(
        self,
        agent_id: str,
        route_ids: list[str],
    ) -> int:
        """Precisely remove specific messages from an agent's offline inbox.

        Unlike ``get_inbox(consume=True)`` which clears the *entire* inbox, this
        method only removes the messages matching the supplied ``route_ids``.  This
        is safe to call while another process is also writing to the inbox (no
        full-key delete, only targeted ZREM).

        The inbox sorted set stores full JSON blobs as members.  Because we cannot
        ZREM by a sub-field, we do a single ZRANGE to fetch all members (bounded
        by _INBOX_CAP ≤ 50), filter in Python, then pipeline the ZREM calls.

        Args:
            agent_id: Agent whose inbox to acknowledge.
            route_ids: List of ``route_id`` values to remove.

        Returns:
            Number of messages actually removed.
        """
        key = f"acn:inbox:{agent_id}"
        route_id_set = set(route_ids)

        raw_members = await self.redis.zrange(key, 0, -1)
        to_remove: list[str] = []
        for raw in raw_members:
            member = raw.decode() if isinstance(raw, bytes) else raw
            try:
                entry = json.loads(member)
                if entry.get("route_id") in route_id_set:
                    to_remove.append(member)
            except (json.JSONDecodeError, TypeError):
                pass

        if not to_remove:
            return 0

        # ZREM accepts multiple members in a single command — no pipeline needed.
        await self.redis.zrem(key, *to_remove)
        return len(to_remove)

    async def get_inbox(
        self,
        agent_id: str,
        limit: int = 100,
        consume: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Get offline inbox for an agent.

        Returns messages that arrived while the agent was unreachable.
        Pass consume=True to clear the inbox after retrieval (acknowledge all).

        Args:
            agent_id: Agent ID
            limit: Max messages to return (use a large value when consume=True
                   to avoid silently discarding un-returned messages)
            consume: If True, delete the inbox key after reading

        Returns:
            List of pending message records, newest first
        """
        key = f"acn:inbox:{agent_id}"
        messages = await self.redis.zrevrange(key, 0, limit - 1)

        if consume:
            await self.redis.delete(key)

        return [json.loads(m) for m in messages]

    async def _log_message(
        self,
        route_id: str,
        from_agent: str,
        to_agent: str,
        message: Any,
        direction: str,
    ):
        """Append a routing event to the bounded audit stream.

        We used to SETEX one string per route_id (7-day TTL). At 1M
        msg/day that's ~1 M × 7 = 7 M keys of 0.5–2 KB each, ~1–14 GB
        steady-state with no consumers — it was debug-only trace. A
        capped Redis stream (XADD ... MAXLEN ~ N) holds the same
        information under a fixed memory ceiling and is still queryable
        via XRANGE / XREVRANGE.
        """
        timestamp = datetime.now(UTC).isoformat()

        if hasattr(message, "model_dump"):
            msg_data = message.model_dump()
        elif hasattr(message, "to_dict"):
            msg_data = message.to_dict()
        else:
            msg_data = str(message)

        await self.redis.xadd(
            MESSAGE_LOG_STREAM_KEY,
            {
                "route_id": route_id,
                "from_agent": from_agent,
                "to_agent": to_agent,
                "direction": direction,
                "timestamp": timestamp,
                # Stream fields must be strings/bytes/int/float, so we
                # serialize the payload here. Readers XRANGE + json.loads.
                "message": json.dumps(msg_data),
            },
            maxlen=MESSAGE_LOG_STREAM_MAXLEN,
            approximate=True,
        )

    async def _route_to_manifest(
        self,
        *,
        route_id: str,
        from_agent: str,
        to_agent: str,
        message: Message,
        attention_fee: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Phase 2 PR #1: divert inbound message into manifest queue.

        Thin wrapper that forwards to ``ManifestDispatcher`` (the
        shared helper used by both the router and the subnet
        manager). The dispatcher handles storage, WS notification,
        and metric counting; this method's only job is to surface
        the result in the router's ``route()`` response shape so the
        caller (``POST /send`` etc.) gets a consistent
        ``{"status": "sent", "delivery_mode": "manifest", ...}`` envelope.

        We don't ``_log_message`` here: the send was accepted, but
        actual delivery is deferred. Logging as ``direction=
        outbound`` would conflate with HTTP-delivered traffic;
        ``inbound`` would conflate with the inbox path. The manifest
        queue itself is the audit trail (every entry carries
        sender_id + ts).
        """
        if self.manifest_dispatcher is None:
            # Configuration error: policy says "route to manifest"
            # but the router has no dispatcher. Fail loudly rather
            # than silently dropping (which would surface as
            # messages disappearing without trace) or fail-open to
            # inbox (which would defeat the recipient's
            # manifest-mode opt-in).
            raise RuntimeError(
                "manifest mode requested but ManifestDispatcher is not wired; "
                "construct MessageRouter with manifest_dispatcher=... or set "
                "the recipient's communication_policy back to open/closed"
            )

        entry = await self.manifest_dispatcher.dispatch(
            owner_id=to_agent,
            sender_id=from_agent,
            message=message,
            path="router",
            route_id=route_id,
            attention_fee=attention_fee,
        )
        # P1-B2 review fix: keep ``status="sent"`` so existing SDK
        # clients that branch on ``result["status"] == "sent"``
        # continue to recognise this as success. ``delivery_mode``
        # is the new field for clients that want to distinguish
        # inbox vs manifest semantics. Pure additive — Phase 1
        # responses didn't carry ``delivery_mode`` at all.
        response: dict[str, Any] = {
            "status": "sent",
            "delivery_mode": "manifest",
            "route_id": route_id,
            "mid": entry.mid,
            "ts": entry.ts_ms,
        }
        # Surface the locked escrow id back to the sender so they can
        # track / reconcile / cancel the lock without depending on a
        # follow-up read of the manifest entry. ``acked_at`` /
        # ``release`` happen on the recipient side.
        attn = entry.extra.get("attention_fee") if entry.extra else None
        if isinstance(attn, dict) and attn.get("escrow_id"):
            response["attention_fee"] = {
                "escrow_id": attn["escrow_id"],
                "amount": attn.get("amount"),
                "currency": attn.get("currency"),
                "status": "locked",
            }
        return response

    async def _store_inbox(self, to_agent: str, log_entry: dict[str, Any]) -> None:
        """
        Store a failed-delivery message in the recipient's offline inbox.

        The inbox is a bounded sorted set (score = unix timestamp).
        Oldest messages are evicted when the cap is reached; the key expires
        automatically after _INBOX_TTL seconds so inactive agents don't consume
        Redis memory indefinitely.

        The three Redis commands are pipelined (non-transactional) so they
        consume a single round-trip instead of three.  Strict atomicity is not
        required here: all three commands target the same key and are ordered
        correctly within the pipeline.

        Args:
            to_agent: Recipient agent ID
            log_entry: Message log dict to persist
        """
        key = f"acn:inbox:{to_agent}"
        score = datetime.now(UTC).timestamp()
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.zadd(key, {json.dumps(log_entry): score})
            pipe.zremrangebyrank(key, 0, -(_INBOX_CAP + 1))
            pipe.expire(key, _INBOX_TTL)
            await pipe.execute()

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
        # Cap DLQ to prevent unbounded Redis memory growth (keep newest 10,000 entries)
        await self.redis.ltrim("acn:dlq", 0, 9999)
        logger.warning(f"Message {route_id} added to DLQ")

    async def retry_dlq(self, max_retries: int = 3, batch_limit: int = 100) -> int:
        """
        Retry messages in dead letter queue

        Args:
            max_retries: Maximum retry attempts per message
            batch_limit: Maximum messages to process per call to prevent long blocking

        Returns:
            Number of successfully retried messages
        """
        success_count = 0
        processed = 0

        while processed < batch_limit:
            entry_json = await self.redis.rpop("acn:dlq")
            if not entry_json:
                break

            processed += 1
            entry = json.loads(entry_json)

            if entry["retry_count"] >= max_retries:
                logger.error(f"Message {entry['route_id']} exceeded max retries, discarding")
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

                # ``Message`` requires ``message_id``. ``_store_dlq`` writes
                # ``model_dump()`` (snake_case) so the original id is in
                # ``message_id``; older payloads written before the rename
                # may carry the camelCase ``messageId``. Fall through to a
                # fresh UUID only if both are absent (extremely rare —
                # implies a hand-edited DLQ entry). Without this, any retry
                # Pydantic-fails on Message construction and the entry
                # bounces forever between rpop and lpush.
                rebuilt_message_id = (
                    msg_data.get("message_id")
                    or msg_data.get("messageId")
                    or f"dlq-{uuid4().hex[:12]}"
                )
                message = Message(
                    role=msg_data.get("role", "user"),
                    parts=parts,
                    message_id=rebuilt_message_id,
                )

                await self.route(
                    from_agent=entry["from_agent"],
                    to_agent=entry["to_agent"],
                    message=message,
                )

                success_count += 1
                logger.info(f"DLQ message {entry['route_id']} delivered")

            except PolicyRejected as e:
                # The recipient's communication_policy now denies this
                # sender. We honor the recipient's *current* intent
                # rather than the intent at the time the message
                # entered DLQ: an agent that flips to ``closed`` while
                # offline should not have stale messages forced into
                # their inbox the moment retry_dlq runs.
                #
                # Drop without requeue, do not bump ``retry_count``
                # (it would be observationally meaningless — the
                # entry never re-enters the queue). The structured
                # "dlq_dropped_by_policy" prefix lets operators grep
                # this apart from genuine retry failures.
                logger.warning(
                    "dlq_dropped_by_policy"
                    " route_id=%s from_agent=%s to_agent=%s reason=%s",
                    entry.get("route_id"),
                    entry.get("from_agent"),
                    entry.get("to_agent"),
                    e.reason,
                )

            except Exception as e:
                logger.error(f"DLQ retry failed: {e}")
                await self.redis.lpush("acn:dlq", json.dumps(entry))
                await self.redis.ltrim("acn:dlq", 0, 9999)

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
