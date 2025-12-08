"""
Tests for ACN Communication Layer

Tests:
- Message Router (using official A2A SDK)
- Broadcast Service
- WebSocket Manager
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Official A2A SDK types
from a2a.types import Message, TextPart  # type: ignore[import-untyped]

from acn.communication import (
    BroadcastResult,
    BroadcastService,
    BroadcastStrategy,
    MessageRouter,
    WebSocketManager,
    create_data_message,
    create_notification_message,
    create_text_message,
)
from acn.registry import AgentRegistry


class TestHelperFunctions:
    """Test helper functions for creating A2A messages"""

    def test_create_text_message(self):
        """Test creating text message"""
        msg = create_text_message("Hello, agent!")

        assert msg.role == "user"
        assert len(msg.parts) == 1
        assert msg.message_id.startswith("msg-")

        # A2A SDK wraps parts in Part discriminated union
        part = msg.parts[0]
        # Access the actual TextPart via .root attribute
        text_part = part.root if hasattr(part, "root") else part
        assert text_part.text == "Hello, agent!"
        assert text_part.kind == "text"

    def test_create_data_message(self):
        """Test creating data message"""
        data = {"task_id": "task-123", "status": "working"}
        msg = create_data_message(data, text="Status update")

        assert msg.role == "user"
        assert len(msg.parts) == 2

        # First part is text
        text_part = msg.parts[0].root if hasattr(msg.parts[0], "root") else msg.parts[0]
        assert text_part.text == "Status update"

        # Second part is data
        data_part = msg.parts[1].root if hasattr(msg.parts[1], "root") else msg.parts[1]
        assert data_part.data == data

    def test_create_notification_message(self):
        """Test creating notification message"""
        msg = create_notification_message(
            notification_type="group_chat_mention",
            content="@CursorAgent 请开始开发",
            metadata={"chat_id": "chat-123", "from": "user-1"},
        )

        assert msg.role == "user"
        assert len(msg.parts) == 2

        # Text part
        text_part = msg.parts[0].root if hasattr(msg.parts[0], "root") else msg.parts[0]
        assert "@CursorAgent" in text_part.text

        # Data part with notification type
        data_part = msg.parts[1].root if hasattr(msg.parts[1], "root") else msg.parts[1]
        assert data_part.data["notification_type"] == "group_chat_mention"
        assert data_part.data["chat_id"] == "chat-123"


class TestMessageRouter:
    """Test MessageRouter class"""

    @pytest.fixture
    def mock_registry(self):
        """Create mock registry"""
        registry = AsyncMock(spec=AgentRegistry)

        # Mock get_agent to return agent info
        agent_info = MagicMock()
        agent_info.agent_id = "cursor-agent"
        agent_info.endpoint = "http://localhost:8001/a2a"
        registry.get_agent.return_value = agent_info

        # Mock search_agents
        registry.search_agents.return_value = [agent_info]

        return registry

    @pytest.fixture
    def mock_redis(self):
        """Create mock Redis client"""
        redis_mock = AsyncMock()
        redis_mock.zadd.return_value = True
        redis_mock.setex.return_value = True
        redis_mock.lpush.return_value = True
        redis_mock.zrevrange.return_value = []
        return redis_mock

    @pytest.fixture
    def router(self, mock_registry, mock_redis):
        """Create MessageRouter instance"""
        return MessageRouter(mock_registry, mock_redis)

    @pytest.mark.asyncio
    async def test_route_discovers_agent(self, router, mock_registry):
        """Test that router discovers agent via registry"""
        msg = create_text_message("Test message")

        # Mock A2A client
        with patch("acn.communication.message_router.A2AClient") as mock_client_class:
            # Create a response that can be serialized
            mock_response = Message(
                role="agent",
                parts=[TextPart(text="Response")],
                message_id="resp-123",
            )

            mock_client = AsyncMock()
            mock_client.send_message.return_value = mock_response
            mock_client_class.from_url.return_value = mock_client

            await router.route(
                from_agent="chat-service",
                to_agent="cursor-agent",
                message=msg,
            )

        # Verify registry was called
        mock_registry.get_agent.assert_called_once_with("cursor-agent")
        # Verify A2A client was used
        mock_client_class.from_url.assert_called_once()

    @pytest.mark.asyncio
    async def test_route_agent_not_found(self, router, mock_registry):
        """Test routing to non-existent agent"""
        mock_registry.get_agent.return_value = None

        msg = create_text_message("Test")

        with pytest.raises(ValueError, match="Agent not found"):
            await router.route(
                from_agent="chat-service",
                to_agent="unknown-agent",
                message=msg,
            )

    @pytest.mark.asyncio
    async def test_route_by_skill(self, router, mock_registry):
        """Test routing by skill discovery"""
        msg = create_text_message("Generate login page")

        with patch("acn.communication.message_router.A2AClient") as mock_client_class:
            mock_response = Message(
                role="agent",
                parts=[TextPart(text="Response")],
                message_id="resp-123",
            )

            mock_client = AsyncMock()
            mock_client.send_message.return_value = mock_response
            mock_client_class.from_url.return_value = mock_client

            await router.route_by_skill(
                from_agent="taskmaster",
                skills=["frontend", "react"],
                message=msg,
            )

        # Verify skill search was called
        mock_registry.search_agents.assert_called()

    @pytest.mark.asyncio
    async def test_register_handler(self, router):
        """Test handler registration"""
        handler_called = False

        async def test_handler(from_agent, message):
            nonlocal handler_called
            handler_called = True

        await router.register_handler("test_type", test_handler)

        # Verify handler is registered
        assert "test_type" in router._handlers
        assert test_handler in router._handlers["test_type"]


class TestBroadcastService:
    """Test BroadcastService class"""

    @pytest.fixture
    def mock_router(self):
        """Create mock router"""
        router_mock = AsyncMock(spec=MessageRouter)

        # Return a proper A2A Message response
        mock_response = Message(
            role="agent",
            parts=[TextPart(text="OK")],
            message_id="resp-123",
        )
        router_mock.route.return_value = mock_response

        # Mock registry
        router_mock.registry = AsyncMock()
        agent_info = MagicMock()
        agent_info.agent_id = "cursor-agent"
        router_mock.registry.search_agents.return_value = [agent_info]

        return router_mock

    @pytest.fixture
    def mock_redis(self):
        """Create mock Redis"""
        redis_mock = AsyncMock()
        redis_mock.setex.return_value = True
        return redis_mock

    @pytest.fixture
    def broadcast_service(self, mock_router, mock_redis):
        """Create BroadcastService instance"""
        return BroadcastService(mock_router, mock_redis)

    @pytest.mark.asyncio
    async def test_broadcast_parallel(self, broadcast_service, mock_router):
        """Test parallel broadcast"""
        msg = create_text_message("Hello all!")

        result = await broadcast_service.send(
            from_agent="chat-service",
            to_agents=["cursor-agent", "figma-agent", "backend-agent"],
            message=msg,
            strategy=BroadcastStrategy.PARALLEL,
        )

        assert isinstance(result, BroadcastResult)
        assert result.total == 3

    @pytest.mark.asyncio
    async def test_broadcast_by_skill(self, broadcast_service, mock_router):
        """Test broadcast by skill"""
        msg = create_text_message("Frontend update")

        result = await broadcast_service.send_by_skill(
            from_agent="taskmaster",
            skills=["frontend"],
            message=msg,
        )

        assert isinstance(result, BroadcastResult)
        mock_router.registry.search_agents.assert_called()

    @pytest.mark.asyncio
    async def test_broadcast_empty_agents(self, broadcast_service):
        """Test broadcast to empty agent list"""
        msg = create_text_message("Test")

        result = await broadcast_service.send(
            from_agent="chat-service",
            to_agents=[],
            message=msg,
        )

        assert result.total == 0
        assert result.success == 0


class TestWebSocketManager:
    """Test WebSocketManager class"""

    @pytest.fixture
    def mock_redis(self):
        """Create mock Redis"""
        redis_mock = AsyncMock()
        redis_mock.publish.return_value = True
        redis_mock.pubsub.return_value = AsyncMock()
        return redis_mock

    @pytest.fixture
    def ws_manager(self, mock_redis):
        """Create WebSocketManager instance"""
        return WebSocketManager(mock_redis)

    @pytest.mark.asyncio
    async def test_connect(self, ws_manager):
        """Test WebSocket connection"""
        mock_websocket = AsyncMock()

        conn_id = await ws_manager.connect(
            websocket=mock_websocket,
            user_id="user-123",
        )

        assert conn_id is not None
        assert len(conn_id) == 16
        mock_websocket.accept.assert_called_once()

    @pytest.mark.asyncio
    async def test_subscribe(self, ws_manager):
        """Test channel subscription"""
        mock_websocket = AsyncMock()
        conn_id = await ws_manager.connect(mock_websocket)

        await ws_manager.subscribe(conn_id, "chat:chat-123")

        # Verify subscription
        assert conn_id in ws_manager._connections
        assert "chat:chat-123" in ws_manager._connections[conn_id].subscriptions

    @pytest.mark.asyncio
    async def test_broadcast(self, ws_manager, mock_redis):
        """Test broadcast to channel"""
        # Connect and subscribe a client
        mock_websocket = AsyncMock()
        conn_id = await ws_manager.connect(mock_websocket)
        await ws_manager.subscribe(conn_id, "chat:chat-123")

        # Broadcast
        await ws_manager.broadcast(
            channel="chat:chat-123",
            message={"type": "message", "content": "Hello!"},
        )

        # Verify Redis publish was called
        mock_redis.publish.assert_called()

    @pytest.mark.asyncio
    async def test_disconnect(self, ws_manager):
        """Test WebSocket disconnection"""
        mock_websocket = AsyncMock()
        conn_id = await ws_manager.connect(mock_websocket)

        await ws_manager.disconnect(conn_id)

        assert conn_id not in ws_manager._connections

    def test_get_stats(self, ws_manager):
        """Test statistics"""
        stats = ws_manager.get_stats()

        assert "total_connections" in stats
        assert "total_channels" in stats
        assert stats["total_connections"] == 0


class TestA2ASDKIntegration:
    """Tests verifying proper use of official A2A SDK"""

    def test_message_type(self):
        """Verify we're using official A2A Message type"""
        msg = create_text_message("Test")

        # Should be official A2A Message, not our custom class
        assert type(msg).__module__.startswith("a2a")
        assert type(msg).__name__ == "Message"

    def test_message_has_id(self):
        """Verify message has auto-generated ID"""
        msg = create_text_message("Test")
        assert msg.message_id is not None
        assert msg.message_id.startswith("msg-")

    def test_message_parts_structure(self):
        """Verify message parts structure"""
        msg = create_text_message("Test")

        # Parts should be accessible
        assert len(msg.parts) == 1

        # Get the actual part (may be wrapped)
        part = msg.parts[0]
        actual_part = part.root if hasattr(part, "root") else part

        # Verify it's a TextPart
        assert actual_part.kind == "text"
        assert actual_part.text == "Test"
