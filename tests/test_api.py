"""
Tests for ACN API

Tests REST API endpoints.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from acn.api import app


class TestACNAPI:
    """Tests for ACN API endpoints"""

    @pytest.fixture
    def mock_registry(self):
        """Create mock registry"""
        registry = AsyncMock()
        registry.register_agent = AsyncMock(return_value=True)
        registry.get_agent = AsyncMock(return_value=None)
        registry.get_agent_card = AsyncMock(return_value=None)
        registry.search_agents = AsyncMock(return_value=[])
        registry.unregister_agent = AsyncMock(return_value=True)
        registry.heartbeat = AsyncMock(return_value=True)
        registry.redis = AsyncMock()
        return registry

    @pytest.mark.asyncio
    async def test_root_endpoint(self, mock_registry):
        """Test root endpoint"""
        with patch("acn.api.registry", mock_registry):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/")

        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "ACN - Agent Collaboration Network"
        assert "version" in data

    @pytest.mark.asyncio
    async def test_health_endpoint(self, mock_registry):
        """Test health check endpoint"""
        with patch("acn.api.registry", mock_registry):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_register_agent_success(self, mock_registry):
        """Test successful agent registration"""
        with patch("acn.api.registry", mock_registry):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/agents/register",
                    json={
                        "agent_id": "test-agent",
                        "name": "Test Agent",
                        "endpoint": "http://localhost:8001",
                        "skills": ["testing"],
                    },
                )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "registered"
        assert data["agent_id"] == "test-agent"

    @pytest.mark.asyncio
    async def test_get_agent_not_found(self, mock_registry):
        """Test getting non-existent agent"""
        mock_registry.get_agent = AsyncMock(return_value=None)

        with patch("acn.api.registry", mock_registry):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/v1/agents/non-existent")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_search_agents_empty(self, mock_registry):
        """Test searching agents with no results"""
        mock_registry.search_agents = AsyncMock(return_value=[])

        with patch("acn.api.registry", mock_registry):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/v1/agents")

        assert response.status_code == 200
        data = response.json()
        assert data["agents"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_unregister_agent_success(self, mock_registry):
        """Test successful agent unregistration"""
        with patch("acn.api.registry", mock_registry):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.delete("/api/v1/agents/test-agent")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "unregistered"

    @pytest.mark.asyncio
    async def test_heartbeat_success(self, mock_registry):
        """Test successful heartbeat"""
        with patch("acn.api.registry", mock_registry):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/api/v1/agents/test-agent/heartbeat")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_get_stats(self, mock_registry):
        """Test stats endpoint"""
        mock_registry.redis.smembers = AsyncMock(return_value={"agent-1", "agent-2"})
        mock_registry.redis.hget = AsyncMock(return_value="online")

        with patch("acn.api.registry", mock_registry):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/v1/stats")

        assert response.status_code == 200
        data = response.json()
        assert "total_agents" in data

    @pytest.mark.asyncio
    async def test_list_skills(self, mock_registry):
        """Test skills listing"""
        mock_registry.redis.keys = AsyncMock(
            return_value=["acn:skills:coding", "acn:skills:design"]
        )
        mock_registry.redis.scard = AsyncMock(return_value=1)

        with patch("acn.api.registry", mock_registry):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/v1/skills")

        assert response.status_code == 200
        data = response.json()
        assert "skills" in data
        assert "total_skills" in data


# =============================================================================
# Test Multi-Subnet API Endpoints
# =============================================================================


class TestSubnetAPI:
    """Tests for subnet-related API endpoints"""

    @pytest.fixture
    def mock_registry(self):
        """Create mock registry"""
        registry = AsyncMock()
        registry.register_agent = AsyncMock(return_value=True)
        registry.get_agent = AsyncMock(return_value=None)
        registry.add_agent_to_subnet = AsyncMock(return_value=True)
        registry.remove_agent_from_subnet = AsyncMock(return_value=True)
        registry.search_agents = AsyncMock(return_value=[])
        registry.redis = AsyncMock()
        return registry

    @pytest.fixture
    def mock_subnet_manager(self):
        """Create mock subnet manager"""
        manager = AsyncMock()
        manager.create_subnet = AsyncMock(
            return_value=(
                AsyncMock(
                    subnet_id="test-subnet",
                    name="Test Subnet",
                    description="A test subnet",
                ),
                "sk_subnet_test123",
            )
        )
        manager.get_subnet = AsyncMock(return_value=None)
        manager.list_subnets = AsyncMock(return_value=[])
        manager.delete_subnet = AsyncMock(return_value=True)
        manager.subnet_exists = AsyncMock(return_value=True)
        return manager

    @pytest.mark.asyncio
    async def test_register_agent_with_multiple_subnets(self, mock_registry, mock_subnet_manager):
        """Test registering agent with multiple subnets"""
        with (
            patch("acn.api.registry", mock_registry),
            patch("acn.api.subnet_manager", mock_subnet_manager),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/agents/register",
                    json={
                        "agent_id": "multi-agent",
                        "name": "Multi-Subnet Agent",
                        "endpoint": "http://localhost:8001",
                        "skills": ["coding"],
                        "subnet_ids": ["public", "enterprise-a"],
                    },
                )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "registered"

    @pytest.mark.asyncio
    async def test_join_subnet(self, mock_registry, mock_subnet_manager):
        """Test agent joining a subnet"""
        mock_agent = AsyncMock()
        mock_agent.agent_id = "test-agent"
        mock_agent.subnet_ids = ["public"]
        mock_registry.get_agent = AsyncMock(return_value=mock_agent)

        with (
            patch("acn.api.registry", mock_registry),
            patch("acn.api.subnet_manager", mock_subnet_manager),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/api/v1/agents/test-agent/subnets/enterprise-a")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "joined"

    @pytest.mark.asyncio
    async def test_leave_subnet(self, mock_registry, mock_subnet_manager):
        """Test agent leaving a subnet"""
        mock_agent = AsyncMock()
        mock_agent.agent_id = "test-agent"
        mock_agent.subnet_ids = ["public", "enterprise-a"]
        mock_registry.get_agent = AsyncMock(return_value=mock_agent)

        with (
            patch("acn.api.registry", mock_registry),
            patch("acn.api.subnet_manager", mock_subnet_manager),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.delete("/api/v1/agents/test-agent/subnets/enterprise-a")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "left"

    @pytest.mark.asyncio
    async def test_get_agent_subnets(self, mock_registry):
        """Test getting agent's subnets"""
        mock_agent = AsyncMock()
        mock_agent.agent_id = "test-agent"
        mock_agent.subnet_ids = ["public", "enterprise-a", "team-alpha"]
        mock_registry.get_agent = AsyncMock(return_value=mock_agent)

        with patch("acn.api.registry", mock_registry):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/v1/agents/test-agent/subnets")

        assert response.status_code == 200
        data = response.json()
        assert data["agent_id"] == "test-agent"
        assert "public" in data["subnet_ids"]


