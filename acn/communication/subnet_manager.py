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
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

import redis.asyncio as redis
from a2a.types import Message  # type: ignore[import-untyped]
from fastapi import WebSocket, WebSocketDisconnect

from ..models import AgentInfo, SubnetInfo
from ..registry import AgentRegistry

logger = logging.getLogger(__name__)


class GatewayMessageType(str, Enum):
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
    ):
        """
        Initialize Subnet Manager

        Args:
            registry: ACN Registry for agent registration
            redis_client: Redis for state persistence
            gateway_base_url: Public URL of this gateway
            heartbeat_interval: Seconds between heartbeat checks
            heartbeat_timeout: Seconds before disconnecting stale agent
        """
        self.registry = registry
        self.redis = redis_client
        self.gateway_base_url = gateway_base_url.rstrip("/")
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout

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
                if token and token == subnet.generated_token:
                    return True

            elif scheme.type == "apiKey":
                # Validate API key
                api_key = credentials.get("api_key") or credentials.get("apiKey")
                if api_key and api_key == subnet.generated_token:
                    return True

            elif scheme.type == "openIdConnect":
                # OAuth validation would require external verification
                # For now, just check if access_token is provided
                # In production, would verify with OAuth provider
                access_token = credentials.get("access_token")
                if access_token:
                    # TODO: Verify with OAuth provider
                    logger.warning(f"OAuth validation not fully implemented for {subnet_id}")
                    return True

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
            data = await asyncio.wait_for(
                connection.websocket.receive_json(),
                timeout=timeout,
            )
        except TimeoutError as e:
            raise ValueError("Registration timeout") from e

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

        # Register in ACN (auto-generates Agent Card if not provided)
        await self.registry.register_agent(
            agent_id=connection.agent_id,
            name=agent_data.get("name", connection.agent_id),
            endpoint=gateway_endpoint,
            skills=agent_data.get("skills", []),
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
            skills=agent_data.get("skills", []),
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
            data = await connection.websocket.receive_json()
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
    ) -> dict[str, Any]:
        """
        Forward A2A request to subnet agent

        Args:
            subnet_id: Target subnet
            agent_id: Target agent
            message: A2A message
            timeout: Response timeout

        Returns:
            A2A response from agent
        """
        if subnet_id not in self._subnets:
            raise ValueError(f"Subnet not found: {subnet_id}")

        subnet = self._subnets[subnet_id]
        if agent_id not in subnet.connections:
            raise ValueError(f"Agent not connected: {subnet_id}/{agent_id}")

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
                    subnet_data = json.loads(data)
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

                        self._subnets[subnet_id] = Subnet(
                            info=SubnetInfo(**subnet_data),
                            generated_token=token,
                        )
                        logger.info(f"Loaded subnet from Redis: {subnet_id}")

            if cursor == 0:
                break
