"""
Subnet Manager (A2A Gateway)

ACN Communication Layer component for cross-subnet communication.
Supports multiple subnets, enabling agents in different private networks
to be accessed through the ACN gateway.

Architecture:
    ┌──────────────────────────────────────────────────────────────┐
    │                      ACN Gateway                              │
    │                    (Public Network)                          │
    │                                                               │
    │  ┌─────────────────────────────────────────────────────────┐ │
    │  │                  Subnet Manager                          │ │
    │  │                                                          │ │
    │  │  Subnet: enterprise-a          Subnet: enterprise-b     │ │
    │  │  ├── Agent A1                  ├── Agent B1             │ │
    │  │  └── Agent A2                  └── Agent B2             │ │
    │  │                                                          │ │
    │  │  Subnet: public (default)                                │ │
    │  │  └── Directly accessible agents                         │ │
    │  └─────────────────────────────────────────────────────────┘ │
    └──────────────────────────────────────────────────────────────┘

Usage:
    # Create subnet
    POST /api/v1/subnets
    {"subnet_id": "enterprise-a", "name": "Enterprise A"}

    # Agent connects to specific subnet
    WebSocket: /gateway/connect/{subnet_id}/{agent_id}

    # Send A2A message to subnet agent
    POST /gateway/a2a/{subnet_id}/{agent_id}
"""

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import redis.asyncio as redis
from a2a.compat.v0_3.types import Message  # type: ignore[import-untyped]
from fastapi import WebSocket, WebSocketDisconnect

from ...core.exceptions import PolicyRejected
from ...models import AgentInfo, SubnetInfo
from ..persistence.redis.registry import AgentRegistry

# ``PolicyCheckService`` is only referenced for type hints; importing
# the submodule directly avoids triggering ``services/__init__.py``
# during this module's import (services -> infrastructure -> services).
if TYPE_CHECKING:
    from ...services.allowlist_service import AllowlistService
    from ...services.policy_service import PolicyCheckService
    from .manifest_dispatcher import ManifestDispatcher

logger = logging.getLogger(__name__)

# M4 — inbound frame size cap for the subnet gateway WebSocket channel.
# ``receive_json()`` buffers the entire text payload before parsing; without
# an app-level cap a malicious agent can send a 1 GB registration or heartbeat
# frame and exhaust memory.  We receive as raw text, reject if oversized, then
# call json.loads() so the check runs before any deserialization work.
_SUBNET_WS_MAX_FRAME_BYTES: int = 1_048_576  # 1 MiB — matches HTTP body cap


class GatewayMessageType(StrEnum):
    """Gateway protocol message types"""

    REGISTER = "register"
    REGISTER_ACK = "register_ack"
    A2A_REQUEST = "a2a_request"
    A2A_RESPONSE = "a2a_response"
    HEARTBEAT = "heartbeat"
    HEARTBEAT_ACK = "heartbeat_ack"
    ERROR = "error"


@dataclass
class GatewayConnection:
    """WebSocket connection from subnet agent"""

    connection_id: str
    subnet_id: str
    agent_id: str
    websocket: WebSocket
    agent_info: AgentInfo | None = None
    connected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_heartbeat: datetime = field(default_factory=lambda: datetime.now(UTC))
    pending_requests: dict[str, asyncio.Future] = field(default_factory=dict)


@dataclass
class Subnet:
    """Subnet information and connections"""

    info: SubnetInfo
    # For bearer/apiKey auth: stored token/key for validation
    generated_token: str | None = None
    connections: dict[str, GatewayConnection] = field(default_factory=dict)