# =============================================================================
# Test Payment API Endpoints
# =============================================================================


class TestPaymentAPI:
    """Tests for payment-related API endpoints"""

    @pytest.fixture
    def mock_registry(self):
        """Create mock registry"""
        registry = AsyncMock()
        registry.get_agent = AsyncMock(return_value=None)
        registry.search_agents = AsyncMock(return_value=[])
        registry.redis = AsyncMock()
        return registry

    @pytest.fixture
    def mock_payment_discovery(self):
        """Create mock payment discovery service"""
        service = AsyncMock()
        service.find_agents_accepting_payment = AsyncMock(return_value=["agent-1"])
        service.get_agent_payment_capability = AsyncMock(return_value=None)
        service.index_payment_capability = AsyncMock()
        return service

    @pytest.fixture
    def mock_payment_task_manager(self):
        """Create mock payment task manager"""
        manager = AsyncMock()
        manager.create_payment_task = AsyncMock()
        manager.get_payment_task = AsyncMock(return_value=None)
        manager.update_task_status = AsyncMock()
        manager.get_agent_tasks = AsyncMock(return_value=[])
        manager.get_payment_stats = AsyncMock(
            return_value={
                "total_transactions": 0,
                "total_amount_usd": "0.00",
            }
        )
        return manager

    @pytest.mark.asyncio
    async def test_set_payment_capability(self, mock_registry, mock_payment_discovery):
        """Test setting agent's payment capability"""
        mock_agent = AsyncMock()
        mock_agent.agent_id = "payment-agent"
        mock_registry.get_agent = AsyncMock(return_value=mock_agent)

        with (
            patch("acn.api.registry", mock_registry),
            patch("acn.api.payment_discovery", mock_payment_discovery),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/agents/payment-agent/payment-capability",
                    json={
                        "accepts_payment": True,
                        "payment_methods": ["usdc", "eth"],
                        "wallet_address": "0xabc123",
                        "supported_networks": ["base"],
                        "default_currency": "USD",
                    },
                )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_discover_payment_agents(self, mock_registry, mock_payment_discovery):
        """Test discovering agents by payment capability"""
        mock_payment_discovery.find_agents_accepting_payment = AsyncMock(
            return_value=["agent-1", "agent-2"]
        )

        mock_agent = AsyncMock()
        mock_agent.agent_id = "agent-1"
        mock_agent.name = "Agent 1"
        mock_agent.model_dump = lambda: {
            "agent_id": "agent-1",
            "name": "Agent 1",
        }
        mock_registry.get_agent = AsyncMock(return_value=mock_agent)

        with (
            patch("acn.api.registry", mock_registry),
            patch("acn.api.payment_discovery", mock_payment_discovery),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    "/api/v1/payments/discover",
                    params={"payment_method": "usdc", "network": "base"},
                )

        assert response.status_code == 200
        data = response.json()
        assert "agents" in data

    # Note: Payment task and stats API tests require full backend integration
    # These tests are covered by unit tests in test_payments.py
