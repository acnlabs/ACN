"""
Tests for ACN Payment Integration

Tests payment discovery, task management, and webhook functionality.
"""

from unittest.mock import AsyncMock

import pytest

from acn.payments.core import (
    PaymentCapability,
    PaymentDiscoveryService,
    PaymentTask,
    PaymentTaskManager,
    PaymentTaskStatus,
    SupportedNetwork,
    SupportedPaymentMethod,
    create_payment_capability,
)
from acn.payments.webhook import (
    WebhookConfig,
    WebhookDelivery,
    WebhookEventType,
    WebhookService,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_redis():
    """Create mock Redis client"""
    redis = AsyncMock()
    redis.sadd = AsyncMock(return_value=1)
    redis.srem = AsyncMock(return_value=1)
    redis.smembers = AsyncMock(return_value=set())
    redis.sinter = AsyncMock(return_value=set())
    redis.set = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=None)
    redis.delete = AsyncMock(return_value=1)
    redis.hset = AsyncMock(return_value=1)
    redis.hget = AsyncMock(return_value=None)
    redis.hgetall = AsyncMock(return_value={})
    redis.lpush = AsyncMock(return_value=1)
    redis.lrange = AsyncMock(return_value=[])
    redis.expire = AsyncMock(return_value=True)
    redis.incr = AsyncMock(return_value=1)
    redis.incrby = AsyncMock(return_value=1)
    return redis


@pytest.fixture
def mock_registry():
    """Create mock agent registry"""
    registry = AsyncMock()
    registry.get_agent = AsyncMock(return_value=None)
    return registry


@pytest.fixture
def mock_webhook_service():
    """Create mock webhook service"""
    service = AsyncMock(spec=WebhookService)
    service.send_notification = AsyncMock()
    return service


@pytest.fixture
def payment_discovery(mock_redis):
    """Create PaymentDiscoveryService instance"""
    return PaymentDiscoveryService(mock_redis)


@pytest.fixture
def payment_task_manager(mock_redis, payment_discovery, mock_webhook_service):
    """Create PaymentTaskManager instance"""
    return PaymentTaskManager(
        redis=mock_redis,
        discovery=payment_discovery,
        webhook_service=mock_webhook_service,
    )


@pytest.fixture
def sample_capability():
    """Create sample payment capability"""
    return PaymentCapability(
        accepts_payment=True,
        payment_methods=[SupportedPaymentMethod.USDC, SupportedPaymentMethod.ETH],
        wallet_address="0x1234567890abcdef1234567890abcdef12345678",
        supported_networks=[SupportedNetwork.BASE, SupportedNetwork.ETHEREUM],
        default_currency="USD",
        pricing={"coding": "50.00", "testing": "25.00"},
    )


# =============================================================================
# Test PaymentCapability
# =============================================================================


class TestPaymentCapability:
    """Test PaymentCapability model"""

    def test_create_default(self):
        """Test creating default payment capability"""
        cap = PaymentCapability()
        assert cap.accepts_payment is False
        assert cap.payment_methods == []
        assert cap.wallet_address is None
        assert cap.default_currency == "USD"

    def test_create_with_crypto(self):
        """Test creating capability with crypto"""
        cap = PaymentCapability(
            accepts_payment=True,
            payment_methods=[SupportedPaymentMethod.USDC],
            wallet_address="0xabc123",
            supported_networks=[SupportedNetwork.BASE],
        )
        assert cap.accepts_payment is True
        assert SupportedPaymentMethod.USDC in cap.payment_methods
        assert SupportedNetwork.BASE in cap.supported_networks

    def test_to_agent_card_extension(self, sample_capability):
        """Test converting to Agent Card extension"""
        ext = sample_capability.to_agent_card_extension()

        assert "ap2" in ext
        assert ext["ap2"]["accepts_payment"] is True
        assert "usdc" in ext["ap2"]["payment_methods"]
        assert "eth" in ext["ap2"]["payment_methods"]
        assert ext["ap2"]["wallet_address"] == sample_capability.wallet_address


class TestCreatePaymentCapability:
    """Test create_payment_capability factory function"""

    def test_create_crypto_capability(self):
        """Test creating crypto payment capability"""
        cap = create_payment_capability(
            payment_methods=["usdc"],
            wallet_address="0xabc123",
            networks=["base"],
        )
        assert cap.accepts_payment is True
        assert cap.wallet_address == "0xabc123"

    def test_create_traditional_capability(self):
        """Test creating traditional payment capability"""
        cap = create_payment_capability(
            payment_methods=["credit_card"],
        )
        assert cap.accepts_payment is True
        assert SupportedPaymentMethod.CREDIT_CARD in cap.payment_methods

    def test_create_platform_credits_capability(self):
        """Test creating platform credits capability"""
        cap = create_payment_capability(
            payment_methods=["platform_credits"],
        )
        assert SupportedPaymentMethod.PLATFORM_CREDITS in cap.payment_methods


