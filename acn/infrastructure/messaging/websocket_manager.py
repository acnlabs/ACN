"""
WebSocket Manager

ACN Communication Layer component for real-time connections.
Manages WebSocket connections to frontend clients.

Responsibilities:
- Manage client connections
- Broadcast messages to connected clients
- Handle subscriptions (chat rooms, agent status)
- Integrate with Redis Pub/Sub for horizontal scaling
"""

import asyncio
import base64
import json
import logging
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

import redis.asyncio as redis
from fastapi import WebSocket

from ...security import safe_external_error

logger = logging.getLogger(__name__)


class MessageType(StrEnum):
    """WebSocket message types"""

    # Chat messages
    MESSAGE = "message"
    AGENT_MESSAGE = "agent_message"

    # Status updates
    AGENT_STATUS = "agent_status"
    AGENT_TYPING = "agent_typing"

    # System messages
    SYSTEM = "system"
    ERROR = "error"

    # Phase 2 manifest mode notification (decision Group B #7).
    # Pushed by MessageRouter._route_to_manifest after a manifest
    # entry is persisted, so the recipient client can decide
    # whether to pull full content via GET /communication/content/{mid}.
    # Payload shape: {type, mid, sender_id, summary, ts}.
    MANIFEST_NOTIFICATION = "manifest_notification"

    # Phase 3 Session layer events. Pushed via WS so participants
    # can react in real-time without polling.
    # Payload shapes (all carry ``session_id``):
    #   session_invite:    {type, session_id, from_agent, metadata}
    #   session_accepted:  {type, session_id, accepted_by}
    #   session_rejected:  {type, session_id, rejected_by}
    #   session_closed:    {type, session_id, closed_by}
    SESSION_INVITE = "session_invite"
    SESSION_ACCEPTED = "session_accepted"
    SESSION_REJECTED = "session_rejected"
    SESSION_CLOSED = "session_closed"

    # Connection management
    PING = "ping"
    PONG = "pong"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"

    # ADR-0012 Mode B — webhook delivery over the agent's outbound WS
    # control channel (for agents with no public HTTP endpoint).
    #   a2a_request:  ACN -> agent. Carries a relayed inbound HTTP request
    #                 ({id, method, path, headers, body, body_encoding,
    #                 deadline_ms}). ``id`` correlates the reply.
    #   a2a_response: agent -> ACN. Reply to an a2a_request, correlated by
    #                 ``id`` ({id, status, headers, body, body_encoding}).
    A2A_REQUEST = "a2a_request"
    A2A_RESPONSE = "a2a_response"

    # ADR-0012 P2d streaming (#171) — when the agent's local A2A server answers
    # an a2a_request with an SSE (text/event-stream) response, the agent streams
    # it back as a sequence of chunk frames terminated by one end frame, all
    # correlated by the original ``id``. Non-streaming replies keep using a
    # single ``a2a_response`` (the agent picks per response content-type), so
    # this is purely additive — old agents and non-stream calls are untouched.
    #   a2a_stream_chunk: agent -> ACN. One SSE chunk ({id, seq, data,
    #                     data_encoding}). ``seq`` is a 0-based monotonic index
    #                     for gap detection / debugging.
    #   a2a_stream_end:   agent -> ACN. Terminates the stream ({id, status?,
    #                     headers?, error?}). ``error`` set ⇒ aborted mid-stream.
    A2A_STREAM_CHUNK = "a2a_stream_chunk"
    A2A_STREAM_END = "a2a_stream_end"


