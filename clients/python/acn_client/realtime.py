"""
ACN WebSocket Client

Real-time communication with ACN server.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

try:
    import websockets
    from websockets.client import WebSocketClientProtocol

    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    WebSocketClientProtocol = Any  # type: ignore


class WSState(str, Enum):
    """WebSocket connection state"""

    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"


@dataclass
class WSMessage:
    """WebSocket message"""

    type: str
    channel: str
    data: Any
    timestamp: str


@dataclass
class ACNRealtimeOptions:
    """WebSocket connection options"""

    auto_reconnect: bool = True
    reconnect_interval: float = 3.0
    max_reconnect_attempts: int = 10
    heartbeat_interval: float = 30.0


MessageHandler = Callable[[WSMessage], None]


class ACNRealtime:
    """
    ACN Real-time Client

    Example:
        >>> realtime = ACNRealtime("ws://localhost:9000")
        >>> 
        >>> @realtime.on("agents")
        ... def handle_agent_event(msg):
        ...     print(f"Agent event: {msg}")
        >>> 
        >>> await realtime.connect()
    """

    def __init__(
        self,
        base_url: str = "ws://localhost:9000",
        options: ACNRealtimeOptions | None = None,
    ):
        """
        Initialize ACN Realtime Client

        Args:
            base_url: ACN WebSocket URL (ws:// or wss://)
            options: Connection options
        """
        if not WEBSOCKETS_AVAILABLE:
            raise ImportError(
                "websockets package is required for real-time features. "
                "Install it with: pip install websockets"
            )

        # Convert http to ws
        self.base_url = base_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
        self.options = options or ACNRealtimeOptions()

        self._ws: WebSocketClientProtocol | None = None
        self._state = WSState.DISCONNECTED
        self._reconnect_attempts = 0
        self._handlers: dict[str, list[MessageHandler]] = {}
        self._global_handlers: list[MessageHandler] = []
        self._state_handlers: list[Callable[[WSState], None]] = []
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._receive_task: asyncio.Task[None] | None = None

    @property
    def state(self) -> WSState:
        """Current connection state"""
        return self._state

    @property
    def is_connected(self) -> bool:
        """Whether currently connected"""
        return self._state == WSState.CONNECTED

    def _set_state(self, state: WSState) -> None:
        """Update state and notify handlers"""
        self._state = state
        for handler in self._state_handlers:
            try:
                handler(state)
            except Exception:
                pass

    async def connect(self, channel: str = "default") -> None:
        """
        Connect to WebSocket channel

        Args:
            channel: Channel name to subscribe to
        """
        if self._ws and self._state == WSState.CONNECTED:
            return

        self._set_state(WSState.CONNECTING)

        try:
            self._ws = await websockets.connect(f"{self.base_url}/ws/{channel}")
            self._set_state(WSState.CONNECTED)
            self._reconnect_attempts = 0

            # Start heartbeat and receive tasks
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            self._receive_task = asyncio.create_task(self._receive_loop(channel))

        except Exception as e:
            self._set_state(WSState.DISCONNECTED)
            raise ConnectionError(f"Failed to connect to ACN: {e}") from e

    async def disconnect(self) -> None:
        """Disconnect from server"""
        # Cancel tasks
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        if self._receive_task:
            self._receive_task.cancel()
            self._receive_task = None

        # Close connection
        if self._ws:
            await self._ws.close()
            self._ws = None

        self._set_state(WSState.DISCONNECTED)

    def on(self, channel: str) -> Callable[[MessageHandler], MessageHandler]:
        """
        Decorator to subscribe to a channel

        Args:
            channel: Channel name

        Example:
            >>> @realtime.on("agents")
            ... def handle_agent(msg):
            ...     print(msg)
        """

        def decorator(handler: MessageHandler) -> MessageHandler:
            self.subscribe(channel, handler)
            return handler

        return decorator

    def subscribe(self, channel: str, handler: MessageHandler) -> Callable[[], None]:
        """
        Subscribe to a channel

        Args:
            channel: Channel name
            handler: Message handler function

        Returns:
            Unsubscribe function
        """
        if channel not in self._handlers:
            self._handlers[channel] = []
        self._handlers[channel].append(handler)

        def unsubscribe() -> None:
            if channel in self._handlers:
                self._handlers[channel].remove(handler)
                if not self._handlers[channel]:
                    del self._handlers[channel]

        return unsubscribe

    def on_message(self, handler: MessageHandler) -> Callable[[], None]:
        """
        Subscribe to all messages

        Args:
            handler: Message handler function

        Returns:
            Unsubscribe function
        """
        self._global_handlers.append(handler)

        def unsubscribe() -> None:
            self._global_handlers.remove(handler)

        return unsubscribe

    def on_state_change(self, handler: Callable[[WSState], None]) -> Callable[[], None]:
        """
        Subscribe to state changes

        Args:
            handler: State change handler

        Returns:
            Unsubscribe function
        """
        self._state_handlers.append(handler)

        def unsubscribe() -> None:
            self._state_handlers.remove(handler)

        return unsubscribe

    async def send(self, data: Any) -> None:
        """
        Send a message

        Args:
            data: Data to send (will be JSON serialized)
        """
        if not self._ws or self._state != WSState.CONNECTED:
            raise ConnectionError("WebSocket not connected")

        await self._ws.send(json.dumps(data))

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats"""
        while self._state == WSState.CONNECTED:
            try:
                await asyncio.sleep(self.options.heartbeat_interval)
                if self._ws:
                    await self._ws.send(json.dumps({"type": "ping"}))
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _receive_loop(self, channel: str) -> None:
        """Receive and dispatch messages"""
        while self._state == WSState.CONNECTED:
            try:
                if not self._ws:
                    break

                raw = await self._ws.recv()
                data = json.loads(raw)

                msg = WSMessage(
                    type=data.get("type", "unknown"),
                    channel=data.get("channel", channel),
                    data=data.get("data"),
                    timestamp=data.get("timestamp", ""),
                )

                # Notify global handlers
                for handler in self._global_handlers:
                    try:
                        handler(msg)
                    except Exception:
                        pass

                # Notify channel handlers
                for ch in [msg.channel, msg.type]:
                    if ch in self._handlers:
                        for handler in self._handlers[ch]:
                            try:
                                handler(msg)
                            except Exception:
                                pass

            except asyncio.CancelledError:
                break
            except Exception:
                if self.options.auto_reconnect and self._reconnect_attempts < self.options.max_reconnect_attempts:
                    await self._reconnect(channel)
                else:
                    self._set_state(WSState.DISCONNECTED)
                    break

    async def _reconnect(self, channel: str) -> None:
        """Attempt to reconnect"""
        self._set_state(WSState.RECONNECTING)
        self._reconnect_attempts += 1

        delay = self.options.reconnect_interval * min(self._reconnect_attempts, 5)
        await asyncio.sleep(delay)

        try:
            await self.connect(channel)
        except Exception:
            pass

    async def __aenter__(self) -> "ACNRealtime":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.disconnect()