# =============================================================================
# Test PaymentTask
# =============================================================================


class TestPaymentTask:
    """Test PaymentTask model"""

    def test_create_task(self):
        """Test creating a payment task"""
        task = PaymentTask(
            buyer_agent="buyer-123",
            seller_agent="seller-456",
            task_description="Write unit tests",
            amount="100.00",
            currency="USD",
        )
        assert task.task_id is not None
        assert task.buyer_agent == "buyer-123"
        assert task.seller_agent == "seller-456"
        assert task.status == PaymentTaskStatus.CREATED
        assert task.amount == "100.00"

    def test_task_with_crypto_payment(self):
        """Test task with crypto payment details"""
        task = PaymentTask(
            buyer_agent="buyer",
            seller_agent="seller",
            task_description="Deploy smart contract",
            amount="0.5",
            currency="ETH",
            payment_method=SupportedPaymentMethod.ETH,
            network=SupportedNetwork.BASE,
            recipient_wallet="0xabc123",
        )
        assert task.payment_method == SupportedPaymentMethod.ETH
        assert task.network == SupportedNetwork.BASE


# =============================================================================
# Test PaymentDiscoveryService
# =============================================================================


class TestPaymentDiscoveryService:
    """Test PaymentDiscoveryService"""

    @pytest.mark.asyncio
    async def test_index_payment_capability(self, payment_discovery, sample_capability, mock_redis):
        """Test indexing payment capability"""
        await payment_discovery.index_payment_capability("agent-1", sample_capability)

        # Should index by payment methods
        assert mock_redis.sadd.call_count >= 2  # USDC, ETH

        # Should index by networks
        assert any("by_network:base" in str(call) for call in mock_redis.sadd.call_args_list)

    @pytest.mark.asyncio
    async def test_index_skips_non_accepting(self, payment_discovery, mock_redis):
        """Test that non-accepting agents are not indexed"""
        cap = PaymentCapability(accepts_payment=False)
        await payment_discovery.index_payment_capability("agent-1", cap)
        mock_redis.sadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_find_agents_by_payment_method(self, payment_discovery, mock_redis):
        """Test finding agents by payment method"""
        mock_redis.smembers = AsyncMock(return_value={"agent-1", "agent-2"})

        agents = await payment_discovery.find_agents_by_payment_method(SupportedPaymentMethod.USDC)

        assert len(agents) == 2
        assert "agent-1" in agents

    @pytest.mark.asyncio
    async def test_find_agents_by_network(self, payment_discovery, mock_redis):
        """Test finding agents by network"""
        mock_redis.smembers = AsyncMock(return_value={"agent-1"})

        agents = await payment_discovery.find_agents_by_network(SupportedNetwork.BASE)

        assert "agent-1" in agents

    @pytest.mark.asyncio
    async def test_find_agents_accepting_payment_with_criteria(self, payment_discovery, mock_redis):
        """Test finding agents with multiple criteria"""
        mock_redis.sinter = AsyncMock(return_value={"matching-agent"})

        agents = await payment_discovery.find_agents_accepting_payment(
            payment_method=SupportedPaymentMethod.USDC,
            network=SupportedNetwork.BASE,
            currency="USD",
        )

        assert "matching-agent" in agents
        mock_redis.sinter.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_agent_payment_capability(
        self, payment_discovery, mock_redis, sample_capability
    ):
        """Test getting agent's payment capability"""
        mock_redis.get = AsyncMock(return_value=sample_capability.model_dump_json())

        cap = await payment_discovery.get_agent_payment_capability("agent-1")

        assert cap is not None
        assert cap.accepts_payment is True
        assert cap.wallet_address == sample_capability.wallet_address

    @pytest.mark.asyncio
    async def test_remove_payment_capability(
        self, payment_discovery, mock_redis, sample_capability
    ):
        """Test removing payment capability"""
        mock_redis.get = AsyncMock(return_value=sample_capability.model_dump_json())

        await payment_discovery.remove_payment_capability("agent-1")

        # Should remove from all indexes
        assert mock_redis.srem.call_count >= 2
        mock_redis.delete.assert_called()


# =============================================================================
# Test PaymentTaskManager
# =============================================================================


