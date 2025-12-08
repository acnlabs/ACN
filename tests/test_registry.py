"""
Tests for ACN Registry

Tests Agent registration, discovery, and management.
"""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from acn.registry import AgentRegistry


class TestAgentRegistry:
    """Tests for AgentRegistry class"""

    @pytest_asyncio.fixture
    async def mock_redis(self):
        """Create mock Redis client"""
        redis = AsyncMock()
        redis.hset = AsyncMock(return_value=True)
        redis.hgetall = AsyncMock(return_value={})
        redis.sadd = AsyncMock(return_value=1)
        redis.srem = AsyncMock(return_value=1)
        redis.smembers = AsyncMock(return_value=set())
        redis.sinter = AsyncMock(return_value=set())
        redis.exists = AsyncMock(return_value=1)
        redis.delete = AsyncMock(return_value=1)
        return redis

    @pytest_asyncio.fixture
    async def registry(self, mock_redis):
        """Create registry with mock Redis"""
        reg = AgentRegistry.__new__(AgentRegistry)
        reg.redis = mock_redis
        return reg

    @pytest.mark.asyncio
    async def test_register_agent_success(self, registry):
        """Test successful agent registration"""
        result = await registry.register_agent(
            agent_id="test-agent",
            name="Test Agent",
            endpoint="http://localhost:8001",
            skills=["testing", "demo"],
        )

        assert result is True
        registry.redis.hset.assert_called()
        registry.redis.sadd.assert_called()

    @pytest.mark.asyncio
    async def test_register_agent_with_agent_card(self, registry):
        """Test registration with custom Agent Card"""
        agent_card = {
            "protocolVersion": "0.3.0",
            "name": "Custom Agent",
            "url": "http://localhost:8001",
            "skills": [],
        }

        result = await registry.register_agent(
            agent_id="custom-agent",
            name="Custom Agent",
            endpoint="http://localhost:8001",
            skills=["custom"],
            agent_card=agent_card,
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_get_agent_found(self, registry):
        """Test getting existing agent"""
        import json
        from datetime import datetime

        registry.redis.hgetall = AsyncMock(
            return_value={
                "agent_id": "test-agent",
                "name": "Test Agent",
                "endpoint": "http://localhost:8001",
                "skills": json.dumps(["testing"]),
                "status": "online",
                "registered_at": datetime.now().isoformat(),
                "agent_card": json.dumps(
                    {
                        "protocolVersion": "0.3.0",
                        "name": "Test Agent",
                        "url": "http://localhost:8001",
                        "skills": [],
                    }
                ),
            }
        )

        agent = await registry.get_agent("test-agent")

        assert agent is not None
        assert agent.agent_id == "test-agent"
        assert agent.name == "Test Agent"

    @pytest.mark.asyncio
    async def test_get_agent_not_found(self, registry):
        """Test getting non-existent agent"""
        registry.redis.hgetall = AsyncMock(return_value={})

        agent = await registry.get_agent("non-existent")

        assert agent is None

    @pytest.mark.asyncio
    async def test_unregister_agent_success(self, registry):
        """Test successful agent unregistration"""
        import json

        registry.redis.hgetall = AsyncMock(
            return_value={
                "agent_id": "test-agent",
                "skills": json.dumps(["testing"]),
            }
        )

        result = await registry.unregister_agent("test-agent")

        assert result is True
        registry.redis.delete.assert_called()

    @pytest.mark.asyncio
    async def test_unregister_agent_not_found(self, registry):
        """Test unregistering non-existent agent"""
        registry.redis.hgetall = AsyncMock(return_value={})

        result = await registry.unregister_agent("non-existent")

        assert result is False

    @pytest.mark.asyncio
    async def test_heartbeat_success(self, registry):
        """Test successful heartbeat"""
        registry.redis.exists = AsyncMock(return_value=1)

        result = await registry.heartbeat("test-agent")

        assert result is True
        registry.redis.hset.assert_called()

    @pytest.mark.asyncio
    async def test_heartbeat_agent_not_found(self, registry):
        """Test heartbeat for non-existent agent"""
        registry.redis.exists = AsyncMock(return_value=0)

        result = await registry.heartbeat("non-existent")

        assert result is False

    @pytest.mark.asyncio
    async def test_search_agents_by_skills(self, registry):
        """Test searching agents by skills"""
        import json
        from datetime import datetime

        registry.redis.sinter = AsyncMock(return_value={"agent-1"})
        registry.redis.hgetall = AsyncMock(
            return_value={
                "agent_id": "agent-1",
                "name": "Agent 1",
                "endpoint": "http://localhost:8001",
                "skills": json.dumps(["coding"]),
                "status": "online",
                "registered_at": datetime.now().isoformat(),
                "agent_card": json.dumps(
                    {
                        "protocolVersion": "0.3.0",
                        "name": "Agent 1",
                        "url": "http://localhost:8001",
                        "skills": [],
                    }
                ),
            }
        )

        agents = await registry.search_agents(skills=["coding"])

        assert len(agents) == 1
        assert agents[0].agent_id == "agent-1"

    @pytest.mark.asyncio
    async def test_generate_agent_card(self, registry):
        """Test Agent Card generation"""
        card = registry._generate_agent_card(
            name="Test Agent",
            endpoint="http://localhost:8001",
            skills=["testing", "demo"],
        )

        assert card["protocolVersion"] == "0.3.0"
        assert card["name"] == "Test Agent"
        assert card["url"] == "http://localhost:8001"
        assert len(card["skills"]) == 2

    @pytest.mark.asyncio
    async def test_validate_agent_card_valid(self, registry):
        """Test valid Agent Card validation"""
        card = {
            "protocolVersion": "0.3.0",
            "name": "Test",
            "url": "http://localhost",
        }

        result = registry._validate_agent_card(card)

        assert result is True

    @pytest.mark.asyncio
    async def test_validate_agent_card_missing_field(self, registry):
        """Test Agent Card validation with missing field"""
        card = {
            "name": "Test",
        }

        with pytest.raises(ValueError):
            registry._validate_agent_card(card)


# =============================================================================
# Test Multi-Subnet Support
# =============================================================================


class TestMultiSubnetSupport:
    """Tests for multi-subnet agent registration"""

    @pytest_asyncio.fixture
    async def mock_redis(self):
        """Create mock Redis client"""
        redis = AsyncMock()
        redis.hset = AsyncMock(return_value=True)
        redis.hgetall = AsyncMock(return_value={})
        redis.sadd = AsyncMock(return_value=1)
        redis.srem = AsyncMock(return_value=1)
        redis.smembers = AsyncMock(return_value=set())
        redis.sinter = AsyncMock(return_value=set())
        redis.exists = AsyncMock(return_value=1)
        redis.delete = AsyncMock(return_value=1)
        return redis

    @pytest_asyncio.fixture
    async def registry(self, mock_redis):
        """Create registry with mock Redis"""
        reg = AgentRegistry.__new__(AgentRegistry)
        reg.redis = mock_redis
        return reg

    @pytest.mark.asyncio
    async def test_register_agent_default_subnet(self, registry):
        """Test agent registration with default subnet"""
        result = await registry.register_agent(
            agent_id="test-agent",
            name="Test Agent",
            endpoint="http://localhost:8001",
            skills=["testing"],
        )

        assert result is True
        # Should be registered to public subnet by default
        registry.redis.sadd.assert_called()

    @pytest.mark.asyncio
    async def test_register_agent_with_subnet_ids(self, registry):
        """Test agent registration with subnet_ids parameter"""
        result = await registry.register_agent(
            agent_id="multi-subnet-agent",
            name="Multi-Subnet Agent",
            endpoint="http://localhost:8001",
            skills=["coding"],
            subnet_ids=["public", "enterprise-a"],
        )

        assert result is True
        # Verify hset and sadd were called
        registry.redis.hset.assert_called()
        registry.redis.sadd.assert_called()

    @pytest.mark.asyncio
    async def test_get_agent_with_subnet_ids(self, registry):
        """Test getting agent returns subnet_ids list"""
        import json
        from datetime import datetime

        registry.redis.hgetall = AsyncMock(
            return_value={
                "agent_id": "test-agent",
                "name": "Test Agent",
                "endpoint": "http://localhost:8001",
                "skills": json.dumps(["testing"]),
                "subnet_ids": json.dumps(["public", "enterprise-a"]),
                "status": "online",
                "registered_at": datetime.now().isoformat(),
                "agent_card": json.dumps(
                    {
                        "protocolVersion": "0.3.0",
                        "name": "Test Agent",
                        "url": "http://localhost:8001",
                        "skills": [],
                    }
                ),
            }
        )

        agent = await registry.get_agent("test-agent")

        assert agent is not None
        assert agent.subnet_ids == ["public", "enterprise-a"]

    @pytest.mark.asyncio
    async def test_add_agent_to_subnet(self, registry):
        """Test adding agent to additional subnet"""
        import json
        from datetime import datetime

        # Mock existing agent
        registry.redis.hgetall = AsyncMock(
            return_value={
                "agent_id": "test-agent",
                "name": "Test Agent",
                "endpoint": "http://localhost:8001",
                "skills": json.dumps(["testing"]),
                "subnet_ids": json.dumps(["public"]),
                "status": "online",
                "registered_at": datetime.now().isoformat(),
                "agent_card": json.dumps({}),
            }
        )

        result = await registry.add_agent_to_subnet("test-agent", "enterprise-a")

        assert result is True
        registry.redis.sadd.assert_called()

    @pytest.mark.asyncio
    async def test_remove_agent_from_subnet(self, registry):
        """Test removing agent from a subnet"""
        import json
        from datetime import datetime

        # Mock agent in multiple subnets
        registry.redis.hgetall = AsyncMock(
            return_value={
                "agent_id": "test-agent",
                "name": "Test Agent",
                "endpoint": "http://localhost:8001",
                "skills": json.dumps(["testing"]),
                "subnet_ids": json.dumps(["public", "enterprise-a", "team-alpha"]),
                "status": "online",
                "registered_at": datetime.now().isoformat(),
                "agent_card": json.dumps({}),
            }
        )

        result = await registry.remove_agent_from_subnet("test-agent", "enterprise-a")

        assert result is True
        registry.redis.srem.assert_called()

    @pytest.mark.asyncio
    async def test_remove_agent_from_last_subnet_behavior(self, registry):
        """Test removing agent from subnet behavior"""
        import json
        from datetime import datetime

        # Mock agent in multiple subnets - removal should succeed
        registry.redis.hgetall = AsyncMock(
            return_value={
                "agent_id": "test-agent",
                "name": "Test Agent",
                "endpoint": "http://localhost:8001",
                "skills": json.dumps(["testing"]),
                "subnet_ids": json.dumps(["public", "enterprise-a"]),
                "status": "online",
                "registered_at": datetime.now().isoformat(),
                "agent_card": json.dumps({}),
            }
        )

        # Should succeed when agent has multiple subnets
        result = await registry.remove_agent_from_subnet("test-agent", "enterprise-a")
        assert result is True


# =============================================================================
# Test Payment Capability in Registry
# =============================================================================


class TestPaymentCapabilityInRegistry:
    """Tests for payment capability in agent registration"""

    @pytest_asyncio.fixture
    async def mock_redis(self):
        """Create mock Redis client"""
        redis = AsyncMock()
        redis.hset = AsyncMock(return_value=True)
        redis.hgetall = AsyncMock(return_value={})
        redis.sadd = AsyncMock(return_value=1)
        redis.srem = AsyncMock(return_value=1)
        redis.smembers = AsyncMock(return_value=set())
        redis.exists = AsyncMock(return_value=1)
        redis.set = AsyncMock(return_value=True)
        return redis

    @pytest_asyncio.fixture
    async def registry(self, mock_redis):
        """Create registry with mock Redis"""
        reg = AgentRegistry.__new__(AgentRegistry)
        reg.redis = mock_redis
        return reg

    @pytest.mark.asyncio
    async def test_register_agent_basic(self, registry):
        """Test basic agent registration"""
        result = await registry.register_agent(
            agent_id="test-agent",
            name="Test Agent",
            endpoint="http://localhost:8001",
            skills=["trading"],
        )

        assert result is True
        registry.redis.hset.assert_called()

    @pytest.mark.asyncio
    async def test_get_agent_with_stored_data(self, registry):
        """Test getting agent returns stored data correctly"""
        import json
        from datetime import datetime

        registry.redis.hgetall = AsyncMock(
            return_value={
                "agent_id": "payment-agent",
                "name": "Payment Agent",
                "endpoint": "http://localhost:8001",
                "skills": json.dumps(["coding"]),
                "subnet_ids": json.dumps(["public"]),
                "status": "online",
                "registered_at": datetime.now().isoformat(),
                "agent_card": json.dumps(
                    {
                        "name": "Payment Agent",
                        "url": "http://localhost:8001",
                        "protocolVersion": "0.3.0",
                    }
                ),
            }
        )

        agent = await registry.get_agent("payment-agent")

        assert agent is not None
        assert agent.agent_id == "payment-agent"
        assert agent.name == "Payment Agent"
        assert agent.skills == ["coding"]
