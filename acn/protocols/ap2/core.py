"""
ACN Payment Integration Layer

Provides ACN-specific payment capabilities that add value beyond raw AP2:

1. Payment Discovery - Find agents by payment capability
2. A2A + AP2 Fusion - Combine task messages with payment requests
3. Payment Tracking - Track payment status across agent interactions
4. Transaction Audit - Record all payment-related events

This is NOT a reimplementation of AP2, but ACN's unique value-add.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from redis.asyncio import Redis

if TYPE_CHECKING:
    from .webhook import WebhookService

# Check AP2 availability
try:
    import ap2  # type: ignore[import-untyped]  # noqa: F401

    AP2_AVAILABLE = True
except ImportError:
    AP2_AVAILABLE = False


# =============================================================================
# Payment Method Definitions
# =============================================================================


# =============================================================================
# ACN Extension URIs
# =============================================================================

# Standard AP2 extension URI
AP2_EXTENSION_URI = "https://github.com/google-agentic-commerce/ap2/tree/v0.1"

# ACN Token Pricing extension URI (our custom extension)
ACN_TOKEN_PRICING_EXTENSION_URI = "https://agentplanet.com/acn/token-pricing/v1"


# =============================================================================
# Network Fee Configuration
# =============================================================================

NETWORK_FEE_RATE = 0.15  # 15% network fee, deducted from agent income
CREDITS_PER_USD = 10.0  # 1 USD = 10 platform credits


class SupportedPaymentMethod(StrEnum):
    """Payment methods supported by ACN agents"""

    # Traditional
    CREDIT_CARD = "credit_card"
    DEBIT_CARD = "debit_card"
    BANK_TRANSFER = "bank_transfer"

    # Digital Wallets
    PAYPAL = "paypal"
    APPLE_PAY = "apple_pay"
    GOOGLE_PAY = "google_pay"

    # Crypto - Stablecoins
    USDC = "usdc"
    USDT = "usdt"
    DAI = "dai"

    # Crypto - Native
    ETH = "eth"
    BTC = "btc"

    # Platform Credits (for platforms using ACN)
    PLATFORM_CREDITS = "platform_credits"


class SupportedNetwork(StrEnum):
    """Blockchain networks supported for crypto payments"""

    # EVM
    ETHEREUM = "ethereum"
    BASE = "base"
    ARBITRUM = "arbitrum"
    OPTIMISM = "optimism"
    POLYGON = "polygon"

    # Others
    SOLANA = "solana"
    BITCOIN = "bitcoin"


class PaymentTaskStatus(StrEnum):
    """Status of a payment task"""

    CREATED = "created"  # Task created, payment not initiated
    PAYMENT_REQUESTED = "payment_requested"  # Payment request sent
    PAYMENT_PENDING = "payment_pending"  # Waiting for payment confirmation
    PAYMENT_CONFIRMED = "payment_confirmed"  # Payment confirmed
    TASK_IN_PROGRESS = "task_in_progress"  # Task being executed
    TASK_COMPLETED = "task_completed"  # Task completed
    PAYMENT_RELEASED = "payment_released"  # Final payment released
    DISPUTED = "disputed"  # Under dispute
    CANCELLED = "cancelled"  # Cancelled
    FAILED = "failed"  # Failed
    PAYMENT_FAILED = "payment_failed"  # Payment failed
    IN_PROGRESS = "in_progress"  # Task in progress
    REFUNDED = "refunded"  # Payment refunded


# =============================================================================
# Token Pricing Model (ACN Extension)
# =============================================================================


class TokenPricing(BaseModel):
    """
    Token-based pricing model for AI agents.

    Follows OpenAI-style pricing: charge per million tokens for input/output.
    This is an ACN extension to AP2, not part of the standard protocol.
    """

    input_price_per_million: float = Field(
        ...,
        description="Price in USD per million input tokens",
        ge=0,
    )
    output_price_per_million: float = Field(
        ...,
        description="Price in USD per million output tokens",
        ge=0,
    )
    currency: str = Field(
        default="USD",
        description="Currency for pricing (currently only USD supported)",
    )

    def calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """
        Calculate total cost in USD for given token usage.

        Args:
            input_tokens: Number of input tokens used
            output_tokens: Number of output tokens used

        Returns:
            Total cost in USD
        """
        input_cost = (input_tokens / 1_000_000) * self.input_price_per_million
        output_cost = (output_tokens / 1_000_000) * self.output_price_per_million
        return input_cost + output_cost

    def calculate_cost_with_network_fee(self, input_tokens: int, output_tokens: int) -> dict:
        """
        Calculate cost breakdown including network fee.

        Returns:
            Dict with total_usd, network_fee_usd, agent_income_usd,
            and their credits equivalents.
        """
        d_total = Decimal(str(self.calculate_cost(input_tokens, output_tokens)))
        d_fee = (d_total * Decimal(str(NETWORK_FEE_RATE))).quantize(Decimal("0.000001"))
        d_income = d_total - d_fee
        d_cpu = Decimal(str(CREDITS_PER_USD))
        d_total_cr = (d_total * d_cpu).quantize(Decimal("0.01"))
        d_fee_cr = (d_fee * d_cpu).quantize(Decimal("0.01"))
        d_income_cr = d_total_cr - d_fee_cr

        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_usd": float(d_total.quantize(Decimal("0.000001"))),
            "network_fee_usd": float(d_fee),
            "agent_income_usd": float(d_income.quantize(Decimal("0.000001"))),
            "total_credits": float(d_total_cr),
            "network_fee_credits": float(d_fee_cr),
            "agent_income_credits": float(d_income_cr),
        }

    def to_extension_params(self) -> dict:
        """Convert to AP2 extension params format"""
        return {
            "token_pricing": {
                "input_price_per_million": self.input_price_per_million,
                "output_price_per_million": self.output_price_per_million,
                "currency": self.currency,
            },
            "network_fee_rate": NETWORK_FEE_RATE,
        }


# =============================================================================
# Payment Capability Models
# =============================================================================


class PaymentCapability(BaseModel):
    """
    Payment capability configuration for an Agent.

    This is stored in AgentInfo and indexed by ACN for discovery.
    """

    # Basic capability
    accepts_payment: bool = Field(
        default=False,
        description="Whether this agent accepts payments",
    )

    # What methods are accepted
    payment_methods: list[SupportedPaymentMethod] = Field(
        default_factory=list,
        description="Payment methods this agent accepts",
    )

    # Crypto wallet info
    wallet_address: str | None = Field(
        default=None,
        description="Primary wallet address for crypto payments (legacy, backward compat)",
    )
    wallet_addresses: dict[str, str] = Field(
        default_factory=dict,
        description="Per-network wallet addresses, key = SupportedNetwork value e.g. {'ethereum': '0x...', 'solana': 'So1...'}",
    )
    supported_networks: list[SupportedNetwork] = Field(
        default_factory=list,
        description="Blockchain networks supported",
    )

    # Traditional payment info
    payment_processor: str | None = Field(
        default=None,
        description="Payment processor (e.g., 'stripe', 'paypal')",
    )

    # Pricing info
    default_currency: str = Field(
        default="USD",
        description="Default currency for pricing",
    )

    # Service pricing (skill -> price) - for fixed-price services
    pricing: dict[str, str] = Field(
        default_factory=dict,
        description="Pricing for specific skills (e.g., {'coding': '10.00'})",
    )

    # Token-based pricing - for usage-based services (ACN extension)
    token_pricing: TokenPricing | None = Field(
        default=None,
        description="Token-based pricing (per million tokens, OpenAI-style)",
    )

    def to_agent_card_extension(self) -> dict:
        """Convert to Agent Card extension format for A2A discovery"""
        extensions = []

        # Standard AP2 extension
        ap2_params = {
            "accepts_payment": self.accepts_payment,
            "payment_methods": [m.value for m in self.payment_methods],
            "wallet_address": self.wallet_address,
            "wallet_addresses": self.wallet_addresses,
            "supported_networks": [n.value for n in self.supported_networks],
            "default_currency": self.default_currency,
            "pricing": self.pricing,
        }
        extensions.append(
            {
                "uri": AP2_EXTENSION_URI,
                "description": "Supports AP2 payment protocol",
                "params": ap2_params,
            }
        )

        # ACN Token Pricing extension (if configured)
        if self.token_pricing:
            extensions.append(
                {
                    "uri": ACN_TOKEN_PRICING_EXTENSION_URI,
                    "description": "Supports per-token pricing (OpenAI-style)",
                    "params": self.token_pricing.to_extension_params(),
                }
            )

        return {"extensions": extensions}

    def get_pricing_type(self) -> str:
        """Get the primary pricing type for this agent"""
        if self.token_pricing:
            return "token_based"
        elif self.pricing:
            return "fixed_price"
        return "none"

    def estimate_cost(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        skill: str | None = None,
    ) -> dict | None:
        """
        Estimate cost for a service call.

        For token-based pricing: uses token counts.
        For fixed pricing: uses skill name.

        Returns cost breakdown or None if pricing not available.
        """
        if self.token_pricing and (input_tokens > 0 or output_tokens > 0):
            return self.token_pricing.calculate_cost_with_network_fee(input_tokens, output_tokens)
        elif skill and skill in self.pricing:
            price_usd = float(self.pricing[skill])
            network_fee = price_usd * NETWORK_FEE_RATE
            agent_income = price_usd * (1 - NETWORK_FEE_RATE)
            return {
                "skill": skill,
                "total_usd": price_usd,
                "network_fee_usd": round(network_fee, 6),
                "agent_income_usd": round(agent_income, 6),
                "total_credits": round(price_usd * CREDITS_PER_USD, 2),
                "network_fee_credits": round(network_fee * CREDITS_PER_USD, 2),
                "agent_income_credits": round(agent_income * CREDITS_PER_USD, 2),
            }
        return None


# =============================================================================
# Payment Task Model (A2A + AP2 Fusion)
# =============================================================================


class PaymentTask(BaseModel):
    """
    A task with associated payment - fuses A2A task with AP2 payment.

    This is ACN's unique value: combining task execution with payment flow.
    """

    # Identifiers
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    payment_id: str | None = Field(default=None, description="Associated payment ID")

    # Parties
    buyer_agent: str = Field(..., description="Agent requesting the service")
    seller_agent: str = Field(..., description="Agent providing the service")

    # Task details (A2A part)
    task_description: str = Field(..., description="What needs to be done")
    task_type: str | None = Field(None, description="Task type/category")
    task_metadata: dict = Field(default_factory=dict)

    # Payment details (AP2 part)
    amount: str = Field(..., description="Payment amount as string")
    currency: str = Field(default="USD")
    payment_method: SupportedPaymentMethod | None = None
    network: SupportedNetwork | None = None

    # Recipient info (resolved from ACN Registry)
    recipient_wallet: str | None = Field(None, description="Resolved wallet address")

    # Status tracking
    status: PaymentTaskStatus = Field(default=PaymentTaskStatus.CREATED)

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payment_requested_at: datetime | None = None
    payment_confirmed_at: datetime | None = None
    task_completed_at: datetime | None = None
    payment_released_at: datetime | None = None

    # Transaction details
    tx_hash: str | None = Field(None, description="Blockchain transaction hash")

    # Dispute info
    dispute: dict | None = None


# =============================================================================
# Payment Discovery Service
# =============================================================================


class PaymentDiscoveryService:
    """
    ACN's unique value: Discover agents by payment capability.

    This is something AP2 alone cannot do - it requires ACN's registry.
    """

    def __init__(self, redis: Redis):
        self.redis = redis
        self._prefix = "acn:payments:"

    async def index_payment_capability(
        self,
        agent_id: str,
        capability: PaymentCapability,
    ):
        """
        Index an agent's payment capability for discovery.

        Called when an agent registers or updates payment info.
        """
        if not capability.accepts_payment:
            return

        # Index by payment method
        for method in capability.payment_methods:
            key = f"{self._prefix}by_method:{method.value}"
            await self.redis.sadd(key, agent_id)

        # Index by network
        for network in capability.supported_networks:
            key = f"{self._prefix}by_network:{network.value}"
            await self.redis.sadd(key, agent_id)

        # Index by currency
        key = f"{self._prefix}by_currency:{capability.default_currency}"
        await self.redis.sadd(key, agent_id)

        # Store capability data
        cap_key = f"{self._prefix}capability:{agent_id}"
        await self.redis.set(cap_key, capability.model_dump_json())

    async def remove_payment_capability(self, agent_id: str):
        """Remove agent from payment indexes"""
        cap_key = f"{self._prefix}capability:{agent_id}"
        cap_data = await self.redis.get(cap_key)

        if cap_data:
            capability = PaymentCapability.model_validate_json(cap_data)

            for method in capability.payment_methods:
                key = f"{self._prefix}by_method:{method.value}"
                await self.redis.srem(key, agent_id)

            for network in capability.supported_networks:
                key = f"{self._prefix}by_network:{network.value}"
                await self.redis.srem(key, agent_id)

            key = f"{self._prefix}by_currency:{capability.default_currency}"
            await self.redis.srem(key, agent_id)

            await self.redis.delete(cap_key)

    async def find_agents_by_payment_method(
        self,
        method: SupportedPaymentMethod,
    ) -> list[str]:
        """Find agents that accept a specific payment method"""
        key = f"{self._prefix}by_method:{method.value}"
        return list(await self.redis.smembers(key))

    async def find_agents_by_network(
        self,
        network: SupportedNetwork,
    ) -> list[str]:
        """Find agents that support a specific blockchain network"""
        key = f"{self._prefix}by_network:{network.value}"
        return list(await self.redis.smembers(key))

    async def find_agents_accepting_payment(
        self,
        payment_method: SupportedPaymentMethod | None = None,
        network: SupportedNetwork | None = None,
        currency: str | None = None,
    ) -> list[str]:
        """
        Find agents matching payment criteria.

        This is ACN's unique value - payment-aware agent discovery.
        """
        sets_to_intersect = []

        if payment_method:
            key = f"{self._prefix}by_method:{payment_method.value}"
            sets_to_intersect.append(key)

        if network:
            key = f"{self._prefix}by_network:{network.value}"
            sets_to_intersect.append(key)

        if currency:
            key = f"{self._prefix}by_currency:{currency}"
            sets_to_intersect.append(key)

        if not sets_to_intersect:
            # Return all agents with payment capability
            all_agents = set()
            for method in SupportedPaymentMethod:
                key = f"{self._prefix}by_method:{method.value}"
                agents = await self.redis.smembers(key)
                all_agents.update(agents)
            return list(all_agents)

        # Intersect all criteria
        if len(sets_to_intersect) == 1:
            return list(await self.redis.smembers(sets_to_intersect[0]))

        return list(await self.redis.sinter(*sets_to_intersect))

    async def get_agent_payment_capability(
        self,
        agent_id: str,
    ) -> PaymentCapability | None:
        """Get an agent's payment capability"""
        cap_key = f"{self._prefix}capability:{agent_id}"
        cap_data = await self.redis.get(cap_key)

        if cap_data:
            return PaymentCapability.model_validate_json(cap_data)
        return None