class TestPaymentTaskManager:
    """Test PaymentTaskManager"""

    @pytest.mark.asyncio
    async def test_create_payment_task(self, payment_task_manager, payment_discovery, mock_redis):
        """Test creating a payment task"""
        # Mock seller's payment capability
        seller_capability = PaymentCapability(
            accepts_payment=True,
            payment_methods=[SupportedPaymentMethod.USDC],
            wallet_address="0xseller123",
            supported_networks=[SupportedNetwork.BASE],
        )
        payment_discovery.get_agent_payment_capability = AsyncMock(return_value=seller_capability)

        task = await payment_task_manager.create_payment_task(
            buyer_agent="buyer-1",
            seller_agent="seller-1",
            task_description="Build API",
            amount="500.00",
            currency="USD",
        )

        assert task.task_id is not None
        assert task.buyer_agent == "buyer-1"
        assert task.seller_agent == "seller-1"
        assert task.status == PaymentTaskStatus.CREATED
        assert task.recipient_wallet == "0xseller123"

    @pytest.mark.asyncio
    async def test_create_task_seller_not_accepting(self, payment_task_manager, payment_discovery):
        """Test creating task with seller not accepting payments"""
        payment_discovery.get_agent_payment_capability = AsyncMock(return_value=None)

        with pytest.raises(ValueError, match="does not accept payments"):
            await payment_task_manager.create_payment_task(
                buyer_agent="buyer-1",
                seller_agent="unknown-seller",
                task_description="Task",
                amount="100.00",
            )

    @pytest.mark.asyncio
    async def test_get_task(self, mock_redis, payment_discovery, mock_webhook_service):
        """Test getting a payment task"""
        manager = PaymentTaskManager(
            redis=mock_redis,
            discovery=payment_discovery,
            webhook_service=mock_webhook_service,
        )
        task = PaymentTask(
            task_id="task-123",
            buyer_agent="buyer",
            seller_agent="seller",
            task_description="Test",
            amount="100.00",
        )
        mock_redis.get = AsyncMock(return_value=task.model_dump_json())

        retrieved = await manager.get_task("task-123")

        assert retrieved is not None
        assert retrieved.task_id == "task-123"

    @pytest.mark.asyncio
    async def test_update_task_status(self, mock_redis, payment_discovery, mock_webhook_service):
        """Test updating task status"""
        manager = PaymentTaskManager(
            redis=mock_redis,
            discovery=payment_discovery,
            webhook_service=mock_webhook_service,
        )
        task = PaymentTask(
            task_id="task-123",
            buyer_agent="buyer",
            seller_agent="seller",
            task_description="Test",
            amount="100.00",
        )
        # Mock get_task to return existing task
        mock_redis.get = AsyncMock(return_value=task.model_dump_json())

        updated = await manager.update_task_status(
            "task-123",
            PaymentTaskStatus.PAYMENT_CONFIRMED,
            tx_hash="0xtx123",
        )

        assert updated.status == PaymentTaskStatus.PAYMENT_CONFIRMED
        assert updated.tx_hash == "0xtx123"
        mock_webhook_service.send_event.assert_called()

    @pytest.mark.asyncio
    async def test_update_task_not_found(self, mock_redis, payment_discovery, mock_webhook_service):
        """Test updating non-existent task"""
        manager = PaymentTaskManager(
            redis=mock_redis,
            discovery=payment_discovery,
            webhook_service=mock_webhook_service,
        )
        mock_redis.get = AsyncMock(return_value=None)

        with pytest.raises(ValueError, match="not found"):
            await manager.update_task_status(
                "unknown-task",
                PaymentTaskStatus.PAYMENT_CONFIRMED,
            )

    @pytest.mark.asyncio
    async def test_get_tasks_by_agent(self, mock_redis, payment_discovery, mock_webhook_service):
        """Test getting tasks for an agent"""
        manager = PaymentTaskManager(
            redis=mock_redis,
            discovery=payment_discovery,
            webhook_service=mock_webhook_service,
        )
        mock_redis.smembers = AsyncMock(return_value={"task-1", "task-2"})

        task1 = PaymentTask(
            task_id="task-1",
            buyer_agent="agent-1",
            seller_agent="other",
            task_description="Test 1",
            amount="100.00",
        )
        task2 = PaymentTask(
            task_id="task-2",
            buyer_agent="agent-1",
            seller_agent="other",
            task_description="Test 2",
            amount="200.00",
        )

        # Mock getting each task
        mock_redis.get = AsyncMock(
            side_effect=[
                task1.model_dump_json(),
                task2.model_dump_json(),
            ]
        )

        tasks = await manager.get_tasks_by_agent("agent-1")

        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_get_payment_stats(self, mock_redis, payment_discovery, mock_webhook_service):
        """Test getting payment statistics"""
        manager = PaymentTaskManager(
            redis=mock_redis,
            discovery=payment_discovery,
            webhook_service=mock_webhook_service,
        )
        # Mock stats stored in Redis
        mock_redis.hget = AsyncMock(
            side_effect=lambda key, field: {
                "total_as_buyer": "5",
                "total_as_seller": "5",
                "amount_as_buyer": "500.00",
                "amount_as_seller": "1000.00",
            }.get(field)
        )

        stats = await manager.get_payment_stats("agent-1")

        # Stats returns a dict with agent payment info
        assert isinstance(stats, dict)


