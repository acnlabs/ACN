"""
ACN Billing Service

Handles token-based billing, network fee calculation, and payment settlement.

Key responsibilities:
1. Calculate costs based on token usage
2. Deduct network fees (15% from agent income)
3. Settle payments in platform credits
4. Record billing transactions for audit

This service is ACN's unique value-add on top of AP2.
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

from ..protocols.ap2.core import (
    CREDITS_PER_USD,
    NETWORK_FEE_RATE,
    TokenPricing,
)

if TYPE_CHECKING:
    from .agent_service import AgentService


# =============================================================================
# Billing Configuration
# =============================================================================


class BillingConfig:
    """
    Centralized billing configuration.

    These values can be overridden via environment variables in production.
    """

    # Network fee rate (deducted from agent income)
    NETWORK_FEE_RATE: float = NETWORK_FEE_RATE  # 15%

    # Credits conversion rate
    CREDITS_PER_USD: float = CREDITS_PER_USD  # 1 USD = 10 credits

    # Minimum billable amount (in credits)
    MIN_BILLABLE_CREDITS: float = 0.01

    # Maximum single transaction (in credits) - safety limit
    MAX_TRANSACTION_CREDITS: float = 100000.0


# =============================================================================
# Billing Models
# =============================================================================


class BillingTransactionType(StrEnum):
    """Types of billing transactions"""

    AGENT_CALL = "agent_call"  # User called an agent
    NETWORK_FEE = "network_fee"  # Network fee collected
    AGENT_INCOME = "agent_income"  # Agent income credited
    REFUND = "refund"  # Refund issued


class BillingTransactionStatus(StrEnum):
    """Status of billing transactions"""

    PENDING = "pending"  # Transaction created, not yet processed
    COMPLETED = "completed"  # Successfully processed
    FAILED = "failed"  # Processing failed
    REFUNDED = "refunded"  # Transaction was refunded


class TokenUsage(BaseModel):
    """Token usage for a single request"""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class CostBreakdown(BaseModel):
    """Detailed cost breakdown for a billing transaction"""

    # Token usage
    input_tokens: int = 0
    output_tokens: int = 0

    # USD amounts
    total_usd: float = 0.0
    network_fee_usd: float = 0.0
    agent_income_usd: float = 0.0

    # Credits amounts
    total_credits: float = 0.0
    network_fee_credits: float = 0.0
    agent_income_credits: float = 0.0

    # Pricing info
    input_price_per_million: float = 0.0
    output_price_per_million: float = 0.0
    currency: str = "USD"


class BillingTransaction(BaseModel):
    """A billing transaction record"""

    transaction_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    # Parties
    user_id: str = Field(..., description="User being charged")
    agent_id: str = Field(..., description="Agent providing service")
    agent_owner_id: str | None = Field(None, description="Owner of the agent")

    # Task reference
    task_id: str | None = Field(None, description="Associated task ID")

    # Transaction details
    type: BillingTransactionType = BillingTransactionType.AGENT_CALL
    status: BillingTransactionStatus = BillingTransactionStatus.PENDING

    # Cost breakdown
    cost: CostBreakdown = Field(default_factory=CostBreakdown)

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    # Error info (if failed)
    error_message: str | None = None


# =============================================================================
# Billing Service
# =============================================================================


class BillingService:
    """
    ACN Billing Service

    Handles all billing operations including:
    - Cost calculation based on token usage
    - Network fee deduction
    - Payment settlement in platform credits
    - Transaction recording and audit
    """

    def __init__(
        self,
        redis: Redis,
        agent_service: AgentService | None = None,
        webhook_url: str | None = None,
    ):
        self.redis = redis
        self.agent_service = agent_service
        self.webhook_url = webhook_url
        self._prefix = "acn:billing:"

    # -------------------------------------------------------------------------
    # Cost Calculation
    # -------------------------------------------------------------------------

    def calculate_cost(
        self,
        input_tokens: int,
        output_tokens: int,
        pricing: TokenPricing,
    ) -> CostBreakdown:
        """
        Calculate cost breakdown for given token usage.

        Args:
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            pricing: Token pricing configuration

        Returns:
            CostBreakdown with all fee details
        """
        # Calculate raw cost in USD using Decimal to avoid float rounding drift
        d_input = (
            Decimal(str(input_tokens))
            / Decimal("1000000")
            * Decimal(str(pricing.input_price_per_million))
        )
        d_output = (
            Decimal(str(output_tokens))
            / Decimal("1000000")
            * Decimal(str(pricing.output_price_per_million))
        )
        d_total_usd = d_input + d_output

        # Derive fee and income from total to guarantee fee + income == total (no rounding gap)
        d_network_fee_usd = (d_total_usd * Decimal(str(BillingConfig.NETWORK_FEE_RATE))).quantize(
            Decimal("0.000001")
        )
        d_agent_income_usd = d_total_usd - d_network_fee_usd

        d_credits_per_usd = Decimal(str(BillingConfig.CREDITS_PER_USD))
        d_total_credits = (d_total_usd * d_credits_per_usd).quantize(Decimal("0.0001"))
        d_network_fee_credits = (d_network_fee_usd * d_credits_per_usd).quantize(Decimal("0.0001"))
        d_agent_income_credits = d_total_credits - d_network_fee_credits

        return CostBreakdown(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_usd=float(d_total_usd.quantize(Decimal("0.000001"))),
            network_fee_usd=float(d_network_fee_usd),
            agent_income_usd=float(d_agent_income_usd.quantize(Decimal("0.000001"))),
            total_credits=float(d_total_credits),
            network_fee_credits=float(d_network_fee_credits),
            agent_income_credits=float(d_agent_income_credits),
            input_price_per_million=pricing.input_price_per_million,
            output_price_per_million=pricing.output_price_per_million,
            currency=pricing.currency,
        )

    def estimate_cost(
        self,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
        pricing: TokenPricing,
    ) -> CostBreakdown:
        """
        Estimate cost before making a request.

        Useful for showing users expected costs before calling an agent.
        """
        return self.calculate_cost(
            estimated_input_tokens,
            estimated_output_tokens,
            pricing,
        )

    # -------------------------------------------------------------------------
    # Transaction Management
    # -------------------------------------------------------------------------

    async def create_transaction(
        self,
        user_id: str,
        agent_id: str,
        agent_owner_id: str | None,
        cost: CostBreakdown,
        task_id: str | None = None,
    ) -> BillingTransaction:
        """
        Create a new billing transaction.

        This is called after an agent call completes with token usage data.
        """
        transaction = BillingTransaction(
            user_id=user_id,
            agent_id=agent_id,
            agent_owner_id=agent_owner_id,
            task_id=task_id,
            type=BillingTransactionType.AGENT_CALL,
            status=BillingTransactionStatus.PENDING,
            cost=cost,
        )

        # Validate transaction
        if cost.total_credits > BillingConfig.MAX_TRANSACTION_CREDITS:
            transaction.status = BillingTransactionStatus.FAILED
            transaction.error_message = f"Transaction exceeds maximum: {cost.total_credits} > {BillingConfig.MAX_TRANSACTION_CREDITS}"

        # Save transaction
        await self._save_transaction(transaction)

        # Index for queries
        await self._index_transaction(transaction)

        return transaction

    async def process_transaction(
        self,
        transaction_id: str,
        deduct_credits_callback,
        add_earnings_callback,
    ) -> BillingTransaction:
        """
        Process a pending transaction.

        This performs the actual billing:
        1. Deduct credits from user
        2. Add earnings to agent owner
        3. Record network fee

        Args:
            transaction_id: ID of transaction to process
            deduct_credits_callback: async func(user_id, amount) to deduct credits
            add_earnings_callback: async func(user_id, amount) to add earnings

        Returns:
            Updated transaction
        """
        transaction = await self.get_transaction(transaction_id)
        if not transaction:
            raise ValueError(f"Transaction not found: {transaction_id}")

        if transaction.status != BillingTransactionStatus.PENDING:
            raise ValueError(f"Transaction not pending: {transaction.status}")

        try:
            # 1. Deduct credits from user
            await deduct_credits_callback(
                transaction.user_id,
                transaction.cost.total_credits,
            )

            # 2. Add earnings to agent owner (if known)
            if transaction.agent_owner_id:
                await add_earnings_callback(
                    transaction.agent_owner_id,
                    transaction.cost.agent_income_credits,
                )

            # 3. Record network fee (internal accounting)
            await self._record_network_fee(
                transaction.transaction_id,
                transaction.cost.network_fee_credits,
            )

            # Mark as completed
            transaction.status = BillingTransactionStatus.COMPLETED
            transaction.completed_at = datetime.now(UTC)

        except Exception as e:
            # Mark as failed
            transaction.status = BillingTransactionStatus.FAILED
            transaction.error_message = str(e)

        await self._save_transaction(transaction)

        # Send webhook notification
        await self._send_billing_webhook(transaction)

        return transaction

    async def refund_transaction(
        self,
        transaction_id: str,
        refund_credits_callback,
        deduct_earnings_callback,
        reason: str = "User requested refund",
    ) -> BillingTransaction:
        """
        Refund a completed transaction.

        This reverses a billing transaction:
        1. Refund credits to user
        2. Deduct earnings from agent owner
        3. Adjust network fee accounting
        """
        transaction = await self.get_transaction(transaction_id)
        if not transaction:
            raise ValueError(f"Transaction not found: {transaction_id}")

        if transaction.status != BillingTransactionStatus.COMPLETED:
            raise ValueError(f"Can only refund completed transactions: {transaction.status}")

        try:
            # 1. Refund credits to user
            await refund_credits_callback(
                transaction.user_id,
                transaction.cost.total_credits,
            )

            # 2. Deduct earnings from agent owner (if known)
            if transaction.agent_owner_id:
                await deduct_earnings_callback(
                    transaction.agent_owner_id,
                    transaction.cost.agent_income_credits,
                )

            # 3. Adjust network fee accounting
            await self._reverse_network_fee(
                transaction.transaction_id,
                transaction.cost.network_fee_credits,
            )

            # Mark as refunded
            transaction.status = BillingTransactionStatus.REFUNDED
            transaction.error_message = reason

        except Exception as e:
            transaction.error_message = f"Refund failed: {str(e)}"

        await self._save_transaction(transaction)

        return transaction

    # -------------------------------------------------------------------------
    # Queries
    # -------------------------------------------------------------------------

    async def get_transaction(
        self,
        transaction_id: str,
    ) -> BillingTransaction | None:
        """Get a transaction by ID"""
        key = f"{self._prefix}tx:{transaction_id}"
        data = await self.redis.get(key)
        if data:
            return BillingTransaction.model_validate_json(data)
        return None

    async def get_user_transactions(
        self,
        user_id: str,
        limit: int = 50,
        status: BillingTransactionStatus | None = None,
    ) -> list[BillingTransaction]:
        """Get transactions for a user"""
        key = f"{self._prefix}by_user:{user_id}"
        tx_ids = await self.redis.lrange(key, 0, limit - 1)

        transactions = []
        for tx_id in tx_ids:
            tx = await self.get_transaction(tx_id)
            if tx and (status is None or tx.status == status):
                transactions.append(tx)

        return transactions

    async def get_agent_transactions(
        self,
        agent_id: str,
        limit: int = 50,
    ) -> list[BillingTransaction]:
        """Get transactions for an agent"""
        key = f"{self._prefix}by_agent:{agent_id}"
        tx_ids = await self.redis.lrange(key, 0, limit - 1)

        transactions = []
        for tx_id in tx_ids:
            tx = await self.get_transaction(tx_id)
            if tx:
                transactions.append(tx)

        return transactions

    async def get_user_billing_stats(
        self,
        user_id: str,
    ) -> dict:
        """Get billing statistics for a user"""
        transactions = await self.get_user_transactions(user_id, limit=1000)

        stats = {
            "total_transactions": len(transactions),
            "total_spent_credits": 0.0,
            "total_spent_usd": 0.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "by_status": {},
            "by_agent": {},
        }

        for tx in transactions:
            # Aggregate totals (only for completed)
            if tx.status == BillingTransactionStatus.COMPLETED:
                stats["total_spent_credits"] += tx.cost.total_credits
                stats["total_spent_usd"] += tx.cost.total_usd
                stats["total_input_tokens"] += tx.cost.input_tokens
                stats["total_output_tokens"] += tx.cost.output_tokens

            # Count by status
            status = tx.status.value
            stats["by_status"][status] = stats["by_status"].get(status, 0) + 1

            # Aggregate by agent
            if tx.agent_id not in stats["by_agent"]:
                stats["by_agent"][tx.agent_id] = {
                    "count": 0,
                    "total_credits": 0.0,
                }
            stats["by_agent"][tx.agent_id]["count"] += 1
            if tx.status == BillingTransactionStatus.COMPLETED:
                stats["by_agent"][tx.agent_id]["total_credits"] += tx.cost.total_credits

        return stats

    async def get_network_fee_stats(self) -> dict:
        """Get network fee statistics (for platform admin)"""
        key = f"{self._prefix}network_fees:total"
        total = await self.redis.get(key)

        return {
            "total_network_fees_credits": float(total) if total else 0.0,
            "fee_rate": BillingConfig.NETWORK_FEE_RATE,
        }

    # -------------------------------------------------------------------------
    # Internal Helpers
    # -------------------------------------------------------------------------

    async def _save_transaction(self, transaction: BillingTransaction):
        """Save transaction to Redis"""
        key = f"{self._prefix}tx:{transaction.transaction_id}"
        await self.redis.set(key, transaction.model_dump_json())

    async def _index_transaction(self, transaction: BillingTransaction):
        """Index transaction for queries"""
        # By user
        user_key = f"{self._prefix}by_user:{transaction.user_id}"
        await self.redis.lpush(user_key, transaction.transaction_id)

        # By agent
        agent_key = f"{self._prefix}by_agent:{transaction.agent_id}"
        await self.redis.lpush(agent_key, transaction.transaction_id)

        # By task (if available)
        if transaction.task_id:
            task_key = f"{self._prefix}by_task:{transaction.task_id}"
            await self.redis.set(task_key, transaction.transaction_id)

    async def _record_network_fee(self, transaction_id: str, amount: float):
        """Record network fee for accounting"""
        # Increment total network fees
        total_key = f"{self._prefix}network_fees:total"
        await self.redis.incrbyfloat(total_key, amount)

        # Record individual fee
        fee_key = f"{self._prefix}network_fees:tx:{transaction_id}"
        await self.redis.set(fee_key, str(amount))

    async def _reverse_network_fee(self, transaction_id: str, amount: float):
        """Reverse network fee (for refunds)"""
        # Decrement total network fees
        total_key = f"{self._prefix}network_fees:total"
        await self.redis.incrbyfloat(total_key, -amount)

        # Mark fee as reversed
        fee_key = f"{self._prefix}network_fees:tx:{transaction_id}"
        await self.redis.set(fee_key, f"REVERSED:{amount}")

    async def _send_billing_webhook(self, transaction: BillingTransaction):
        """Send webhook notification for billing event"""
        if not self.webhook_url:
            return

        # In production, use httpx/aiohttp to send webhook
        # For now, just log to Redis
        webhook_key = f"{self._prefix}webhooks:pending"
        await self.redis.lpush(
            webhook_key,
            json.dumps(
                {
                    "event": "billing.transaction",
                    "transaction_id": transaction.transaction_id,
                    "status": transaction.status.value,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            ),
        )


# =============================================================================
# Helper Functions
# =============================================================================


def calculate_estimated_credits(
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    input_price_per_million: float,
    output_price_per_million: float,
) -> float:
    """
    Quick helper to estimate credits needed for a request.

    Example:
        credits = calculate_estimated_credits(
            estimated_input_tokens=1000,
            estimated_output_tokens=500,
            input_price_per_million=3.00,
            output_price_per_million=15.00,
        )
        # credits == 0.0345
    """
    input_usd = (estimated_input_tokens / 1_000_000) * input_price_per_million
    output_usd = (estimated_output_tokens / 1_000_000) * output_price_per_million
    total_usd = input_usd + output_usd
    return round(total_usd * CREDITS_PER_USD, 4)
