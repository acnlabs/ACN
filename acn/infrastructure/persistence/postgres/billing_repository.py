"""PostgreSQL Implementation of IBillingRepository"""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ....core.interfaces.billing_repository import IBillingRepository
from ....services.billing_service import (
    BillingTransaction,
    BillingTransactionStatus,
    BillingTransactionType,
    CostBreakdown,
)
from .models import BillingTransactionModel


class PostgresBillingRepository(IBillingRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # =========================================================================
    # Mapping
    # =========================================================================

    def _model_to_tx(self, row: BillingTransactionModel) -> BillingTransaction:
        detail = row.cost_detail or {}
        cost = CostBreakdown(
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            total_usd=row.total_usd,
            total_credits=row.total_credits,
            network_fee_credits=row.network_fee_credits,
            network_fee_usd=detail.get("network_fee_usd", 0.0),
            agent_income_credits=row.agent_income_credits,
            agent_income_usd=detail.get("agent_income_usd", 0.0),
            input_price_per_million=detail.get("input_price_per_million", 0.0),
            output_price_per_million=detail.get("output_price_per_million", 0.0),
            currency=detail.get("currency", "USD"),
        )
        return BillingTransaction(
            transaction_id=row.transaction_id,
            user_id=row.user_id,
            agent_id=row.agent_id,
            agent_owner_id=row.agent_owner_id,
            task_id=row.task_id,
            type=BillingTransactionType(row.type),
            status=BillingTransactionStatus(row.status),
            cost=cost,
            created_at=row.created_at,
            completed_at=row.completed_at,
            error_message=row.error_message,
        )

    @staticmethod
    def _tz(dt: datetime | None) -> datetime | None:
        """Ensure a datetime is timezone-aware (UTC). asyncpg rejects naive datetimes."""
        if dt is None:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

    def _tx_to_model(self, tx: BillingTransaction) -> BillingTransactionModel:
        cost_detail = {
            "network_fee_usd": tx.cost.network_fee_usd,
            "agent_income_usd": tx.cost.agent_income_usd,
            "input_price_per_million": tx.cost.input_price_per_million,
            "output_price_per_million": tx.cost.output_price_per_million,
            "currency": tx.cost.currency,
        }
        return BillingTransactionModel(
            transaction_id=tx.transaction_id,
            user_id=tx.user_id,
            agent_id=tx.agent_id,
            agent_owner_id=tx.agent_owner_id,
            task_id=tx.task_id,
            type=tx.type.value,
            status=tx.status.value,
            total_credits=tx.cost.total_credits,
            total_usd=tx.cost.total_usd,
            network_fee_credits=tx.cost.network_fee_credits,
            agent_income_credits=tx.cost.agent_income_credits,
            input_tokens=tx.cost.input_tokens,
            output_tokens=tx.cost.output_tokens,
            cost_detail=cost_detail,
            created_at=self._tz(tx.created_at) or datetime.now(UTC),
            completed_at=self._tz(tx.completed_at),
            error_message=tx.error_message,
        )

    # =========================================================================
    # IBillingRepository
    # =========================================================================

    async def save(self, transaction: BillingTransaction) -> None:
        model = self._tx_to_model(transaction)
        async with self._session_factory() as session:
            existing = await session.get(BillingTransactionModel, transaction.transaction_id)
            if existing:
                await session.execute(
                    update(BillingTransactionModel)
                    .where(BillingTransactionModel.transaction_id == transaction.transaction_id)
                    .values(
                        status=model.status,
                        total_credits=model.total_credits,
                        total_usd=model.total_usd,
                        network_fee_credits=model.network_fee_credits,
                        agent_income_credits=model.agent_income_credits,
                        cost_detail=model.cost_detail,
                        completed_at=model.completed_at,
                        error_message=model.error_message,
                    )
                )
            else:
                session.add(model)
            await session.commit()

    async def find_by_id(self, transaction_id: str) -> BillingTransaction | None:
        async with self._session_factory() as session:
            row = await session.get(BillingTransactionModel, transaction_id)
            return self._model_to_tx(row) if row else None

    async def find_by_user(
        self,
        user_id: str,
        limit: int = 50,
        status: BillingTransactionStatus | None = None,
    ) -> list[BillingTransaction]:
        async with self._session_factory() as session:
            stmt = (
                select(BillingTransactionModel)
                .where(BillingTransactionModel.user_id == user_id)
                .order_by(BillingTransactionModel.created_at.desc())
                .limit(limit)
            )
            if status:
                stmt = stmt.where(BillingTransactionModel.status == status.value)
            result = await session.execute(stmt)
            return [self._model_to_tx(r) for r in result.scalars().all()]

    async def find_by_agent(self, agent_id: str, limit: int = 50) -> list[BillingTransaction]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(BillingTransactionModel)
                .where(BillingTransactionModel.agent_id == agent_id)
                .order_by(BillingTransactionModel.created_at.desc())
                .limit(limit)
            )
            return [self._model_to_tx(r) for r in result.scalars().all()]

    async def find_by_task(self, task_id: str) -> BillingTransaction | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(BillingTransactionModel)
                .where(BillingTransactionModel.task_id == task_id)
                .order_by(BillingTransactionModel.created_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            return self._model_to_tx(row) if row else None

    async def record_network_fee(self, transaction_id: str, amount: float) -> None:
        """No-op in PG: fee is already embedded in the transaction row."""
        pass

    async def reverse_network_fee(self, transaction_id: str, amount: float) -> None:
        """No-op in PG: refund status is tracked on the transaction row."""
        pass

    async def get_total_network_fees(self) -> float:
        from sqlalchemy import func
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.coalesce(func.sum(BillingTransactionModel.network_fee_credits), 0.0))
                .where(BillingTransactionModel.status == BillingTransactionStatus.COMPLETED.value)
            )
            return float(result.scalar() or 0.0)
