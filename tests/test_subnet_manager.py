"""
Tests for Subnet Manager (A2A Gateway) - Multi-Subnet Support

Tests the gateway functionality for cross-subnet communication.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.communication.subnet_manager import (
    GatewayConnection,
    GatewayMessageType,
    SubnetManager,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_registry():
    """Create mock registry"""
    registry = MagicMock()
    registry.register_agent = AsyncMock(return_value=True)
    registry.unregister_agent = AsyncMock(return_value=True)
    registry.get_agent = AsyncMock(return_value=None)
    return registry


@pytest.fixture
def mock_redis():
    """Create mock Redis client"""
    redis = AsyncMock()
    redis.set = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.delete = AsyncMock()
    redis.scan = AsyncMock(return_value=(0, []))
    return redis


@pytest.fixture
def subnet_manager(mock_registry, mock_redis):
    """Create SubnetManager instance"""
    return SubnetManager(
        registry=mock_registry,
        redis_client=mock_redis,
        gateway_base_url="https://gateway.example.com",
        heartbeat_interval=30,
        heartbeat_timeout=90,
    )


@pytest.fixture
def mock_websocket():
    """Create mock WebSocket"""
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.receive_json = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    return ws


# =============================================================================
# Test GatewayMessageType
# =============================================================================


class TestGatewayMessageType:
    """Test gateway message types"""

    def test_message_types_exist(self):
        """Test all message types are defined"""
        assert GatewayMessageType.REGISTER == "register"
        assert GatewayMessageType.REGISTER_ACK == "register_ack"
        assert GatewayMessageType.A2A_REQUEST == "a2a_request"
        assert GatewayMessageType.A2A_RESPONSE == "a2a_response"
        assert GatewayMessageType.HEARTBEAT == "heartbeat"
        assert GatewayMessageType.HEARTBEAT_ACK == "heartbeat_ack"
        assert GatewayMessageType.ERROR == "error"


# =============================================================================
# Test GatewayConnection
# =============================================================================


class TestGatewayConnection:
    """Test gateway connection dataclass"""

    def test_create_connection(self, mock_websocket):
        """Test creating a gateway connection"""
        conn = GatewayConnection(
            connection_id="conn-123",
            subnet_id="test-subnet",
            agent_id="test-agent",
            websocket=mock_websocket,
        )

        assert conn.connection_id == "conn-123"
        assert conn.subnet_id == "test-subnet"
        assert conn.agent_id == "test-agent"
        assert conn.websocket == mock_websocket
        assert conn.agent_info is None
        assert isinstance(conn.connected_at, datetime)
        assert isinstance(conn.last_heartbeat, datetime)
        assert conn.pending_requests == {}


# =============================================================================
# Test SubnetManager Initialization
# =============================================================================


class TestSubnetManagerInit:
    """Test SubnetManager initialization"""

    def test_init(self, subnet_manager):
        """Test initialization"""
        assert subnet_manager.gateway_base_url == "https://gateway.example.com"
        assert subnet_manager.heartbeat_interval == 30
        assert subnet_manager.heartbeat_timeout == 90
        assert subnet_manager._running is False

        # Default subnet should exist
        assert SubnetManager.DEFAULT_SUBNET in subnet_manager._subnets
        assert subnet_manager._subnets[SubnetManager.DEFAULT_SUBNET].info.name == "Public Network"

    @pytest.mark.asyncio
    async def test_start_stop(self, subnet_manager):
        """Test start and stop lifecycle"""
        await subnet_manager.start()
        assert subnet_manager._running is True
        assert subnet_manager._heartbeat_task is not None

        await subnet_manager.stop()
        assert subnet_manager._running is False


# =============================================================================
# Test Subnet Management
# =============================================================================


class TestSubnetManagement:
    """Test subnet creation and management"""

    @pytest.mark.asyncio
    async def test_create_subnet(self, subnet_manager):
        """Test creating a new subnet (public)"""
        info, token = await subnet_manager.create_subnet(
            subnet_id="enterprise-a",
            name="Enterprise A",
            description="Test enterprise subnet",
            metadata={"tier": "premium"},
        )

        assert info.subnet_id == "enterprise-a"
        assert info.name == "Enterprise A"
        assert info.description == "Test enterprise subnet"
        assert info.metadata == {"tier": "premium"}
        assert info.security_schemes is None  # Public subnet
        assert token is None  # No token for public subnet

        # Subnet should be stored
        assert "enterprise-a" in subnet_manager._subnets

    @pytest.mark.asyncio
    async def test_create_subnet_with_bearer_auth(self, subnet_manager):
        """Test creating a subnet with bearer token auth"""
        info, token = await subnet_manager.create_subnet(
            subnet_id="private-team",
            name="Private Team",
            security_schemes={"bearer": {"type": "http", "scheme": "bearer"}},
        )

        assert info.subnet_id == "private-team"
        assert info.security_schemes is not None
        assert "bearer" in info.security_schemes
        assert token is not None
        assert token.startswith("sk_subnet_")

        # Stored token should match
        assert subnet_manager._subnets["private-team"].generated_token == token

    @pytest.mark.asyncio
    async def test_create_duplicate_subnet(self, subnet_manager):
        """Test creating duplicate subnet raises error"""
        await subnet_manager.create_subnet("test-subnet", "Test")

        with pytest.raises(ValueError, match="already exists"):
            await subnet_manager.create_subnet("test-subnet", "Test 2")

    @pytest.mark.asyncio
    async def test_delete_subnet(self, subnet_manager):
        """Test deleting a subnet"""
        await subnet_manager.create_subnet("to-delete", "To Delete")
        assert "to-delete" in subnet_manager._subnets

        await subnet_manager.delete_subnet("to-delete")
        assert "to-delete" not in subnet_manager._subnets

    @pytest.mark.asyncio
    async def test_delete_default_subnet(self, subnet_manager):
        """Test cannot delete default subnet"""
        with pytest.raises(ValueError, match="Cannot delete default"):
            await subnet_manager.delete_subnet(SubnetManager.DEFAULT_SUBNET)

    @pytest.mark.asyncio
    async def test_delete_subnet_with_agents(self, subnet_manager, mock_websocket):
        """Test cannot delete subnet with connected agents"""
        await subnet_manager.create_subnet("has-agents", "Has Agents")

        # Add a mock connection
        conn = GatewayConnection(
            connection_id="conn-1",
            subnet_id="has-agents",
            agent_id="agent-1",
            websocket=mock_websocket,
        )
        subnet_manager._subnets["has-agents"].connections["agent-1"] = conn

        # Should fail without force
        with pytest.raises(ValueError, match="has .* connected agents"):
            await subnet_manager.delete_subnet("has-agents")

        # Should succeed with force
        await subnet_manager.delete_subnet("has-agents", force=True)
        assert "has-agents" not in subnet_manager._subnets

    def test_get_subnet(self, subnet_manager):
        """Test getting subnet info"""
        info = subnet_manager.get_subnet(SubnetManager.DEFAULT_SUBNET)
        assert info is not None
        assert info.subnet_id == SubnetManager.DEFAULT_SUBNET

        # Non-existent
        assert subnet_manager.get_subnet("nonexistent") is None

    def test_list_subnets(self, subnet_manager):
        """Test listing subnets"""
        subnets = subnet_manager.list_subnets()
        assert len(subnets) == 1  # Default subnet
        assert subnets[0].subnet_id == SubnetManager.DEFAULT_SUBNET

    def test_subnet_exists(self, subnet_manager):
        """Test subnet existence check"""
        assert subnet_manager.subnet_exists(SubnetManager.DEFAULT_SUBNET) is True
        assert subnet_manager.subnet_exists("nonexistent") is False


# =============================================================================
# Test Agent Registration (Multi-Subnet)
# =============================================================================


class TestAgentRegistration:
    """Test agent registration flow"""

    @pytest.mark.asyncio
    async def test_registration_success(self, subnet_manager, mock_websocket, mock_registry):
        """Test successful agent registration in subnet"""
        await subnet_manager.create_subnet("enterprise-a", "Enterprise A")

        mock_websocket.receive_json = AsyncMock(
            side_effect=[
                {
                    "type": "register",
                    "agent_info": {
                        "name": "Test Agent",
                        "description": "A test agent",
                        "skills": ["testing"],
                    },
                },
                Exception("Connection closed"),
            ]
        )

        conn = GatewayConnection(
            connection_id="conn-123",
            subnet_id="enterprise-a",
            agent_id="test-agent",
            websocket=mock_websocket,
        )

        await subnet_manager._handle_registration(conn)

        # Verify agent info
        assert conn.agent_info is not None
        assert conn.agent_info.agent_id == "test-agent"
        assert conn.agent_info.metadata["subnet_id"] == "enterprise-a"
        assert (
            conn.agent_info.endpoint
            == "https://gateway.example.com/gateway/a2a/enterprise-a/test-agent"
        )

        # Verify ack contains subnet_id
        ack = mock_websocket.send_json.call_args[0][0]
        assert ack["type"] == "register_ack"
        assert ack["subnet_id"] == "enterprise-a"

    @pytest.mark.asyncio
    async def test_registration_timeout(self, subnet_manager, mock_websocket):
        """Test registration timeout"""

        async def slow_receive():
            await asyncio.sleep(100)
            return {}

        mock_websocket.receive_json = slow_receive

        conn = GatewayConnection(
            connection_id="conn-123",
            subnet_id="public",
            agent_id="test-agent",
            websocket=mock_websocket,
        )

        with pytest.raises(ValueError, match="timeout"):
            await subnet_manager._handle_registration(conn, timeout=0.01)


# =============================================================================
# Test Message Forwarding (Multi-Subnet)
# =============================================================================


class TestMessageForwarding:
    """Test message forwarding to subnet agents"""

    @pytest.mark.asyncio
    async def test_forward_request_success(self, subnet_manager, mock_websocket):
        """Test successful message forwarding"""
        await subnet_manager.create_subnet("test-subnet", "Test")

        conn = GatewayConnection(
            connection_id="conn-123",
            subnet_id="test-subnet",
            agent_id="test-agent",
            websocket=mock_websocket,
        )
        subnet_manager._subnets["test-subnet"].connections["test-agent"] = conn

        async def mock_response():
            await asyncio.sleep(0.01)
            for _request_id, future in list(conn.pending_requests.items()):
                if not future.done():
                    future.set_result({"status": "success"})
                    break

        asyncio.create_task(mock_response())

        response = await subnet_manager.forward_request(
            subnet_id="test-subnet",
            agent_id="test-agent",
            message={"role": "user", "content": "Hello"},
            timeout=1.0,
        )

        assert response == {"status": "success"}

    @pytest.mark.asyncio
    async def test_forward_request_subnet_not_found(self, subnet_manager):
        """Test forwarding to non-existent subnet"""
        with pytest.raises(ValueError, match="Subnet not found"):
            await subnet_manager.forward_request(
                subnet_id="nonexistent",
                agent_id="agent",
                message={},
            )

    @pytest.mark.asyncio
    async def test_forward_request_agent_not_connected(self, subnet_manager):
        """Test forwarding to non-connected agent"""
        await subnet_manager.create_subnet("test-subnet", "Test")

        with pytest.raises(ValueError, match="Agent not connected"):
            await subnet_manager.forward_request(
                subnet_id="test-subnet",
                agent_id="unknown-agent",
                message={},
            )


# =============================================================================
# Test Query Methods (Multi-Subnet)
# =============================================================================


class TestQueryMethods:
    """Test query methods"""

    @pytest.mark.asyncio
    async def test_is_connected(self, subnet_manager, mock_websocket):
        """Test is_connected check with subnet"""
        await subnet_manager.create_subnet("test-subnet", "Test")

        assert subnet_manager.is_connected("test-subnet", "test-agent") is False

        conn = GatewayConnection(
            connection_id="conn-123",
            subnet_id="test-subnet",
            agent_id="test-agent",
            websocket=mock_websocket,
        )
        subnet_manager._subnets["test-subnet"].connections["test-agent"] = conn

        assert subnet_manager.is_connected("test-subnet", "test-agent") is True
        assert subnet_manager.is_connected("other-subnet", "test-agent") is False

    @pytest.mark.asyncio
    async def test_get_subnet_agents(self, subnet_manager, mock_websocket):
        """Test getting agents in a subnet"""
        await subnet_manager.create_subnet("test-subnet", "Test")

        assert subnet_manager.get_subnet_agents("test-subnet") == []

        for i in range(3):
            conn = GatewayConnection(
                connection_id=f"conn-{i}",
                subnet_id="test-subnet",
                agent_id=f"agent-{i}",
                websocket=mock_websocket,
            )
            subnet_manager._subnets["test-subnet"].connections[f"agent-{i}"] = conn

        agents = subnet_manager.get_subnet_agents("test-subnet")
        assert len(agents) == 3
        assert "agent-0" in agents

    @pytest.mark.asyncio
    async def test_get_all_agents(self, subnet_manager, mock_websocket):
        """Test getting all agents by subnet"""
        await subnet_manager.create_subnet("subnet-a", "A")
        await subnet_manager.create_subnet("subnet-b", "B")

        # Add agents to different subnets
        for subnet_id, count in [("subnet-a", 2), ("subnet-b", 3)]:
            for i in range(count):
                conn = GatewayConnection(
                    connection_id=f"{subnet_id}-conn-{i}",
                    subnet_id=subnet_id,
                    agent_id=f"{subnet_id}-agent-{i}",
                    websocket=mock_websocket,
                )
                subnet_manager._subnets[subnet_id].connections[f"{subnet_id}-agent-{i}"] = conn

        all_agents = subnet_manager.get_all_agents()
        assert len(all_agents["subnet-a"]) == 2
        assert len(all_agents["subnet-b"]) == 3

    @pytest.mark.asyncio
    async def test_get_connection_info(self, subnet_manager, mock_websocket):
        """Test getting connection info"""
        await subnet_manager.create_subnet("test-subnet", "Test")

        assert subnet_manager.get_connection_info("test-subnet", "test-agent") is None

        conn = GatewayConnection(
            connection_id="conn-123",
            subnet_id="test-subnet",
            agent_id="test-agent",
            websocket=mock_websocket,
        )
        subnet_manager._subnets["test-subnet"].connections["test-agent"] = conn

        info = subnet_manager.get_connection_info("test-subnet", "test-agent")
        assert info is not None
        assert info["agent_id"] == "test-agent"
        assert info["subnet_id"] == "test-subnet"

    @pytest.mark.asyncio
    async def test_get_stats(self, subnet_manager, mock_websocket):
        """Test getting gateway stats"""
        await subnet_manager.create_subnet("subnet-a", "A")

        conn = GatewayConnection(
            connection_id="conn-1",
            subnet_id="subnet-a",
            agent_id="agent-1",
            websocket=mock_websocket,
        )
        subnet_manager._subnets["subnet-a"].connections["agent-1"] = conn

        stats = subnet_manager.get_stats()
        assert stats["gateway_url"] == "https://gateway.example.com"
        assert stats["total_subnets"] == 2  # default + subnet-a
        assert stats["total_agents"] == 1


# =============================================================================
# Test Disconnect (Multi-Subnet)
# =============================================================================


class TestDisconnect:
    """Test agent disconnection"""

    @pytest.mark.asyncio
    async def test_disconnect_cleanup(self, subnet_manager, mock_websocket, mock_registry):
        """Test disconnect cleans up properly"""
        await subnet_manager.create_subnet("test-subnet", "Test")

        conn = GatewayConnection(
            connection_id="conn-123",
            subnet_id="test-subnet",
            agent_id="test-agent",
            websocket=mock_websocket,
        )
        subnet_manager._subnets["test-subnet"].connections["test-agent"] = conn

        future: asyncio.Future = asyncio.Future()
        conn.pending_requests["req-1"] = future

        await subnet_manager._disconnect("test-subnet", "test-agent", "test reason")

        assert "test-agent" not in subnet_manager._subnets["test-subnet"].connections
        assert future.done()

        mock_registry.unregister_agent.assert_called_once_with("test-agent")
        mock_websocket.close.assert_called_once()


# =============================================================================
# Test Heartbeat (Multi-Subnet)
# =============================================================================


class TestHeartbeat:
    """Test heartbeat management"""

    @pytest.mark.asyncio
    async def test_check_heartbeats_removes_stale(
        self, subnet_manager, mock_websocket, mock_registry
    ):
        """Test stale agents are disconnected"""
        await subnet_manager.create_subnet("test-subnet", "Test")

        conn = GatewayConnection(
            connection_id="conn-123",
            subnet_id="test-subnet",
            agent_id="test-agent",
            websocket=mock_websocket,
        )
        conn.last_heartbeat = datetime(2020, 1, 1, tzinfo=UTC)
        subnet_manager._subnets["test-subnet"].connections["test-agent"] = conn

        await subnet_manager._check_heartbeats()

        assert "test-agent" not in subnet_manager._subnets["test-subnet"].connections
