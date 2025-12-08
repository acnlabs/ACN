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
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

import redis.asyncio as redis
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class MessageType(str, Enum):
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

    # Connection management
    PING = "ping"
    PONG = "pong"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"


@dataclass
class Connection:
    """WebSocket connection info"""

    connection_id: str
    websocket: WebSocket
    user_id: str | None = None
    subscriptions: set[str] = field(default_factory=set)
    connected_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)


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
    ):
        """
        Initialize WebSocket Manager

        Args:
            redis_client: Redis for Pub/Sub
            heartbeat_interval: Heartbeat interval in seconds
        """
        self.redis = redis_client
        self.heartbeat_interval = heartbeat_interval

        # Active connections
        self._connections: dict[str, Connection] = {}

        # Channel subscriptions: channel -> set of connection_ids
        self._channels: dict[str, set[str]] = {}

        # Message handlers
        self._handlers: dict[str, Callable] = {}

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
            await self._pubsub.close()

        # Close all connections
        for conn in list(self._connections.values()):
            await self._close_connection(conn)

        logger.info("WebSocket Manager stopped")

    async def connect(
        self,
        websocket: WebSocket,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """
        Accept a new WebSocket connection

        Args:
            websocket: FastAPI WebSocket
            user_id: Optional user ID
            metadata: Optional connection metadata

        Returns:
            Connection ID
        """
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
        connection.last_activity = datetime.utcnow()

        message_type = data.get("type", "")

        # Handle built-in message types
        if message_type == MessageType.PING.value:
            await self._send(
                connection,
                {
                    "type": MessageType.PONG.value,
                    "timestamp": datetime.utcnow().isoformat(),
                },
            )
            return

        if message_type == MessageType.SUBSCRIBE.value:
            channel = data.get("channel")
            if channel:
                await self.subscribe(connection_id, channel)
            return

        if message_type == MessageType.UNSUBSCRIBE.value:
            channel = data.get("channel")
            if channel:
                await self.unsubscribe(connection_id, channel)
            return

        # Call registered handlers
        if message_type in self._handlers:
            try:
                await self._handlers[message_type](connection, data)
            except Exception as e:
                logger.error(f"Handler error for {message_type}: {e}")
                await self._send(
                    connection,
                    {
                        "type": MessageType.ERROR.value,
                        "error": str(e),
                    },
                )

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




