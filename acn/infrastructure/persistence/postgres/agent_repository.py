"""PostgreSQL Implementation of IAgentRepository

Persistent agent storage. Heartbeat TTL remains in Redis.
"""

from datetime import UTC, datetime

import redis.asyncio as aioredis
import structlog
from sqlalchemy import String, cast, delete, func, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ....core.entities.agent import Agent, AgentStatus, ClaimStatus
from ....core.interfaces import IAgentRepository
from .models import AgentModel

logger = structlog.get_logger()

_ALIVE_KEY = "acn:agents:{agent_id}:alive"
_ALIVE_TTL = 90  # seconds


def _tz(dt: datetime | None) -> datetime | None:
    """Ensure datetime is timezone-aware (UTC). asyncpg rejects naive datetimes."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class PostgresAgentRepository(IAgentRepository):
    """
    PostgreSQL-backed AgentRepository.

    Persistent data  → PostgreSQL
    Heartbeat TTL    → Redis  (acn:agents:{id}:alive)
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: aioredis.Redis,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis_client

    # =========================================================================
    # Mapping helpers
    # =========================================================================

    def _model_to_agent(self, row: AgentModel) -> Agent:
        meta = row.agent_metadata or {}
        return Agent(
            agent_id=row.agent_id,
            name=row.name,
            owner=row.owner,
            endpoint=row.endpoint,
            status=AgentStatus(row.status),
            # Prefer dedicated SQL column; fall back to JSONB for rows saved before this fix
            description=row.description or meta.get("description"),
            tags=list(row.tags or []),
            subnet_ids=list(row.subnet_ids or ["public"]),
            metadata=meta.get("extra_metadata", {}),
            registered_at=row.registered_at,
            last_heartbeat=row.last_heartbeat,
            api_key=row.api_key,
            auth0_client_id=row.auth0_client_id,
            auth0_token_endpoint=row.auth0_token_endpoint,
            claim_status=ClaimStatus(row.claim_status) if row.claim_status else None,
            verification_code=row.verification_code,
            referrer_id=row.referrer_id,
            owner_changed_at=row.owner_changed_at,
            agent_card=row.agent_card,
            wallet_address=row.wallet_address,
            wallet_addresses=dict(row.wallet_addresses or {}),
            accepts_payment=row.accepts_payment,
            payment_methods=list(row.payment_methods or []),
            token_pricing=row.token_pricing or meta.get("token_pricing"),
            erc8004_agent_id=meta.get("erc8004_agent_id"),
            erc8004_chain=meta.get("erc8004_chain"),
            erc8004_tx_hash=meta.get("erc8004_tx_hash"),
            erc8004_registered_at=meta.get("erc8004_registered_at")
            and datetime.fromisoformat(meta["erc8004_registered_at"]),
        )

    def _agent_to_model(self, agent: Agent) -> AgentModel:
        extra_meta: dict = {
            "description": agent.description,
            "extra_metadata": agent.metadata,
            "erc8004_agent_id": agent.erc8004_agent_id,
            "erc8004_chain": agent.erc8004_chain,
            "erc8004_tx_hash": agent.erc8004_tx_hash,
            "erc8004_registered_at": agent.erc8004_registered_at.isoformat()
            if agent.erc8004_registered_at
            else None,
        }
        return AgentModel(
            agent_id=agent.agent_id,
            name=agent.name,
            owner=agent.owner,
            endpoint=agent.endpoint,
            status=agent.status.value,
            description=agent.description,
            tags=agent.tags or None,
            subnet_ids=agent.subnet_ids or None,
            api_key=agent.api_key,
            auth0_client_id=agent.auth0_client_id,
            auth0_token_endpoint=agent.auth0_token_endpoint,
            claim_status=agent.claim_status.value if agent.claim_status else None,
            verification_code=agent.verification_code,
            referrer_id=agent.referrer_id,
            wallet_address=agent.wallet_address,
            wallet_addresses=agent.wallet_addresses or None,
            accepts_payment=agent.accepts_payment,
            payment_methods=list(agent.payment_methods) if agent.payment_methods else None,
            token_pricing=agent.token_pricing or None,
            agent_card=agent.agent_card,
            agent_metadata=extra_meta,
            registered_at=_tz(agent.registered_at) or datetime.now(UTC),
            last_heartbeat=_tz(agent.last_heartbeat),
            owner_changed_at=_tz(agent.owner_changed_at),
        )

    # =========================================================================
    # CRUD
    # =========================================================================

    async def save(self, agent: Agent) -> None:
        model = self._agent_to_model(agent)
        async with self._session_factory() as session:
            existing = await session.get(AgentModel, agent.agent_id)
            if existing:
                await session.execute(
                    update(AgentModel)
                    .where(AgentModel.agent_id == agent.agent_id)
                    .values(
                        name=model.name,
                        owner=model.owner,
                        endpoint=model.endpoint,
                        status=model.status,
                        description=model.description,
                        tags=model.tags,
                        subnet_ids=model.subnet_ids,
                        api_key=model.api_key,
                        auth0_client_id=model.auth0_client_id,
                        auth0_token_endpoint=model.auth0_token_endpoint,
                        claim_status=model.claim_status,
                        verification_code=model.verification_code,
                        referrer_id=model.referrer_id,
                        wallet_address=model.wallet_address,
                        wallet_addresses=model.wallet_addresses,
                        accepts_payment=model.accepts_payment,
                        payment_methods=model.payment_methods,
                        token_pricing=model.token_pricing,
                        agent_card=model.agent_card,
                        agent_metadata=model.agent_metadata,
                        last_heartbeat=model.last_heartbeat,
                        owner_changed_at=model.owner_changed_at,
                    )
                )
            else:
                session.add(model)
            await session.commit()

    async def find_by_id(self, agent_id: str) -> Agent | None:
        async with self._session_factory() as session:
            row = await session.get(AgentModel, agent_id)
            return self._model_to_agent(row) if row else None

    async def find_by_owner_and_endpoint(self, owner: str, endpoint: str) -> Agent | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentModel).where(
                    AgentModel.owner == owner,
                    AgentModel.endpoint == endpoint,
                )
            )
            row = result.scalar_one_or_none()
            return self._model_to_agent(row) if row else None

    async def find_all(self) -> list[Agent]:
        async with self._session_factory() as session:
            result = await session.execute(select(AgentModel))
            return [self._model_to_agent(r) for r in result.scalars().all()]

    async def find_by_subnet(self, subnet_id: str) -> list[Agent]:
        """Agents whose subnet_ids array contains the given subnet_id."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentModel).where(
                    AgentModel.subnet_ids.contains(cast([subnet_id], ARRAY(String)))
                )
            )
            return [self._model_to_agent(r) for r in result.scalars().all()]

    async def find_by_tags(self, tags: list[str], status: str = "online") -> list[Agent]:
        async with self._session_factory() as session:
            stmt = select(AgentModel).where(AgentModel.status == status)
            if tags:
                stmt = stmt.where(
                    AgentModel.tags.contains(cast(tags, ARRAY(String)))
                )
            result = await session.execute(stmt)
            return [self._model_to_agent(r) for r in result.scalars().all()]

    async def find_by_owner(self, owner: str) -> list[Agent]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentModel).where(AgentModel.owner == owner)
            )
            return [self._model_to_agent(r) for r in result.scalars().all()]

    async def delete(self, agent_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(AgentModel).where(AgentModel.agent_id == agent_id)
            )
            await session.commit()
            return result.rowcount > 0

    async def exists(self, agent_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentModel.agent_id).where(AgentModel.agent_id == agent_id)
            )
            return result.scalar() is not None

    async def count_by_subnet(self, subnet_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count()).where(
                    AgentModel.subnet_ids.contains(cast([subnet_id], ARRAY(String)))
                )
            )
            return result.scalar() or 0

    async def find_by_api_key(self, api_key: str) -> Agent | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentModel).where(AgentModel.api_key == api_key)
            )
            row = result.scalar_one_or_none()
            return self._model_to_agent(row) if row else None

    async def find_unclaimed(self, limit: int = 100) -> list[Agent]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentModel)
                .where(AgentModel.claim_status == ClaimStatus.UNCLAIMED.value)
                .limit(limit)
            )
            return [self._model_to_agent(r) for r in result.scalars().all()]

    # =========================================================================
    # Heartbeat (Redis TTL — unchanged from Redis implementation)
    # =========================================================================

    async def set_alive(self, agent_id: str, ttl: int = _ALIVE_TTL) -> None:
        key = _ALIVE_KEY.format(agent_id=agent_id)
        await self._redis.set(key, "1", ex=ttl)

    async def filter_alive(self, agent_ids: list[str]) -> set[str]:
        if not agent_ids:
            return set()
        async with self._redis.pipeline(transaction=False) as pipe:
            for aid in agent_ids:
                pipe.exists(_ALIVE_KEY.format(agent_id=aid))
            results = await pipe.execute()
        return {aid for aid, exists in zip(agent_ids, results, strict=False) if exists}

    async def mark_offline_stale(self, batch_size: int = 500) -> int:
        """Mark agents whose alive key has expired as OFFLINE in PostgreSQL.

        Streams through agents that are currently marked ONLINE in batches
        of `batch_size`, checks each batch against Redis heartbeat keys,
        and updates only the stale ones. Previous implementation used
        `find_all()` which loaded the entire agents table into memory on
        every tick — unusable once the table passes ~100k rows.

        Depends on the partial index
        `ix_agents_status_online_agent_id` (migration c3d4e5f6a7b8): the
        `WHERE status='online' AND agent_id > :c ORDER BY agent_id LIMIT N`
        query degrades to a full pkey scan without it.
        """
        total_stale = 0
        last_agent_id: str | None = None

        while True:
            async with self._session_factory() as session:
                stmt = (
                    select(AgentModel.agent_id)
                    .where(AgentModel.status == AgentStatus.ONLINE.value)
                    .order_by(AgentModel.agent_id)
                    .limit(batch_size)
                )
                if last_agent_id is not None:
                    stmt = stmt.where(AgentModel.agent_id > last_agent_id)
                result = await session.execute(stmt)
                batch_ids: list[str] = list(result.scalars().all())

            if not batch_ids:
                break

            alive_ids = await self.filter_alive(batch_ids)
            stale_ids = [aid for aid in batch_ids if aid not in alive_ids]

            if stale_ids:
                async with self._session_factory() as session:
                    await session.execute(
                        update(AgentModel)
                        .where(AgentModel.agent_id.in_(stale_ids))
                        .values(status=AgentStatus.OFFLINE.value)
                    )
                    await session.commit()
                total_stale += len(stale_ids)

            # Advance the cursor. Use the last id of the batch we just
            # read, not of stale_ids, or we'd loop forever on a batch
            # where every agent is still alive.
            last_agent_id = batch_ids[-1]

            if len(batch_ids) < batch_size:
                break

        logger.info("pg_mark_offline_stale", count=total_stale)
        return total_stale