# =============================================================================
# Payment Task Manager (A2A + AP2 Fusion)
# =============================================================================


class PaymentTaskManager:
    """
    Manages payment tasks - combining A2A task flow with AP2 payments.

    This is ACN's unique value: unified task + payment lifecycle.

    Features:
    - Automatic seller wallet resolution from ACN Registry
    - Payment status tracking
    - Webhook notifications to backend (e.g., PlatformBillingEngine)
    - Audit logging
    """

    def __init__(
        self,
        redis: Redis,
        discovery: PaymentDiscoveryService,
        webhook_service: WebhookService | None = None,
    ):
        self.redis = redis
        self.discovery = discovery
        self.webhook = webhook_service
        self._prefix = "acn:payment_tasks:"

    async def create_payment_task(
        self,
        buyer_agent: str,
        seller_agent: str,
        task_description: str,
        amount: str,
        currency: str = "USD",
        payment_method: SupportedPaymentMethod | None = None,
        task_type: str | None = None,
        metadata: dict | None = None,
    ) -> PaymentTask:
        """
        Create a new payment task.

        Automatically resolves seller's wallet address from ACN Registry.
        """
        # Get seller's payment capability
        capability = await self.discovery.get_agent_payment_capability(seller_agent)

        if not capability or not capability.accepts_payment:
            raise ValueError(f"Agent {seller_agent} does not accept payments")

        # Determine payment method
        if payment_method:
            if payment_method not in capability.payment_methods:
                raise ValueError(f"Agent {seller_agent} does not accept {payment_method.value}")
        else:
            # Use first available method
            if capability.payment_methods:
                payment_method = capability.payment_methods[0]

        # Determine network and resolve the correct wallet address for that network
        network = capability.supported_networks[0] if capability.supported_networks else None
        if network and capability.wallet_addresses:
            recipient_wallet = capability.wallet_addresses.get(
                network.value, capability.wallet_address
            )
        else:
            recipient_wallet = capability.wallet_address

        # Create task
        task = PaymentTask(
            buyer_agent=buyer_agent,
            seller_agent=seller_agent,
            task_description=task_description,
            task_type=task_type,
            task_metadata=metadata or {},
            amount=amount,
            currency=currency,
            payment_method=payment_method,
            recipient_wallet=recipient_wallet,
            network=network,
        )

        # Save task
        await self._save_task(task)

        # Index by agents
        await self._index_task(task)

        # Log to audit
        await self._audit_log(
            task_id=task.task_id,
            event="task_created",
            data={
                "buyer": buyer_agent,
                "seller": seller_agent,
                "amount": amount,
                "currency": currency,
            },
        )

        # Send webhook notification
        await self._send_webhook(
            event_type="payment_task.created",
            task=task,
        )

        return task

    async def update_task_status(
        self,
        task_id: str,
        status: PaymentTaskStatus,
        tx_hash: str | None = None,
    ) -> PaymentTask:
        """Update task status"""
        task = await self.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        old_status = task.status
        task.status = status

        # Update timestamps
        now = datetime.now(UTC)
        if status == PaymentTaskStatus.PAYMENT_REQUESTED:
            task.payment_requested_at = now
        elif status == PaymentTaskStatus.PAYMENT_CONFIRMED:
            task.payment_confirmed_at = now
        elif status == PaymentTaskStatus.TASK_COMPLETED:
            task.task_completed_at = now
        elif status == PaymentTaskStatus.PAYMENT_RELEASED:
            task.payment_released_at = now

        if tx_hash:
            task.tx_hash = tx_hash

        await self._save_task(task)

        # Audit log
        await self._audit_log(
            task_id=task_id,
            event="status_changed",
            data={
                "old_status": old_status.value,
                "new_status": status.value,
                "tx_hash": tx_hash,
            },
        )

        # Send webhook based on status
        webhook_event = self._status_to_webhook_event(status)
        if webhook_event:
            await self._send_webhook(webhook_event, task)

        return task

    def _status_to_webhook_event(self, status: PaymentTaskStatus) -> str | None:
        """Map task status to webhook event type"""
        mapping = {
            PaymentTaskStatus.PAYMENT_REQUESTED: "payment_task.payment_pending",
            PaymentTaskStatus.PAYMENT_CONFIRMED: "payment_task.payment_confirmed",
            PaymentTaskStatus.PAYMENT_FAILED: "payment_task.payment_failed",
            PaymentTaskStatus.IN_PROGRESS: "payment_task.in_progress",
            PaymentTaskStatus.TASK_COMPLETED: "payment_task.completed",
            PaymentTaskStatus.DISPUTED: "payment_task.disputed",
            PaymentTaskStatus.REFUNDED: "payment_task.refunded",
            PaymentTaskStatus.CANCELLED: "payment_task.cancelled",
        }
        return mapping.get(status)

    async def get_task(self, task_id: str) -> PaymentTask | None:
        """Get task by ID"""
        key = f"{self._prefix}{task_id}"
        data = await self.redis.get(key)
        if data:
            return PaymentTask.model_validate_json(data)
        return None

    async def get_tasks_by_agent(
        self,
        agent_id: str,
        role: str = "all",  # "buyer", "seller", "all"
        status: PaymentTaskStatus | None = None,
        limit: int = 50,
    ) -> list[PaymentTask]:
        """Get tasks for an agent"""
        task_ids = set()

        if role in ["buyer", "all"]:
            key = f"{self._prefix}by_buyer:{agent_id}"
            buyer_tasks = await self.redis.smembers(key)
            task_ids.update(buyer_tasks)

        if role in ["seller", "all"]:
            key = f"{self._prefix}by_seller:{agent_id}"
            seller_tasks = await self.redis.smembers(key)
            task_ids.update(seller_tasks)

        tasks = []
        for task_id in list(task_ids)[:limit]:
            task = await self.get_task(task_id)
            if task:
                if status is None or task.status == status:
                    tasks.append(task)

        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks[:limit]

    async def get_payment_stats(self, agent_id: str) -> dict:
        """Get payment statistics for an agent"""
        tasks = await self.get_tasks_by_agent(agent_id, limit=1000)

        stats = {
            "total_tasks": len(tasks),
            "as_buyer": {"count": 0, "total_amount": Decimal("0")},
            "as_seller": {"count": 0, "total_amount": Decimal("0")},
            "by_status": {},
            "completed_transactions": 0,
        }

        for task in tasks:
            # By status
            status = task.status.value
            stats["by_status"][status] = stats["by_status"].get(status, 0) + 1

            # By role
            amount = Decimal(task.amount)
            if task.buyer_agent == agent_id:
                stats["as_buyer"]["count"] += 1
                stats["as_buyer"]["total_amount"] += amount
            if task.seller_agent == agent_id:
                stats["as_seller"]["count"] += 1
                stats["as_seller"]["total_amount"] += amount

            # Completed
            if task.status == PaymentTaskStatus.PAYMENT_RELEASED:
                stats["completed_transactions"] += 1

        # Convert Decimal to string
        stats["as_buyer"]["total_amount"] = str(stats["as_buyer"]["total_amount"])
        stats["as_seller"]["total_amount"] = str(stats["as_seller"]["total_amount"])

        return stats

    # -------------------------------------------------------------------------
    # AP2 Message Builders (A2A + AP2 Fusion)
    # -------------------------------------------------------------------------

    def build_payment_request(self, task: PaymentTask) -> dict:
        """
        Build an AP2 PaymentRequest from a PaymentTask.

        This can be embedded in an A2A message.
        """
        if not AP2_AVAILABLE:
            raise RuntimeError("AP2 SDK not available")

        # Build AP2-compatible payment request
        return {
            "ap2_version": "0.1",
            "task_id": task.task_id,
            "payment_request": {
                "id": task.payment_id or task.task_id,
                "methodData": [
                    {
                        "supportedMethods": (
                            task.payment_method.value if task.payment_method else "usdc"
                        ),
                        "data": {
                            "network": task.network.value if task.network else "base",
                            "recipient": task.recipient_wallet,
                        },
                    }
                ],
                "details": {
                    "total": {
                        "label": task.task_description[:50],
                        "amount": {
                            "currency": task.currency,
                            "value": task.amount,
                        },
                    },
                    "displayItems": [
                        {
                            "label": task.task_description,
                            "amount": {
                                "currency": task.currency,
                                "value": task.amount,
                            },
                        }
                    ],
                },
            },
        }

    def build_a2a_payment_message(self, task: PaymentTask) -> dict:
        """
        Build a combined A2A + AP2 message.

        This is ACN's unique value: one message for task + payment.
        """
        return {
            "type": "payment_task",
            "task_id": task.task_id,
            "a2a": {
                "task": {
                    "description": task.task_description,
                    "type": task.task_type,
                    "metadata": task.task_metadata,
                },
                "from": task.buyer_agent,
                "to": task.seller_agent,
            },
            "ap2": self.build_payment_request(task),
            "status": task.status.value,
            "created_at": task.created_at.isoformat(),
        }

    # -------------------------------------------------------------------------
    # Internal Helpers
    # -------------------------------------------------------------------------

    async def _save_task(self, task: PaymentTask):
        """Save task to Redis"""
        key = f"{self._prefix}{task.task_id}"
        await self.redis.set(key, task.model_dump_json())

    async def _index_task(self, task: PaymentTask):
        """Index task for queries"""
        # By buyer
        key = f"{self._prefix}by_buyer:{task.buyer_agent}"
        await self.redis.sadd(key, task.task_id)

        # By seller
        key = f"{self._prefix}by_seller:{task.seller_agent}"
        await self.redis.sadd(key, task.task_id)

    async def _audit_log(self, task_id: str, event: str, data: dict):
        """Log payment event for audit"""
        log_key = f"{self._prefix}audit:{task_id}"
        entry = {
            "event": event,
            "data": data,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        await self.redis.lpush(log_key, json.dumps(entry))

    async def _send_webhook(self, event_type: str, task: PaymentTask):
        """Send webhook notification to backend"""
        if not self.webhook:
            return

        # Import here to avoid circular import
        from .webhook import WebhookEventType

        try:
            webhook_event = WebhookEventType(event_type)
        except ValueError:
            # Unknown event type, skip
            return

        await self.webhook.send_event(
            event=webhook_event,
            task_id=task.task_id,
            data=task.model_dump(),
            buyer_agent=task.buyer_agent,
            seller_agent=task.seller_agent,
            amount=task.amount,
            currency=task.currency,
            payment_method=task.payment_method.value if task.payment_method else None,
        )


# =============================================================================
# Helper Functions
# =============================================================================


def create_payment_capability(
    payment_methods: list[str],
    wallet_address: str | None = None,
    networks: list[str] | None = None,
    pricing: dict[str, str] | None = None,
    token_pricing: dict | None = None,
) -> PaymentCapability:
    """
    Helper to create a PaymentCapability.

    Example (fixed pricing):
        cap = create_payment_capability(
            payment_methods=["usdc", "eth"],
            wallet_address="0x1234...",
            networks=["base", "ethereum"],
            pricing={"coding": "50.00", "analysis": "25.00"},
        )

    Example (token-based pricing, OpenAI-style):
        cap = create_payment_capability(
            payment_methods=["platform_credits"],
            token_pricing={
                "input_price_per_million": 3.00,
                "output_price_per_million": 15.00,
            },
        )
    """
    methods = [SupportedPaymentMethod(m) for m in payment_methods]
    nets = [SupportedNetwork(n) for n in (networks or [])]

    # Create TokenPricing if provided
    tp = None
    if token_pricing:
        tp = TokenPricing(
            input_price_per_million=token_pricing.get("input_price_per_million", 0),
            output_price_per_million=token_pricing.get("output_price_per_million", 0),
            currency=token_pricing.get("currency", "USD"),
        )

    return PaymentCapability(
        accepts_payment=True,
        payment_methods=methods,
        wallet_address=wallet_address,
        supported_networks=nets,
        pricing=pricing or {},
        token_pricing=tp,
    )


def create_token_pricing(
    input_price_per_million: float,
    output_price_per_million: float,
    currency: str = "USD",
) -> TokenPricing:
    """
    Helper to create TokenPricing configuration.

    Example (GPT-4 style pricing):
        pricing = create_token_pricing(
            input_price_per_million=30.00,   # $30 per 1M input tokens
            output_price_per_million=60.00,  # $60 per 1M output tokens
        )

    Example (GPT-3.5 style pricing):
        pricing = create_token_pricing(
            input_price_per_million=0.50,   # $0.50 per 1M input tokens
            output_price_per_million=1.50,  # $1.50 per 1M output tokens
        )
    """
    return TokenPricing(
        input_price_per_million=input_price_per_million,
        output_price_per_million=output_price_per_million,
        currency=currency,
    )