# =============================================================================
# Test WebhookService
# =============================================================================


class TestWebhookService:
    """Test WebhookService"""

    def test_webhook_config_creation(self):
        """Test creating webhook config"""
        config = WebhookConfig(
            url="https://api.example.com/webhooks/acn",
            secret="test-secret-key",
            timeout=30,
            retry_count=3,
        )
        assert config.url == "https://api.example.com/webhooks/acn"
        assert config.secret == "test-secret-key"
        assert config.enabled is True

    def test_webhook_payload_creation(self):
        """Test creating webhook payload"""
        from acn.payments.webhook import WebhookPayload

        payload = WebhookPayload(
            event=WebhookEventType.PAYMENT_CONFIRMED,
            task_id="task-123",
            data={"amount": "100.00"},
            buyer_agent="buyer",
            seller_agent="seller",
        )
        assert payload.task_id == "task-123"
        assert payload.event == WebhookEventType.PAYMENT_CONFIRMED

    def test_webhook_delivery_creation(self):
        """Test creating webhook delivery record"""
        from acn.payments.webhook import WebhookPayload

        payload = WebhookPayload(
            event=WebhookEventType.PAYMENT_CONFIRMED,
            task_id="task-123",
            data={"amount": "100.00"},
        )
        delivery = WebhookDelivery(
            id="del-123",
            payload=payload,
            url="https://api.example.com/webhooks/acn",
            status="pending",
        )
        assert delivery.id == "del-123"
        assert delivery.status == "pending"
        assert delivery.attempts == 0

    def test_webhook_event_types(self):
        """Test webhook event type values"""
        assert WebhookEventType.TASK_CREATED.value == "payment_task.created"
        assert WebhookEventType.PAYMENT_CONFIRMED.value == "payment_task.payment_confirmed"
        assert WebhookEventType.TASK_COMPLETED.value == "payment_task.completed"


# =============================================================================
# Test Payment Status Transitions
# =============================================================================


class TestPaymentStatusTransitions:
    """Test valid payment status transitions"""

    def test_status_enum_values(self):
        """Test all status enum values exist"""
        assert PaymentTaskStatus.CREATED.value == "created"
        assert PaymentTaskStatus.PAYMENT_REQUESTED.value == "payment_requested"
        assert PaymentTaskStatus.PAYMENT_CONFIRMED.value == "payment_confirmed"
        assert PaymentTaskStatus.TASK_COMPLETED.value == "task_completed"
        assert PaymentTaskStatus.PAYMENT_RELEASED.value == "payment_released"
        assert PaymentTaskStatus.DISPUTED.value == "disputed"
        assert PaymentTaskStatus.CANCELLED.value == "cancelled"
        assert PaymentTaskStatus.FAILED.value == "failed"


# =============================================================================
# Test Supported Methods and Networks
# =============================================================================


class TestSupportedEnums:
    """Test supported payment methods and networks"""

    def test_payment_methods(self):
        """Test payment method enum values"""
        # Crypto
        assert SupportedPaymentMethod.USDC.value == "usdc"
        assert SupportedPaymentMethod.USDT.value == "usdt"
        assert SupportedPaymentMethod.ETH.value == "eth"

        # Traditional
        assert SupportedPaymentMethod.CREDIT_CARD.value == "credit_card"
        assert SupportedPaymentMethod.PAYPAL.value == "paypal"

        # Platform
        assert SupportedPaymentMethod.PLATFORM_CREDITS.value == "platform_credits"

    def test_supported_networks(self):
        """Test supported network enum values"""
        assert SupportedNetwork.ETHEREUM.value == "ethereum"
        assert SupportedNetwork.BASE.value == "base"
        assert SupportedNetwork.ARBITRUM.value == "arbitrum"
        assert SupportedNetwork.SOLANA.value == "solana"