class SubnetManager:
    """
    A2A Gateway - Multi-Subnet Manager

    Manages multiple subnets, each containing agents from different
    private networks.

    Features:
    - Create/delete subnets dynamically
    - Agents connect to specific subnets
    - Isolation between subnets (agents only see their subnet)
    - Cross-subnet communication via MessageRouter

    Usage:
        subnet_manager = SubnetManager(registry, redis_client)

        # Create subnet
        await subnet_manager.create_subnet("enterprise-a", "Enterprise A")

        # Agent connects
        @app.websocket("/gateway/connect/{subnet_id}/{agent_id}")
        async def gateway_ws(ws, subnet_id, agent_id):
            await subnet_manager.handle_connection(ws, subnet_id, agent_id)

        # Forward A2A request
        @app.post("/gateway/a2a/{subnet_id}/{agent_id}")
        async def gateway_a2a(subnet_id, agent_id, message):
            return await subnet_manager.forward_request(subnet_id, agent_id, message)
    """

    # Default subnet for backwards compatibility
    DEFAULT_SUBNET = "public"

    def __init__(
        self,
        registry: AgentRegistry,
        redis_client: redis.Redis,
        gateway_base_url: str = "https://gateway.agentplanet.com",
        heartbeat_interval: int = 30,
        heartbeat_timeout: int = 90,
        policy_service: "PolicyCheckService | None" = None,
        manifest_dispatcher: "ManifestDispatcher | None" = None,
        allowlist_service: "AllowlistService | None" = None,
    ):
        """
        Initialize Subnet Manager

        Args:
            registry: ACN Registry for agent registration
            redis_client: Redis for state persistence
            gateway_base_url: Public URL of this gateway
            heartbeat_interval: Seconds between heartbeat checks
            heartbeat_timeout: Seconds before disconnecting stale agent
            policy_service: Optional gateway-level access control. When
                provided, ``forward_request()`` short-circuits with
                ``PolicyRejected`` for recipients whose
                ``communication_policy`` denies the sender. ``None``
                preserves pre-Phase-1 behaviour (rollout opt-out used
                by legacy fixtures and the api.py wiring transition).
            manifest_dispatcher: Phase 2 PR #1 review fix (P0-A1) —
                shared helper for manifest-mode divert. When the
                policy decision yields ``route_to == "manifest"``,
                ``forward_request`` calls into the dispatcher
                instead of pushing the message via WebSocket. This
                mirrors what ``MessageRouter`` already does on the
                HTTP path so manifest semantics are uniform across
                ingress channels. Defaulting to ``None`` keeps
                policy-only legacy fixtures working — manifest mode
                will then surface a clear ``RuntimeError`` rather
                than silently bypass.
            allowlist_service: Phase 2 PR #2 allowlist trust list.
                Threaded into ``PolicyCheckService.check_inbound``
                as the ``is_in_allowlist`` callback so subnet-bound
                ingress (gateway A2A path) honours allowlist mode
                identically to the HTTP path. ``None`` falls back to
                policy_service's "missing callback → divert to
                manifest" safety branch — same shape as
                ``MessageRouter`` for symmetry.
        """
        self.registry = registry
        self.redis = redis_client
        self.gateway_base_url = gateway_base_url.rstrip("/")
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.policy_service = policy_service
        self.manifest_dispatcher = manifest_dispatcher
        self.allowlist_service = allowlist_service

        # Subnets: {subnet_id: Subnet}
        self._subnets: dict[str, Subnet] = {}

        # Background tasks
        self._heartbeat_task: asyncio.Task | None = None
        self._running = False

        # Create default public subnet
        self._subnets[self.DEFAULT_SUBNET] = Subnet(
            info=SubnetInfo(
                subnet_id=self.DEFAULT_SUBNET,
                name="Public Network",
                owner="backend@internal",
                description="Default subnet for public agents",
            )
        )

        logger.info(f"Subnet Manager initialized (gateway: {gateway_base_url})")

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self):
        """Start background tasks"""
        if self._running:
            return

        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("Subnet Manager started")

    async def stop(self):
        """Stop and cleanup"""
        self._running = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # Disconnect all agents in all subnets
        for subnet_id in list(self._subnets.keys()):
            subnet = self._subnets[subnet_id]
            for agent_id in list(subnet.connections.keys()):
                await self._disconnect(subnet_id, agent_id, "Gateway shutting down")

        logger.info("Subnet Manager stopped")

    # =========================================================================
    # Subnet Management
    # =========================================================================

    async def create_subnet(
        self,
        subnet_id: str,
        name: str,
        description: str | None = None,
        security_schemes: dict | None = None,
        default_security: list[str] | None = None,
        metadata: dict | None = None,
        owner: str = "backend@internal",
    ) -> tuple[SubnetInfo, str | None]:
        """
        Create a new subnet with A2A-style security

        Args:
            subnet_id: Unique subnet identifier
            name: Human-readable subnet name
            description: Optional description
            security_schemes: A2A-style security schemes (None = public)
            default_security: Which schemes to require
            metadata: Optional metadata

        Returns:
            Tuple of (SubnetInfo, generated_token)
            - generated_token is only returned for bearer/apiKey auth

        Raises:
            ValueError: If subnet already exists

        Examples:
            # Public subnet
            await create_subnet("public-demo", "Public")

            # Bearer token auth
            await create_subnet(
                "team-a", "Team A",
                security_schemes={"bearer": {"type": "http", "scheme": "bearer"}}
            )
        """
        if subnet_id in self._subnets:
            raise ValueError(f"Subnet already exists: {subnet_id}")

        # Parse and validate security schemes
        parsed_schemes = None
        if security_schemes:
            from ..models import SecurityScheme

            parsed_schemes = {
                name: SecurityScheme(**scheme) for name, scheme in security_schemes.items()
            }

        info = SubnetInfo(
            subnet_id=subnet_id,
            name=name,
            owner=owner,
            description=description,
            security_schemes=parsed_schemes,
            default_security=default_security,
            metadata=metadata or {},
        )

        # Generate token for bearer/apiKey auth
        generated_token = None
        if security_schemes:
            for _scheme_name, scheme in security_schemes.items():
                if scheme.get("type") == "http" and scheme.get("scheme") == "bearer":
                    # Generate bearer token
                    import secrets

                    generated_token = f"sk_subnet_{secrets.token_urlsafe(32)}"
                    break
                elif scheme.get("type") == "apiKey":
                    # Generate API key
                    import secrets

                    generated_token = f"ak_subnet_{secrets.token_urlsafe(32)}"
                    break

        self._subnets[subnet_id] = Subnet(
            info=info,
            generated_token=generated_token,
        )

        # Persist to Redis
        await self._persist_subnet(info, generated_token)

        is_public = security_schemes is None
        logger.info(f"Created subnet: {subnet_id} (public={is_public})")
        return info, generated_token

    def is_subnet_public(self, subnet_id: str) -> bool:
        """Check if subnet is public (no auth required)"""
        if subnet_id not in self._subnets:
            return False
        return self._subnets[subnet_id].info.security_schemes is None

    async def validate_credentials(
        self,
        subnet_id: str,
        credentials: dict | None,
    ) -> bool:
        """
        Validate credentials for joining a subnet

        Args:
            subnet_id: Subnet to join
            credentials: Authentication credentials
                - For bearer: {"token": "sk_subnet_xxx"}
                - For apiKey: {"api_key": "ak_subnet_xxx"}
                - For OAuth: {"access_token": "oauth_token"}

        Returns:
            True if valid, False otherwise
        """
        if subnet_id not in self._subnets:
            return False

        subnet = self._subnets[subnet_id]

        # Public subnet - no auth needed
        if subnet.info.security_schemes is None:
            return True

        if not credentials:
            return False

        # Check each security scheme
        for _scheme_name, scheme in subnet.info.security_schemes.items():
            if scheme.type == "http" and scheme.scheme == "bearer":
                # Validate bearer token
                token = credentials.get("token") or credentials.get("bearer")
                if token and secrets.compare_digest(token, subnet.generated_token):
                    return True

            elif scheme.type == "apiKey":
                # Validate API key
                api_key = credentials.get("api_key") or credentials.get("apiKey")
                if api_key and api_key == subnet.generated_token:
                    return True

            elif scheme.type in ("openIdConnect", "oauth2"):
                # openIdConnect / oauth2 subnet creation is blocked at the API layer.
                # This branch handles any subnets that existed before that restriction
                # was introduced. Deny access until token introspection is implemented.
                # Tracked: https://github.com/acnlabs/ACN/issues/9
                logger.warning(
                    "Subnet %s uses unsupported auth type '%s'. Access denied.",
                    subnet_id,
                    scheme.type,
                )
                return False

        return False

    async def delete_subnet(self, subnet_id: str, force: bool = False):
        """
        Delete a subnet

        Args:
            subnet_id: Subnet to delete
            force: If True, disconnect all agents first

        Raises:
            ValueError: If subnet doesn't exist or has connected agents
        """
        if subnet_id == self.DEFAULT_SUBNET:
            raise ValueError("Cannot delete default subnet")

        if subnet_id not in self._subnets:
            raise ValueError(f"Subnet not found: {subnet_id}")

        subnet = self._subnets[subnet_id]

        if subnet.connections and not force:
            raise ValueError(
                f"Subnet has {len(subnet.connections)} connected agents. "
                "Use force=True to disconnect them."
            )

        # Disconnect all agents
        for agent_id in list(subnet.connections.keys()):
            await self._disconnect(subnet_id, agent_id, "Subnet deleted")

        # Remove subnet
        del self._subnets[subnet_id]

        # Remove from Redis
        await self._remove_subnet_state(subnet_id)

        logger.info(f"Deleted subnet: {subnet_id}")

    def get_subnet(self, subnet_id: str) -> SubnetInfo | None:
        """Get subnet info"""
        if subnet_id not in self._subnets:
            return None
        return self._subnets[subnet_id].info

    def list_subnets(self) -> list[SubnetInfo]:
        """List all subnets"""
        return [subnet.info for subnet in self._subnets.values()]

    def subnet_exists(self, subnet_id: str) -> bool:
        """Check if subnet exists"""
        return subnet_id in self._subnets

    # =========================================================================
    # Connection Handling
    # =========================================================================

    async def handle_connection(
        self,
        websocket: WebSocket,
        subnet_id: str,
        agent_id: str,
        credentials: dict | None = None,
    ):
        """
        Handle WebSocket connection from subnet agent

        Args:
            websocket: FastAPI WebSocket
            subnet_id: Subnet to join
            agent_id: Agent identifier
            credentials: Authentication credentials (for non-public subnets)
                - bearer: {"token": "sk_subnet_xxx"}
                - apiKey: {"api_key": "ak_subnet_xxx"}
                - oauth: {"access_token": "..."}
        """
        # Validate subnet exists
        if subnet_id not in self._subnets:
            await websocket.close(code=4004, reason=f"Subnet not found: {subnet_id}")
            return

        # Validate credentials for non-public subnets
        if not self.is_subnet_public(subnet_id):
            if not await self.validate_credentials(subnet_id, credentials):
                await websocket.close(code=4001, reason="Authentication required for this subnet")
                return

        await websocket.accept()
        connection_id = str(uuid4())

        logger.info(f"Agent connecting: {subnet_id}/{agent_id} (authenticated)")

        connection = GatewayConnection(
            connection_id=connection_id,
            subnet_id=subnet_id,
            agent_id=agent_id,
            websocket=websocket,
        )

        try:
            # Wait for registration
            await self._handle_registration(connection)

            # Store connection
            self._subnets[subnet_id].connections[agent_id] = connection

            # Message loop
            await self._message_loop(connection)

        except WebSocketDisconnect:
            logger.info(f"Agent disconnected: {subnet_id}/{agent_id}")
        except Exception as e:
            logger.error(f"Connection error for {subnet_id}/{agent_id}: {e}")
            await self._send_error(websocket, str(e))
        finally:
            await self._disconnect(subnet_id, agent_id)

    async def _handle_registration(
        self,
        connection: GatewayConnection,
        timeout: float = 30.0,
    ):
        """Wait for and process registration message"""
        try:
            # M4: receive as raw text first so we can enforce the frame size
            # cap before any JSON parsing work is done.
            raw = await asyncio.wait_for(
                connection.websocket.receive_text(),
                timeout=timeout,
            )
        except TimeoutError as e:
            raise ValueError("Registration timeout") from e

        if len(raw.encode("utf-8")) > _SUBNET_WS_MAX_FRAME_BYTES:
            raise ValueError(
                f"Registration frame too large: {len(raw.encode('utf-8'))} bytes "
                f"(limit {_SUBNET_WS_MAX_FRAME_BYTES})"
            )
        data = json.loads(raw)

        if data.get("type") != GatewayMessageType.REGISTER:
            raise ValueError(f"Expected REGISTER, got {data.get('type')}")

        # Build agent info with gateway endpoint
        agent_data = data.get("agent_info", {})
        gateway_endpoint = (
            f"{self.gateway_base_url}/gateway/a2a/{connection.subnet_id}/{connection.agent_id}"
        )

        # Prepare metadata
        metadata = {
            **agent_data.get("metadata", {}),
            "gateway": self.gateway_base_url,
            "subnet_id": connection.subnet_id,
            "connection_type": "gateway",
        }

        # Read: support both new "tags" and legacy "skills" key from gateway payload
        _agent_tags = agent_data.get("tags") or agent_data.get("skills", [])

        # Register in ACN (auto-generates Agent Card if not provided)
        await self.registry.register_agent(
            agent_id=connection.agent_id,
            name=agent_data.get("name", connection.agent_id),
            endpoint=gateway_endpoint,
            tags=_agent_tags,
            agent_card=agent_data.get("agent_card"),  # May be None, will be auto-generated
            subnet_id=connection.subnet_id,
            description=agent_data.get("description", ""),
            metadata=metadata,
        )

        # Build agent info for local cache
        agent_info = AgentInfo(
            agent_id=connection.agent_id,
            name=agent_data.get("name", connection.agent_id),
            description=agent_data.get("description", ""),
            tags=_agent_tags,  # AgentInfo.tags field (not renamed)
            endpoint=gateway_endpoint,
            status="online",
            subnet_id=connection.subnet_id,
            metadata=metadata,
        )

        connection.agent_info = agent_info

        # Acknowledge
        await connection.websocket.send_json(
            {
                "type": GatewayMessageType.REGISTER_ACK,
                "agent_id": connection.agent_id,
                "subnet_id": connection.subnet_id,
                "gateway_endpoint": gateway_endpoint,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

        logger.info(
            f"Agent registered: {connection.subnet_id}/{connection.agent_id} -> {gateway_endpoint}"
        )

    async def _message_loop(self, connection: GatewayConnection):
        """Process messages from subnet agent"""
        while True:
            # M4: receive as text first, enforce size cap, then parse.
            raw = await connection.websocket.receive_text()
            if len(raw.encode("utf-8")) > _SUBNET_WS_MAX_FRAME_BYTES:
                logger.warning(
                    "subnet_ws_frame_too_large",
                    subnet_id=connection.subnet_id,
                    agent_id=connection.agent_id,
                    frame_bytes=len(raw.encode("utf-8")),
                    limit=_SUBNET_WS_MAX_FRAME_BYTES,
                )
                await self._send_error(
                    connection.websocket, "frame_too_large: message exceeds 1 MiB limit"
                )
                await connection.websocket.close(code=4400, reason="frame_too_large")
                return
            data = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == GatewayMessageType.HEARTBEAT:
                connection.last_heartbeat = datetime.now(UTC)
                await connection.websocket.send_json(
                    {
                        "type": GatewayMessageType.HEARTBEAT_ACK,
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                )

            elif msg_type == GatewayMessageType.A2A_RESPONSE:
                request_id = data.get("request_id")
                if request_id in connection.pending_requests:
                    future = connection.pending_requests.pop(request_id)
                    if not future.done():
                        future.set_result(data.get("response", {}))

            else:
                logger.debug(
                    f"Unhandled message from {connection.subnet_id}/"
                    f"{connection.agent_id}: {msg_type}"
                )

    async def _disconnect(
        self,
        subnet_id: str,
        agent_id: str,
        reason: str = "",
    ):
        """Cleanup disconnected agent"""
        if subnet_id not in self._subnets:
            return

        subnet = self._subnets[subnet_id]
        if agent_id not in subnet.connections:
            return

        connection = subnet.connections.pop(agent_id)

        # Cancel pending requests
        for future in connection.pending_requests.values():
            if not future.done():
                future.set_exception(ConnectionError(f"Agent disconnected: {reason}"))

        # Unregister from ACN
        try:
            await self.registry.unregister_agent(agent_id)
        except Exception as e:
            logger.warning(f"Failed to unregister {agent_id}: {e}")

        # Close WebSocket
        try:
            await connection.websocket.close()
        except Exception:
            pass

        logger.info(f"Agent cleaned up: {subnet_id}/{agent_id}")

    async def _send_error(self, websocket: WebSocket, error: str):
        """Send error to agent"""
        try:
            await websocket.send_json(
                {
                    "type": GatewayMessageType.ERROR,
                    "error": error,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )
        except Exception:
            pass

    # =========================================================================
    # Message Forwarding
    # =========================================================================

    async def forward_request(
        self,
        subnet_id: str,
        agent_id: str,
        message: Message | dict[str, Any],
        timeout: float = 30.0,
        *,
        from_agent: str | None = None,
    ) -> dict[str, Any]:
        """
        Forward A2A request to subnet agent

        Args:
            subnet_id: Target subnet
            agent_id: Target agent
            message: A2A message
            timeout: Response timeout
            from_agent: Sender agent id (or reserved ``system:<slug>``
                namespace). Optional + keyword-only — older callers
                that predate Step 2.3 keep working; the policy gate
                treats ``None`` as a non-system unknown sender, which
                fails closed under ``closed`` and passes under ``open``.
                A2A protocol callers should plumb the value from
                ``context.metadata.from_agent``.

        Returns:
            A2A response from agent on the open/inbox path; an
            envelope dict ``{"status": "sent", "delivery_mode":
            "manifest", "mid": ..., "ts": ...}`` when the recipient's
            policy is ``manifest`` and the message was diverted into
            the manifest queue.

        Raises:
            ValueError: subnet or agent not found / not connected.
            PolicyRejected: recipient's ``communication_policy``
                denies the sender. Raised before any WebSocket frame
                is sent — the recipient never sees the rejected
                request.
            RuntimeError: recipient is in manifest mode but the
                subnet manager was constructed without a
                ``manifest_dispatcher``. Fail loudly rather than
                silently bypass the divert.
        """
        # Design decision (Phase 3): ``attention_fee`` is intentionally
        # NOT supported on the subnet forward path. Subnets represent
        # an operator-curated trust circle — agents join by invitation
        # and share credentials via the gateway handshake. The
        # ``attention_fee`` mechanism is designed for *open-internet*
        # communication between unknown agents, where the fee signals
        # credibility and compensates the recipient for review effort.
        # Inside a subnet that contract is already satisfied by
        # membership itself. Supporting fees on this path would add
        # escrow complexity without delivering meaningful trust benefit.
        # Senders who want to attach a fee should route via the HTTP
        # ``POST /communication/send`` path instead.
        if subnet_id not in self._subnets:
            raise ValueError(f"Subnet not found: {subnet_id}")

        subnet = self._subnets[subnet_id]
        if agent_id not in subnet.connections:
            raise ValueError(f"Agent not connected: {subnet_id}/{agent_id}")

        # Gateway-level access control (Phase 1) + manifest divert (Phase 2 PR #1).
        #
        # The cached ``connection.agent_info`` is built from the
        # client-supplied register payload at gateway-connect time and
        # does NOT carry ``communication_policy``. We therefore re-fetch
        # the canonical AgentInfo from the registry on every forward —
        # this also ensures that policy changes (open ↔ closed ↔
        # manifest) take effect immediately for already-connected
        # agents, without requiring a reconnect.
        #
        # If the registry lookup misses (e.g. Redis flake or the
        # connected agent has not yet been persisted), we fall back to
        # ``policy=None`` which the service treats as ``open`` — the
        # WebSocket connection itself already proves the agent's
        # presence so failing closed here would manufacture outages.
        #
        # PR #1 review fix (P0-A1): we now use ``check_inbound`` (not
        # ``..._or_raise``) so we can branch on ``decision.route_to``.
        # Manifest mode → divert into the manifest queue + push WS
        # notification, identical to the HTTP/A2A path. Closed mode →
        # raise ``PolicyRejected`` (preserves the Phase 1 surface).
        if self.policy_service is not None:
            policy: dict[str, Any] | None = None
            try:
                fresh_info = await self.registry.get_agent(agent_id)
                if fresh_info is not None:
                    policy = fresh_info.communication_policy
            except Exception as exc:  # noqa: BLE001 — see fall-through note above
                logger.warning(
                    "subnet_policy_lookup_failed agent_id=%s error=%s",
                    agent_id,
                    exc,
                )

            # Bind the allowlist callback only when wired. With the
            # callback absent, ``check_inbound`` will fail-closed
            # to manifest for ``mode=allowlist`` (see policy_service
            # "missing callback" branch) — same direction as the
            # HTTP path so behaviour is uniform across ingress.
            is_in_allowlist = (
                self.allowlist_service.is_member
                if self.allowlist_service is not None
                else None
            )
            decision = await self.policy_service.check_inbound(
                sender_id=from_agent or "unknown",
                recipient_id=agent_id,
                recipient_policy=policy,
                is_in_allowlist=is_in_allowlist,
            )
            if not decision.allow:
                # Mirror ``check_inbound_or_raise`` semantics — the
                # caller (gateway HTTP handler / A2A protocol
                # router) already maps ``PolicyRejected`` to the
                # canonical 403 / TaskState.rejected response.
                assert decision.reason is not None, (
                    "PolicyDecision(allow=False) must always carry a reason"
                )
                raise PolicyRejected(
                    reason=decision.reason,
                    reject_reason=decision.reject_reason,
                    recipient_id=agent_id,
                )
            if decision.route_to == "manifest":
                if self.manifest_dispatcher is None:
                    # Same fail-loud reasoning as the router-side
                    # branch: silently dropping or fail-open routing
                    # to the WS push would defeat the recipient's
                    # opt-in. A clear RuntimeError surfaces the
                    # missing wiring at the smallest possible blast
                    # radius (one subnet send), and production
                    # wiring (acn/api.py) always installs one.
                    raise RuntimeError(
                        "manifest mode requested on subnet path but "
                        "ManifestDispatcher is not wired; construct "
                        "SubnetManager with manifest_dispatcher=..."
                    )
                # Coerce the subnet-side message — historically a
                # plain dict — into an A2A Message so the dispatcher
                # can extract a summary from its TextParts. The
                # dispatcher itself falls back to a placeholder if
                # the parts list is empty, so an unparseable dict
                # still produces a non-blank manifest entry.
                a2a_message = (
                    message
                    if isinstance(message, Message)
                    else Message.model_validate(message)
                    if hasattr(Message, "model_validate")
                    else message
                )
                entry = await self.manifest_dispatcher.dispatch(
                    owner_id=agent_id,
                    sender_id=from_agent or "unknown",
                    message=a2a_message,
                    path="subnet",
                )
                # Subnet callers historically got an A2A response
                # dict. Returning the manifest envelope keeps the
                # shape contract identical to the HTTP path so any
                # caller that already understands manifest divert
                # (Phase 2 SDK) recognises both ingress channels
                # uniformly.
                return {
                    "status": "sent",
                    "delivery_mode": "manifest",
                    "mid": entry.mid,
                    "ts": entry.ts_ms,
                }

        connection = subnet.connections[agent_id]
        request_id = str(uuid4())

        future: asyncio.Future = asyncio.Future()
        connection.pending_requests[request_id] = future

        try:
            message_dict = message.model_dump() if hasattr(message, "model_dump") else message

            await connection.websocket.send_json(
                {
                    "type": GatewayMessageType.A2A_REQUEST,
                    "request_id": request_id,
                    "message": message_dict,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )

            return await asyncio.wait_for(future, timeout=timeout)

        except TimeoutError:
            connection.pending_requests.pop(request_id, None)
            raise TimeoutError(f"Response timeout: {subnet_id}/{agent_id}") from None
        except Exception:
            connection.pending_requests.pop(request_id, None)
            raise

    # =========================================================================
    # Heartbeat
    # =========================================================================

    async def _heartbeat_loop(self):
        """Check agent heartbeats periodically"""
        while self._running:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                await self._check_heartbeats()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")

    async def _check_heartbeats(self):
        """Disconnect stale agents"""
        now = datetime.now(UTC)
        stale = []

        for subnet_id, subnet in self._subnets.items():
            for agent_id, conn in subnet.connections.items():
                elapsed = (now - conn.last_heartbeat).total_seconds()
                if elapsed > self.heartbeat_timeout:
                    stale.append((subnet_id, agent_id))
                    logger.warning(
                        f"Agent {subnet_id}/{agent_id} heartbeat timeout ({elapsed:.0f}s)"
                    )

        for subnet_id, agent_id in stale:
            await self._disconnect(subnet_id, agent_id, "Heartbeat timeout")

    # =========================================================================
    # Query Methods
    # =========================================================================

    def is_connected(self, subnet_id: str, agent_id: str) -> bool:
        """Check if agent is connected in subnet"""
        if subnet_id not in self._subnets:
            return False
        return agent_id in self._subnets[subnet_id].connections

    def get_subnet_agents(self, subnet_id: str) -> list[str]:
        """Get list of connected agents in subnet"""
        if subnet_id not in self._subnets:
            return []
        return list(self._subnets[subnet_id].connections.keys())

    def get_all_agents(self) -> dict[str, list[str]]:
        """Get all connected agents by subnet"""
        return {
            subnet_id: list(subnet.connections.keys())
            for subnet_id, subnet in self._subnets.items()
        }

    def get_connection_info(
        self,
        subnet_id: str,
        agent_id: str,
    ) -> dict[str, Any] | None:
        """Get connection info for agent"""
        if subnet_id not in self._subnets:
            return None

        subnet = self._subnets[subnet_id]
        if agent_id not in subnet.connections:
            return None

        conn = subnet.connections[agent_id]
        return {
            "agent_id": agent_id,
            "subnet_id": subnet_id,
            "connection_id": conn.connection_id,
            "connected_at": conn.connected_at.isoformat(),
            "last_heartbeat": conn.last_heartbeat.isoformat(),
            "pending_requests": len(conn.pending_requests),
            "agent_info": conn.agent_info.model_dump() if conn.agent_info else None,
        }

    def get_stats(self) -> dict[str, Any]:
        """Get gateway statistics"""
        total_agents = sum(len(subnet.connections) for subnet in self._subnets.values())

        return {
            "gateway_url": self.gateway_base_url,
            "total_subnets": len(self._subnets),
            "total_agents": total_agents,
            "heartbeat_interval": self.heartbeat_interval,
            "heartbeat_timeout": self.heartbeat_timeout,
            "subnets": [
                {
                    "subnet_id": subnet_id,
                    "name": subnet.info.name,
                    "agent_count": len(subnet.connections),
                    "agents": [
                        {
                            "agent_id": agent_id,
                            "connected_at": conn.connected_at.isoformat(),
                        }
                        for agent_id, conn in subnet.connections.items()
                    ],
                }
                for subnet_id, subnet in self._subnets.items()
            ],
        }

    # =========================================================================
    # Redis Persistence
    # =========================================================================

    async def _persist_subnet(self, info: SubnetInfo, generated_token: str | None = None):
        """Persist subnet info to Redis"""
        import json

        key = f"acn:subnet:{info.subnet_id}"
        await self.redis.set(key, json.dumps(info.model_dump(), default=str))

        # Store token separately (for security)
        if generated_token:
            token_key = f"acn:subnet:{info.subnet_id}:token"
            await self.redis.set(token_key, generated_token)

    async def _remove_subnet_state(self, subnet_id: str):
        """Remove subnet state from Redis"""
        await self.redis.delete(f"acn:subnet:{subnet_id}")
        await self.redis.delete(f"acn:subnet:{subnet_id}:token")

    async def load_subnets_from_redis(self):
        """Load persisted subnets from Redis on startup"""
        import json

        pattern = "acn:subnet:*"
        cursor = 0

        while True:
            cursor, keys = await self.redis.scan(cursor, match=pattern)
            for key in keys:
                # Skip token keys
                if key.endswith(":token"):
                    continue

                data = await self.redis.get(key)
                if data:
                    try:
                        subnet_data = json.loads(data)
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(
                            f"subnet_manager: skipping corrupted subnet key during restore: {key}"
                        )
                        continue
                    subnet_id = subnet_data["subnet_id"]

                    if subnet_id not in self._subnets:
                        # Load token if exists
                        token = await self.redis.get(f"acn:subnet:{subnet_id}:token")

                        # Parse security_schemes if present
                        security_schemes = subnet_data.get("security_schemes")
                        if security_schemes:
                            from ..models import SecurityScheme

                            subnet_data["security_schemes"] = {
                                name: SecurityScheme(**scheme)
                                for name, scheme in security_schemes.items()
                            }

                        # Backfill owner for legacy Redis records persisted
                        # before SubnetInfo modelled the field. ACN treats
                        # missing owners as system-owned (consistent with the
                        # default Public Network).
                        subnet_data.setdefault("owner", "backend@internal")

                        self._subnets[subnet_id] = Subnet(
                            info=SubnetInfo(**subnet_data),
                            generated_token=token,
                        )
                        logger.info(f"Loaded subnet from Redis: {subnet_id}")

            if cursor == 0:
                break
