"""
ACN WebSocket Client

Real-time communication with ACN server.
"""

import asyncio
import json
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import quote

try:
    import websockets
    from websockets.asyncio.client import ClientConnection as WebSocketClientProtocol

    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    WebSocketClientProtocol = Any  # type: ignore[assignment,misc]


class WSState(StrEnum):
    """WebSocket connection state"""

    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"


class AuthMode(StrEnum):
    """How to deliver the agent API key during the WebSocket handshake.

    - ``HEADER`` (default, recommended): send ``Authorization: Bearer
      <key>`` as a handshake header. Keeps the secret out of access logs
      and Referer headers. Works for any non-browser client (server-side
      Python, CLI, daemons).
    - ``FIRST_MESSAGE``: connect first, then send
      ``{"type": "auth", "token": "<key>"}`` as the first frame and wait
      for ``{"type": "auth_ok"}``. The only option in browsers because
      ``new WebSocket()`` cannot set arbitrary headers.
    - ``QUERY``: append ``?token=<key>`` to the URL. **Deprecated.**
      Disabled on the ACN server unless ``WEBSOCKET_ALLOW_QUERY_TOKEN=true``
      (auto-enabled in dev). Leaks the key into access logs and URL bars.
    """

    HEADER = "header"
    FIRST_MESSAGE = "first_message"
    QUERY = "query"


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

    Example (server-side Python, recommended):
        >>> realtime = ACNRealtime(
        ...     "ws://localhost:9000",
        ...     agent_id="agent_abc",
        ...     api_key="ak_live_...",
        ... )
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
        *,
        agent_id: str | None = None,
        api_key: str | None = None,
        auth_mode: AuthMode | str = AuthMode.HEADER,
    ):
        """Initialize ACN Realtime Client.

        Args:
            base_url: ACN WebSocket URL (``ws://`` or ``wss://``). HTTP(S)
                URLs are auto-rewritten.
            options: Reconnect / heartbeat tuning.
            agent_id: The agent ID owning this connection. ACN's WS
                endpoint is ``/ws/{agent_id}`` and the server validates
                that the API key matches the URL's agent_id, so the two
                must be paired correctly.
            api_key: Agent API key. Required for any non-dev ACN deployment.
                If ``None`` the client connects unauthenticated and the
                server will close the socket with code ``4401`` unless
                ACN is running in dev mode without auth.
            auth_mode: How to deliver ``api_key`` during the handshake.
                See :class:`AuthMode`. Defaults to ``HEADER`` —
                ``Authorization: Bearer`` — which is safe everywhere
                except in-browser usage.
        """
        if not WEBSOCKETS_AVAILABLE:
            raise ImportError(
                "websockets package is required for real-time features. "
                "Install it with: pip install websockets"
            )

        # Convert http to ws
        self.base_url = (
            base_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
        )
        self.options = options or ACNRealtimeOptions()
        self.agent_id = agent_id
        self.api_key = api_key
        # Normalize: accept plain strings ("header" / "first_message" / "query")
        # for ergonomics — many users won't import AuthMode.
        self.auth_mode = AuthMode(auth_mode) if not isinstance(auth_mode, AuthMode) else auth_mode

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

    async def connect(self, channel: str | None = None) -> None:
        """Connect to ACN WebSocket and complete the auth handshake.

        ACN's endpoint is ``/ws/{agent_id}`` — there is no "channel"
        concept on the server side. The legacy ``channel`` argument is
        kept as a positional alias for ``agent_id`` for back-compat with
        early SDK callers; constructor-supplied ``agent_id`` always wins.

        Args:
            channel: Deprecated alias for ``agent_id`` when not set on
                the constructor. Emits a ``DeprecationWarning`` if used.
        """
        if self._ws and self._state == WSState.CONNECTED:
            return

        # Resolve target agent_id: constructor wins; otherwise fall back
        # to the legacy ``channel`` arg (with a deprecation warning); if
        # neither was supplied we default to "default" only to preserve
        # the historical no-arg behaviour, but ACN will refuse this in
        # any realistic deployment.
        target = self.agent_id
        if target is None and channel is not None:
            warnings.warn(
                "ACNRealtime.connect(channel=...) is deprecated; pass "
                "agent_id=... to the constructor instead. ACN's WebSocket "
                "endpoint is /ws/{agent_id}, not a generic channel.",
                DeprecationWarning,
                stacklevel=2,
            )
            target = channel
        if target is None:
            target = "default"

        self._set_state(WSState.CONNECTING)

        try:
            url = f"{self.base_url}/ws/{target}"
            connect_kwargs: dict[str, Any] = {}

            # Header auth: keep the secret out of access logs / Referer.
            # ``websockets`` >= 14 (asyncio.client) takes
            # ``additional_headers``; older releases used ``extra_headers``.
            # We pass the new name and accept that callers on very old
            # websockets versions need to upgrade — the import at the top
            # of this file already targets the modern API.
            if self.api_key and self.auth_mode == AuthMode.HEADER:
                connect_kwargs["additional_headers"] = [
                    ("Authorization", f"Bearer {self.api_key}")
                ]

            # Query auth: deprecated. The server enforces a feature flag,
            # so this only succeeds in dev. ``safe=""`` percent-encodes
            # *every* reserved character — notably ``/``, which the
            # default ``quote`` would leave alone and which would split
            # the path from the query string if it ever appeared in a key.
            if self.api_key and self.auth_mode == AuthMode.QUERY:
                url = f"{url}?token={quote(self.api_key, safe='')}"

            self._ws = await websockets.connect(url, **connect_kwargs)

            # First-message auth handshake. ACN expects:
            #   client → {"type":"auth","token":"<key>"}
            #   server → {"type":"auth_ok"}   (or close with 4401)
            # We do *not* set CONNECTED until the ack arrives — otherwise
            # the receive loop would race the auth response against
            # application messages.
            if self.api_key and self.auth_mode == AuthMode.FIRST_MESSAGE:
                await self._ws.send(json.dumps({"type": "auth", "token": self.api_key}))
                ack = await self._ws.recv()
                try:
                    ack_data = json.loads(ack) if isinstance(ack, str) else json.loads(ack.decode())
                except (json.JSONDecodeError, AttributeError) as exc:
                    await self._ws.close()
                    self._ws = None
                    raise ConnectionError(
                        f"ACN auth handshake: server sent non-JSON ack: {ack!r}"
                    ) from exc
                if ack_data.get("type") != "auth_ok":
                    await self._ws.close()
                    self._ws = None
                    raise ConnectionError(
                        f"ACN auth handshake failed: {ack_data!r}"
                    )

            self._set_state(WSState.CONNECTED)
            self._reconnect_attempts = 0

            # Start heartbeat and receive tasks
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            self._receive_task = asyncio.create_task(self._receive_loop(target))

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
                should_reconnect = (
                    self.options.auto_reconnect
                    and self._reconnect_attempts < self.options.max_reconnect_attempts
                )
                if should_reconnect:
                    await self._reconnect(channel)
                else:
                    self._set_state(WSState.DISCONNECTED)
                    break

    async def _reconnect(self, channel: str) -> None:
        """Attempt to reconnect to the same target."""
        self._set_state(WSState.RECONNECTING)
        self._reconnect_attempts += 1

        delay = self.options.reconnect_interval * min(self._reconnect_attempts, 5)
        await asyncio.sleep(delay)

        try:
            # If agent_id was set on the constructor, use the modern path
            # so we don't re-emit the legacy-channel DeprecationWarning on
            # every reconnect. Otherwise replay the original channel arg
            # but suppress the warning — the user has already been warned
            # once on the first connect().
            if self.agent_id is not None:
                await self.connect()
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    await self.connect(channel)
        except Exception:
            pass

    async def __aenter__(self) -> "ACNRealtime":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.disconnect()