@dataclass
class Connection:
    """WebSocket connection info"""

    connection_id: str
    websocket: WebSocket
    user_id: str | None = None
    subscriptions: set[str] = field(default_factory=set)
    connected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_activity: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)
    # Serialize writes to this socket: a Mode-B relay sends ``a2a_request``
    # frames from arbitrary proxy request tasks, concurrently with the WS
    # receive loop's pong/welcome sends. Starlette's ``send`` is not safe
    # under concurrent callers, so every ``_send`` takes this lock.
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class WebSocketManager:
    """
    WebSocket Manager

    Manages real-time WebSocket connections for:
    - Chat message streaming
    - Agent status updates
    - Typing indicators
    - System notifications

    Supports horizontal scaling via Redis Pub/Sub.

    Usage:
        ws_manager = WebSocketManager(redis_client)

        # In FastAPI endpoint
        @app.websocket("/ws/chat/{chat_id}")
        async def chat_websocket(websocket: WebSocket, chat_id: str):
            conn_id = await ws_manager.connect(websocket, user_id="user-123")
            await ws_manager.subscribe(conn_id, f"chat:{chat_id}")

            try:
                while True:
                    data = await websocket.receive_json()
                    await ws_manager.handle_message(conn_id, data)
            except WebSocketDisconnect:
                await ws_manager.disconnect(conn_id)

        # Broadcast from anywhere
        await ws_manager.broadcast(
            channel="chat:chat-123",
            message={"type": "message", "content": "Hello!"}
        )
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        heartbeat_interval: float = 30.0,
        max_connections: int = 10_000,
    ):
        """
        Initialize WebSocket Manager

        Args:
            redis_client: Redis for Pub/Sub
            heartbeat_interval: Heartbeat interval in seconds
            max_connections: Maximum concurrent WebSocket connections (0 = unlimited)
        """
        self.redis = redis_client
        self.heartbeat_interval = heartbeat_interval
        self.max_connections = max_connections

        # Active connections
        self._connections: dict[str, Connection] = {}

        # Channel subscriptions: channel -> set of connection_ids
        self._channels: dict[str, set[str]] = {}

        # Message handlers
        self._handlers: dict[str, Callable] = {}

        # ADR-0012 Mode B relay correlation: correlation_id -> Future that
        # the proxy path awaits and the WS receive loop resolves when the
        # matching ``a2a_response`` frame arrives. In-process by design —
        # the awaiting HTTP request and the agent's WS connection must live
        # on the same worker (ACN deploys single-instance; multi-replica
        # would need sticky routing or a pub/sub relay, tracked separately).
        self._relay_futures: dict[str, asyncio.Future] = {}

        # ADR-0012 P2d streaming (#171) correlation: correlation_id -> bounded
        # Queue the streaming caller drains while the WS receive loop enqueues
        # chunk/end frames. Same in-process single-worker constraint as the
        # single-shot futures above. ``_relay_stream_aborted`` tracks ids whose
        # consumer fell behind so the backpressure end-frame is emitted once.
        self._relay_streams: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._relay_stream_aborted: set[str] = set()

        # Pub/Sub subscriber
        self._pubsub: redis.client.PubSub | None = None
        self._pubsub_task: asyncio.Task | None = None

        logger.info("WebSocket Manager initialized")

    async def start(self):
        """Start the WebSocket manager (Pub/Sub listener)"""
        self._pubsub = self.redis.pubsub()
        self._pubsub_task = asyncio.create_task(self._listen_pubsub())
        logger.info("WebSocket Manager started")

    async def stop(self):
        """Stop the WebSocket manager"""
        if self._pubsub_task:
            self._pubsub_task.cancel()
            try:
                await self._pubsub_task
            except asyncio.CancelledError:
                pass

        if self._pubsub:
            # redis-py 5.0.1+ deprecated PubSub.close() in favor of aclose().
            await self._pubsub.aclose()

        # Close all connections
        for conn in list(self._connections.values()):
            await self._close_connection(conn)

        logger.info("WebSocket Manager stopped")

    async def connect(
        self,
        websocket: WebSocket,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        already_accepted: bool = False,
    ) -> str:
        """
        Accept a new WebSocket connection

        Args:
            websocket: FastAPI WebSocket
            user_id: Optional principal identifier (user_id, agent_id, etc.)
            metadata: Optional connection metadata
            already_accepted: Set to True if the caller already invoked
                ``websocket.accept()`` (e.g. to perform an auth handshake
                before handing the socket off to the manager). When True,
                the manager will not call ``accept()`` again — calling it
                twice raises RuntimeError in Starlette.

        Returns:
            Connection ID (empty string if rejected due to max_connections).
        """
        if self.max_connections > 0 and len(self._connections) >= self.max_connections:
            if not already_accepted:
                await websocket.accept()
            await websocket.close(code=4429, reason="Too many connections")
            logger.warning(
                f"WebSocket connection rejected: max_connections={self.max_connections} reached"
            )
            return ""

        if not already_accepted:
            await websocket.accept()

        connection_id = uuid4().hex[:16]

        connection = Connection(
            connection_id=connection_id,
            websocket=websocket,
            user_id=user_id,
            metadata=metadata or {},
        )

        self._connections[connection_id] = connection

        logger.info(
            f"WebSocket connected: {connection_id} (user={user_id}, total={len(self._connections)})"
        )

        # Send welcome message
        await self._send(
            connection,
            {
                "type": MessageType.SYSTEM.value,
                "message": "Connected to ACN",
                "connection_id": connection_id,
            },
        )

        return connection_id

    async def disconnect(self, connection_id: str):
        """
        Disconnect a WebSocket connection

        Args:
            connection_id: Connection ID to disconnect
        """
        if connection_id not in self._connections:
            return

        connection = self._connections[connection_id]

        # Remove from all channels
        for channel in list(connection.subscriptions):
            await self.unsubscribe(connection_id, channel)

        # Close connection
        await self._close_connection(connection)

        # Remove from connections
        del self._connections[connection_id]

        logger.info(f"WebSocket disconnected: {connection_id} (total={len(self._connections)})")

    async def disconnect_user(
        self,
        user_id: str,
        *,
        code: int = 4001,
        reason: str = "credentials_rotated",
    ) -> int:
        """Force-close every live connection held by a principal.

        Used by the ownership hand-off path (P3 §15.7 C2): rotating an
        agent's API key invalidates *future* authentication, but a socket
        that authenticated with the OLD key *before* rotation stays open —
        a "live tail" that keeps relaying A2A traffic on the previous
        owner's behalf. Tearing those sockets down forces a reconnect,
        where the now-stale old key fails the auth handshake and the
        previous owner is locked out for real.

        Sends a structured close frame (``code``/``reason``) so a
        conformant client can distinguish a forced rotation from a network
        drop and avoid hammering reconnect with a dead key. Returns the
        number of connections closed.
        """
        target_ids = [
            cid for cid, conn in self._connections.items() if conn.user_id == user_id
        ]
        for connection_id in target_ids:
            conn = self._connections.get(connection_id)
            if conn is None:  # pragma: no cover - concurrent disconnect
                continue
            try:
                async with conn.send_lock:
                    await conn.websocket.close(code=code, reason=reason)
            except Exception:  # noqa: BLE001 — best-effort close frame
                pass
            # Channel + registry cleanup, mirroring disconnect() but WITHOUT a
            # second close(): on a real socket that double-close raises (caught
            # but wasteful), and it would clobber the structured close code we
            # just sent. The route's receive loop will also call disconnect()
            # once the closed socket unblocks its recv — a harmless no-op since
            # we already removed the entry here.
            for channel in list(conn.subscriptions):
                await self.unsubscribe(connection_id, channel)
            self._connections.pop(connection_id, None)

        if target_ids:
            logger.info(
                f"Force-disconnected {len(target_ids)} connection(s) "
                f"for {user_id} ({reason})"
            )
        return len(target_ids)

    async def subscribe(
        self,
        connection_id: str,
        channel: str,
    ):
        """
        Subscribe connection to a channel

        Args:
            connection_id: Connection ID
            channel: Channel name (e.g., "chat:chat-123")
        """
        if connection_id not in self._connections:
            return

        connection = self._connections[connection_id]
        connection.subscriptions.add(channel)

        # Add to channel set
        if channel not in self._channels:
            self._channels[channel] = set()
            # Subscribe to Redis Pub/Sub
            if self._pubsub:
                await self._pubsub.subscribe(f"acn:ws:{channel}")

        self._channels[channel].add(connection_id)

        logger.debug(f"Connection {connection_id} subscribed to {channel}")

    async def unsubscribe(
        self,
        connection_id: str,
        channel: str,
    ):
        """
        Unsubscribe connection from a channel

        Args:
            connection_id: Connection ID
            channel: Channel name
        """
        if connection_id not in self._connections:
            return

        connection = self._connections[connection_id]
        connection.subscriptions.discard(channel)

        # Remove from channel set
        if channel in self._channels:
            self._channels[channel].discard(connection_id)

            # Cleanup empty channel
            if not self._channels[channel]:
                del self._channels[channel]
                if self._pubsub:
                    await self._pubsub.unsubscribe(f"acn:ws:{channel}")

        logger.debug(f"Connection {connection_id} unsubscribed from {channel}")

    async def broadcast(
        self,
        channel: str,
        message: dict[str, Any],
        exclude: set[str] | None = None,
    ):
        """
        Broadcast message to all connections in a channel

        Args:
            channel: Channel name
            message: Message dict
            exclude: Connection IDs to exclude
        """
        # Publish to Redis for horizontal scaling
        await self.redis.publish(
            f"acn:ws:{channel}",
            json.dumps(message),
        )

        # Also send locally
        await self._broadcast_local(channel, message, exclude)

    async def _broadcast_local(
        self,
        channel: str,
        message: dict[str, Any],
        exclude: set[str] | None = None,
    ):
        """Broadcast to local connections only"""
        if channel not in self._channels:
            return

        exclude = exclude or set()

        for connection_id in self._channels[channel]:
            if connection_id in exclude:
                continue

            if connection_id in self._connections:
                connection = self._connections[connection_id]
                await self._send(connection, message)

    async def send_to_user(
        self,
        user_id: str,
        message: dict[str, Any],
    ):
        """
        Send message to all connections of a user

        Args:
            user_id: User ID
            message: Message dict
        """
        for connection in self._connections.values():
            if connection.user_id == user_id:
                await self._send(connection, message)

    async def send_to_connection(
        self,
        connection_id: str,
        message: dict[str, Any],
    ):
        """
        Send message to specific connection

        Args:
            connection_id: Connection ID
            message: Message dict
        """
        if connection_id in self._connections:
            await self._send(self._connections[connection_id], message)

    async def handle_message(
        self,
        connection_id: str,
        data: dict[str, Any],
    ):
        """
        Handle incoming WebSocket message

        Args:
            connection_id: Connection ID
            data: Message data
        """
        if connection_id not in self._connections:
            return

        connection = self._connections[connection_id]
        connection.last_activity = datetime.now(UTC)

        message_type = data.get("type", "")

        # Handle built-in message types
        if message_type == MessageType.PING.value:
            await self._send(
                connection,
                {
                    "type": MessageType.PONG.value,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )
            return

        if message_type == MessageType.SUBSCRIBE.value:
            channel = data.get("channel")
            if channel:
                # M5: validate channel before subscribing.
                err = self._check_channel_auth(connection, channel)
                if err:
                    await self._send(
                        connection,
                        {"type": MessageType.ERROR.value, "error": err},
                    )
                    return
                await self.subscribe(connection_id, channel)
            return

        if message_type == MessageType.UNSUBSCRIBE.value:
            channel = data.get("channel")
            if channel:
                # M5: only allow unsubscribing from channels the agent
                # is actually subscribed to (avoids resource confusion).
                if channel in connection.subscriptions:
                    await self.unsubscribe(connection_id, channel)
            return

        # Call registered handlers
        if message_type in self._handlers:
            try:
                await self._handlers[message_type](connection, data)
            except Exception as e:
                logger.error(f"Handler error for {message_type}: {e}")
                # M12: error frame is sent back over the WebSocket to the
                # client. ``str(e)`` would otherwise leak handler-internal
                # details (httpx URLs, traceback fragments). The category
                # alone is sufficient for clients to act on (retry,
                # surface a UI message); full detail is in the server log.
                await self._send(
                    connection,
                    {
                        "type": MessageType.ERROR.value,
                        "error": safe_external_error(e),
                    },
                )

    # M5 — channel subscription authorization.
    #
    # Channel naming convention:
    #   agent:<agent_id>   — per-agent notification channel; only the
    #                        owning agent may subscribe.
    #   session:<id>       — bilateral session channels; any participant may
    #                        subscribe (membership validated at session layer).
    #   broadcast:<topic>  — public broadcast channels; anyone may subscribe.
    #   system:<slug>      — RESERVED for internal server-side use; no client
    #                        should ever need to subscribe directly.
    #
    # Any channel that does not match a known prefix is also blocked to
    # prevent typo-drift from silently creating orphan subscriptions.
    _MAX_CHANNEL_NAME_LEN: int = 256
    _ALLOWED_PREFIXES: tuple[str, ...] = ("session:", "broadcast:")

    def _check_channel_auth(self, connection: Connection, channel: str) -> str | None:
        """Return an error reason string if the subscription is denied, else None.

        Rules (M5):
        * ``system:*`` — always denied (server-internal namespace).
        * ``agent:*``   — only allowed when the suffix matches the
                          connection's own ``user_id``.
        * ``session:*`` / ``broadcast:*`` — allowed (public / session layer).
        * Any other prefix — denied (allowlist, not denylist).
        * Name length > _MAX_CHANNEL_NAME_LEN — denied (DoS guard).
        """
        if len(channel) > self._MAX_CHANNEL_NAME_LEN:
            return "channel_name_too_long"
        if channel.startswith("system:"):
            return "channel_subscription_denied"
        if channel.startswith("agent:"):
            owner_id = channel[len("agent:"):]
            if connection.user_id != owner_id:
                return "channel_subscription_denied"
            return None
        for prefix in self._ALLOWED_PREFIXES:
            if channel.startswith(prefix):
                return None
        return "channel_subscription_denied"

    def register_handler(
        self,
        message_type: str,
        handler: Callable,
    ):
        """
        Register handler for message type

        Args:
            message_type: Message type
            handler: Async handler function(connection, data)
        """
        self._handlers[message_type] = handler
        logger.info(f"Registered WebSocket handler for: {message_type}")

    async def _send(
        self,
        connection: Connection,
        message: dict[str, Any],
    ):
        """Send message to connection"""
        try:
            async with connection.send_lock:
                await connection.websocket.send_json(message)
        except Exception as e:
            logger.error(f"Failed to send to {connection.connection_id}: {e}")

    async def _close_connection(self, connection: Connection):
        """Close a WebSocket connection"""
        try:
            await connection.websocket.close()
        except Exception:
            pass

    async def _listen_pubsub(self):
        """Listen for Redis Pub/Sub messages"""
        if not self._pubsub:
            return

        try:
            async for message in self._pubsub.listen():
                if message["type"] != "message":
                    continue

                # Parse channel: acn:ws:chat:chat-123 -> chat:chat-123
                channel = message["channel"]
                if isinstance(channel, bytes):
                    channel = channel.decode()

                if channel.startswith("acn:ws:"):
                    channel = channel[7:]  # Remove prefix

                # Parse data
                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode()

                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    continue

                # Broadcast to local connections
                await self._broadcast_local(channel, data)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Pub/Sub listener error: {e}")

    # --- Convenience methods for common operations ---

    async def broadcast_chat_message(
        self,
        chat_id: str,
        message: dict[str, Any],
    ):
        """
        Broadcast a chat message

        Args:
            chat_id: Chat ID
            message: Message dict
        """
        await self.broadcast(
            channel=f"chat:{chat_id}",
            message={
                "type": MessageType.MESSAGE.value,
                "message": message,
            },
        )

    async def broadcast_agent_status(
        self,
        chat_id: str,
        agent_id: str,
        status: str,
    ):
        """
        Broadcast agent status change

        Args:
            chat_id: Chat ID
            agent_id: Agent ID
            status: New status (online/offline/busy)
        """
        await self.broadcast(
            channel=f"chat:{chat_id}",
            message={
                "type": MessageType.AGENT_STATUS.value,
                "agent_id": agent_id,
                "status": status,
            },
        )

    async def broadcast_agent_typing(
        self,
        chat_id: str,
        agent_id: str,
        is_typing: bool,
    ):
        """
        Broadcast agent typing indicator

        Args:
            chat_id: Chat ID
            agent_id: Agent ID
            is_typing: Whether agent is typing
        """
        await self.broadcast(
            channel=f"chat:{chat_id}",
            message={
                "type": MessageType.AGENT_TYPING.value,
                "agent_id": agent_id,
                "is_typing": is_typing,
            },
        )

    def get_stats(self) -> dict[str, Any]:
        """Get WebSocket manager statistics"""
        return {
            "total_connections": len(self._connections),
            "total_channels": len(self._channels),
            "connections_by_channel": {
                channel: len(conn_ids) for channel, conn_ids in self._channels.items()
            },
        }

    def is_user_connected(self, user_id: str) -> bool:
        """
        Return True if the given principal has at least one active connection.

        ``user_id`` is the generic principal identifier passed to ``connect()``.
        Route layer may pass an ``agent_id`` here — the manager is agnostic to
        the principal type.

        Args:
            user_id: Principal identifier set during ``connect()``.
        """
        return any(conn.user_id == user_id for conn in self._connections.values())

    # --- ADR-0012 Mode B: real-time webhook relay over the WS channel ---

    def _first_connection_for(self, user_id: str) -> "Connection | None":
        """Return one live connection for the principal, or None if offline."""
        for conn in self._connections.values():
            if conn.user_id == user_id:
                return conn
        return None

    async def relay_request_to_agent(
        self,
        agent_id: str,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes | str,
        timeout: float = 30.0,
    ) -> dict[str, Any] | None:
        """Relay an inbound HTTP request to a connected agent and await its reply.

        This is the real-time delivery path for agents that registered
        without a public HTTP endpoint: instead of ACN dialing the agent
        (Mode A), the agent holds an outbound WebSocket and ACN pushes the
        request down it, then blocks for the correlated ``a2a_response``.

        Args:
            agent_id: Recipient agent (matched against connection ``user_id``).
            method: Original HTTP method.
            path: Sub-path beyond the proxy root ("/" for root A2A POST).
            headers: Forward headers (hop-by-hop already stripped by caller).
            body: Raw request body.
            timeout: Seconds to wait for the agent's response.

        Returns:
            ``{"status", "headers", "body", "body_encoding"}`` when the agent
            replied; ``None`` when the agent holds no live WS connection (the
            caller falls back to inbox / 503).

        Raises:
            TimeoutError: agent is connected but did not reply within ``timeout``.
        """
        # Deliver to exactly ONE connection. ``send_to_user`` broadcasts to
        # every connection the agent holds, which would make a non-idempotent
        # request execute once per connection (double charge, duplicate
        # reply). For request/response relay we pick a single connection and
        # correlate its reply.
        connection = self._first_connection_for(agent_id)
        if connection is None:
            return None

        correlation_id = uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._relay_futures[correlation_id] = future

        if isinstance(body, bytes):
            try:
                body_text = body.decode("utf-8")
                body_encoding = "utf-8"
            except UnicodeDecodeError:
                import base64

                body_text = base64.b64encode(body).decode("ascii")
                body_encoding = "base64"
        else:
            body_text = body or ""
            body_encoding = "utf-8"

        frame = {
            "type": MessageType.A2A_REQUEST.value,
            "id": correlation_id,
            "method": method,
            "path": path or "/",
            "headers": headers,
            "body": body_text,
            "body_encoding": body_encoding,
            "deadline_ms": int(timeout * 1000),
        }

        try:
            await self._send(connection, frame)
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            # Always drop the pending entry — on success, timeout, or
            # cancellation — so the registry cannot leak Futures.
            self._relay_futures.pop(correlation_id, None)

    def resolve_relay_response(self, correlation_id: str, payload: dict[str, Any]) -> bool:
        """Resolve a pending relay request with the agent's ``a2a_response``.

        Called from the WS receive loop. Returns True when a waiting request
        was matched, False for an unknown / already-settled correlation id
        (stale reply after a timeout, or a duplicate from a second connection).
        """
        future = self._relay_futures.get(correlation_id)
        if future is None or future.done():
            return False
        future.set_result(payload)
        return True

    # --- ADR-0012 P2d streaming (#171): SSE relay over the WS channel ---

    # Bounded so a slow SSE consumer cannot make the WS receive loop buffer an
    # unbounded number of chunks (memory) — on overflow the stream is aborted
    # (see ``enqueue_relay_stream_frame``) rather than blocking the shared loop.
    _RELAY_STREAM_QUEUE_MAXSIZE = 256

    @staticmethod
    def _encode_relay_body(body: bytes | str) -> tuple[str, str]:
        """Encode a request body for transport in a JSON frame.

        Returns ``(body_text, body_encoding)`` — UTF-8 when the bytes decode
        cleanly, base64 otherwise. Mirrors the inline logic in
        ``relay_request_to_agent`` so single-shot and streaming frames carry
        identical bytes for the same input.
        """
        if isinstance(body, bytes):
            try:
                return body.decode("utf-8"), "utf-8"
            except UnicodeDecodeError:
                return base64.b64encode(body).decode("ascii"), "base64"
        return (body or ""), "utf-8"

    async def relay_request_open(
        self,
        agent_id: str,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes | str,
        timeout: float = 30.0,
    ) -> "tuple[dict[str, Any], asyncio.Queue[dict[str, Any]]] | None":
        """Send an ``a2a_request`` and await the agent's FIRST reply frame.

        This is the stream-aware entry point. The agent decides per response:
        a non-streaming handler replies with a single ``a2a_response`` (first
        frame is terminal); a streaming (SSE) handler replies with one or more
        ``a2a_stream_chunk`` frames then ``a2a_stream_end``.

        Returns ``(first_frame, queue)`` — inspect ``first_frame["type"]``:
        ``a2a_response`` means single-shot (ignore the queue); ``a2a_stream_chunk``
        means drain ``queue`` for the remaining chunks until an ``a2a_stream_end``
        frame. Returns ``None`` when the agent holds no live WS connection (the
        caller falls back to inbox / 503, exactly like ``relay_request_to_agent``).

        Raises ``TimeoutError`` when the agent is connected but sends no frame
        within ``timeout`` (surfaced by callers as 504 — "connected but silent").

        The caller MUST drain/close the returned queue via ``close_relay_stream``
        when done so the registry cannot leak.
        """
        connection = self._first_connection_for(agent_id)
        if connection is None:
            return None

        correlation_id = uuid4().hex
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=self._RELAY_STREAM_QUEUE_MAXSIZE
        )
        self._relay_streams[correlation_id] = queue

        body_text, body_encoding = self._encode_relay_body(body)
        frame = {
            "type": MessageType.A2A_REQUEST.value,
            "id": correlation_id,
            "method": method,
            "path": path or "/",
            "headers": headers,
            "body": body_text,
            "body_encoding": body_encoding,
            "deadline_ms": int(timeout * 1000),
        }

        try:
            await self._send(connection, frame)
            first = await asyncio.wait_for(queue.get(), timeout=timeout)
        except BaseException:
            # Timeout / cancellation before any frame: drop the registration so
            # a late frame from the agent does not leak the queue.
            self.close_relay_stream(correlation_id)
            raise
        return first, queue

    def enqueue_relay_stream_frame(self, correlation_id: str, frame: dict[str, Any]) -> bool:
        """Hand a streaming frame from the WS receive loop to the waiting caller.

        Non-blocking by contract: the WS receive loop is shared across every
        agent connection, so it must never ``await`` on a slow consumer's queue.
        On overflow (consumer fell behind) the stream is ABORTED — the oldest
        buffered chunk is dropped to free a slot and a synthetic ``a2a_stream_end``
        with ``error="relay_backpressure_abort"`` is enqueued exactly once, so the
        consumer terminates cleanly instead of stalling the loop or leaking memory.

        Returns True when a stream with ``correlation_id`` is registered (the
        frame was accepted or the abort sentinel was queued), False for an
        unknown / already-closed id (stale frame after timeout/disconnect).
        """
        queue = self._relay_streams.get(correlation_id)
        if queue is None:
            return False
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            if correlation_id in self._relay_stream_aborted:
                # Already aborted; drop silently — the end sentinel is queued.
                return True
            self._relay_stream_aborted.add(correlation_id)
            try:
                queue.get_nowait()  # free one slot (ordering moot once aborted)
            except asyncio.QueueEmpty:  # pragma: no cover - defensive
                pass
            try:
                queue.put_nowait(
                    {
                        "type": MessageType.A2A_STREAM_END.value,
                        "id": correlation_id,
                        "error": "relay_backpressure_abort",
                    }
                )
            except asyncio.QueueFull:  # pragma: no cover - defensive
                pass
        return True

    def close_relay_stream(self, correlation_id: str) -> None:
        """Drop a stream's registration. Idempotent; safe to call in finally."""
        self._relay_streams.pop(correlation_id, None)
        self._relay_stream_aborted.discard(correlation_id)

    async def relay_request_to_agent_stream(
        self,
        agent_id: str,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes | str,
        timeout: float = 30.0,
    ) -> "AsyncGenerator[dict[str, Any], None]":
        """High-level streaming relay: yield reply frames until the stream ends.

        Yields the agent's reply frames in order: either a single
        ``a2a_response`` (non-streaming handler) or a run of ``a2a_stream_chunk``
        frames followed by ``a2a_stream_end``. Raises ``LookupError`` when the
        agent is offline (no live WS) and ``TimeoutError`` on first-frame /
        inter-chunk silence, so the SSE caller can surface a clear error.

        Used by ``MessageRouter.route_stream``; the HTTP gateway proxy uses the
        lower-level ``relay_request_open`` directly so it can choose between a
        buffered ``Response`` and a ``StreamingResponse`` on the first frame.
        """
        opened = await self.relay_request_open(
            agent_id,
            method=method,
            path=path,
            headers=headers,
            body=body,
            timeout=timeout,
        )
        if opened is None:
            raise LookupError(f"agent {agent_id!r} has no live relay connection")
        first, queue = opened
        correlation_id = first.get("id", "")
        try:
            frame = first
            while True:
                yield frame
                ftype = frame.get("type")
                if ftype in (
                    MessageType.A2A_RESPONSE.value,
                    MessageType.A2A_STREAM_END.value,
                ):
                    return
                frame = await asyncio.wait_for(queue.get(), timeout=timeout)
        finally:
            self.close_relay_stream(correlation_id)
