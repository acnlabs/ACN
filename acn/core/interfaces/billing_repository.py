"""Billing Repository Interface

Defines contract for billing transaction persistence operations.
"""

from abc import ABC, abstractmethod

from ...services.billing_service import BillingTransaction, BillingTransactionStatus


class IBillingRepository(ABC):
    """
    Abstract interface for BillingTransaction persistence.

    Infrastructure layer provides concrete implementation (Redis or PostgreSQL).
    """

    @abstractmethod
    async def save(self, transaction: BillingTransaction) -> None:
        """Save or update a billing transaction"""
        pass

    @abstractmethod
    async def find_by_id(self, transaction_id: str) -> BillingTransaction | None:
        """Find transaction by ID"""
        pass

    @abstractmethod
    async def find_by_user(
        self,
        user_id: str,
        limit: int = 50,
        status: BillingTransactionStatus | None = None,
    ) -> list[BillingTransaction]:
        """Find transactions for a user, optionally filtered by status"""
        pass

    @abstractmethod
    async def find_by_agent(self, agent_id: str, limit: int = 50) -> list[BillingTransaction]:
        """Find transactions for an agent"""
        pass

    @abstractmethod
    async def find_by_task(self, task_id: str) -> BillingTransaction | None:
        """Find the billing transaction for a task"""
        pass

    @abstractmethod
    async def record_network_fee(self, transaction_id: str, amount: float) -> None:
        """Record network fee for a transaction"""
        pass

    @abstractmethod
    async def reverse_network_fee(self, transaction_id: str, amount: float) -> None:
        """Reverse a previously recorded network fee (for refunds)"""
        pass

    @abstractmethod
    async def get_total_network_fees(self) -> float:
        """Get total accumulated network fees"""
        pass
