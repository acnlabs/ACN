"""PostgreSQL Implementation of IAgentRepository

Persistent agent storage. Heartbeat TTL remains in Redis.
"""

from datetime import UTC, datetime

import redis.asyncio as aioredis
import structlog
from sqlalchemy import String, cast, delete, func, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ....core.entities.agent import Agent, ClaimStatus
from ....core.interfaces import IAgentRepository
from .models import AgentModel

logger = structlog.get_logger()

_ALIVE_KEY = "acn:agents:{agent_id}:alive"
_INBOUND_KEY = "acn:agents:{agent_id}:inbound"


def _as_str(v: object) -> str | None:
    """Normalize a Redis hash value (bytes or str, depending on the client's
    ``decode_responses`` setting) to ``str``."""
    if v is None:
        return None
    return v.decode() if isinstance(v, bytes | bytearray) else str(v)


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
            a2a_endpoint=meta.get("a2a_endpoint") or row.endpoint,
            # ``status`` column deliberately not read — see entity-layer
            # comment for ``AgentStatus``. The column itself is dropped
            # in this PR's alembic migration.
            description=row.description or meta.get("description"),
            tags=list(row.tags or []),
            subnet_ids=list(row.subnet_ids or ["public"]),
            metadata=meta.get("extra_metadata", {}),
            registered_at=row.registered_at,
            last_heartbeat=row.last_heartbeat,
            api_key=row.api_key,
            claim_status=ClaimStatus(row.claim_status) if row.claim_status else None,
            verification_code=row.verification_code,
            referrer_id=row.referrer_id,
            owner_changed_at=row.owner_changed_at,
            agent_card=row.agent_card,
            agent_card_url=meta.get("agent_card_url"),
            wallet_address=row.wallet_address,
            wallet_addresses=dict(row.wallet_addresses or {}),
            accepts_payment=row.accepts_payment,
            payment_methods=list(row.payment_methods or []),
            token_pricing=row.token_pricing or meta.get("token_pricing"),
            communication_policy=row.communication_policy,
            social_card_url=row.social_card_url,
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
            "a2a_endpoint": agent.a2a_endpoint,
            "agent_card_url": agent.agent_card_url,
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
            # ``status`` deliberately not written — column is being
            # dropped by this PR's migration. The model still defines
            # the column with ``default="online"`` so rows inserted by
            # a still-running OLD process during deploy don't violate
            # the NOT NULL constraint before the migration runs.
            description=agent.description,
            tags=agent.tags or None,
            subnet_ids=agent.subnet_ids or None,
            api_key=agent.api_key,
            claim_status=agent.claim_status.value if agent.claim_status else None,
            verification_code=agent.verification_code,
            referrer_id=agent.referrer_id,
            wallet_address=agent.wallet_address,
            wallet_addresses=agent.wallet_addresses or None,
            accepts_payment=agent.accepts_payment,
            payment_methods=list(agent.payment_methods) if agent.payment_methods else None,
            token_pricing=agent.token_pricing or None,
            agent_card=agent.agent_card,
            social_card_url=agent.social_card_url,
            communication_policy=agent.communication_policy,
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
                        description=model.description,
                        tags=model.tags,
                        subnet_ids=model.subnet_ids,
                        api_key=model.api_key,
                        claim_status=model.claim_status,
                        verification_code=model.verification_code,
                        referrer_id=model.referrer_id,
                        wallet_address=model.wallet_address,
                        wallet_addresses=model.wallet_addresses,
                        accepts_payment=model.accepts_payment,
                        payment_methods=model.payment_methods,
                        token_pricing=model.token_pricing,
                        agent_card=model.agent_card,
                        social_card_url=model.social_card_url,
                        communication_policy=model.communication_policy,
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

    async def find_by_subnet(self, slug: str) -> list[Agent]:
        """Agents whose subnet_ids array contains the given subnet_id."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentModel).where(
                    AgentModel.subnet_ids.contains(cast([slug], ARRAY(String)))
                )
            )
            return [self._model_to_agent(r) for r in result.scalars().all()]

    async def find_by_tags(self, tags: list[str]) -> list[Agent]:
        """Find agents with ALL of *tags*.

        Online/offline filtering happens at the service layer via
        ``AgentService._filter_by_status`` so the single source of
        truth (Redis alive key) is consulted exactly once per query.
        """
        async with self._session_factory() as session:
            stmt = select(AgentModel)
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
            # Load before delete so we can clear Redis indexes that key on
            # the agent's api_key hash and other fields.
            row = await session.get(AgentModel, agent_id)
            result = await session.execute(
                delete(AgentModel).where(AgentModel.agent_id == agent_id)
            )
            await session.commit()
            deleted = result.rowcount > 0

        if deleted:
            # Mirror the Redis cleanup performed by RedisAgentRepository.delete
            # so that alive checks, inbox, and API-key lookups all invalidate
            # immediately rather than waiting for TTL expiry.
            await self._redis.delete(f"acn:agents:{agent_id}:alive")
            await self._redis.delete(f"acn:inbox:{agent_id}")
            if row is not None:
                meta = row.agent_metadata or {}
                api_key_hash = meta.get("api_key_hash") or (row.agent_metadata or {}).get("api_key")
                if api_key_hash:
                    await self._redis.delete(f"acn:agents:by_api_key:{api_key_hash}")
                if row.owner:
                    await self._redis.srem(f"acn:agents:by_owner:{row.owner}", agent_id)
                await self._redis.srem("acn:agents:unclaimed", agent_id)
                for slug in (row.subnet_ids or []):
                    await self._redis.srem(f"acn:subnets:{slug}:agents", agent_id)

        return deleted

    async def exists(self, agent_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentModel.agent_id).where(AgentModel.agent_id == agent_id)
            )
            return result.scalar() is not None

    async def count_by_subnet(self, slug: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count()).where(
                    AgentModel.subnet_ids.contains(cast([slug], ARRAY(String)))
                )
            )
            return result.scalar() or 0

    async def find_by_api_key(self, key_hash: str) -> Agent | None:
        """Find agent by SHA-256 hash of their API key."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentModel).where(AgentModel.api_key == key_hash)
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

    async def set_alive(self, agent_id: str, ttl: int) -> None:
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

    # =========================================================================
    # Inbound reachability (Redis hash — decoupled from ``alive``)
    # =========================================================================

    async def record_inbound_delivery(
        self,
        agent_id: str,
        *,
        ok: bool,
        probe_ms: float | None = None,
        error: str | None = None,
        ttl: int,
    ) -> None:
        key = _INBOUND_KEY.format(agent_id=agent_id)
        now = datetime.now(UTC).isoformat()
        async with self._redis.pipeline(transaction=False) as pipe:
            if ok:
                pipe.hset(key, mapping={"last_ok_at": now, "consec_fail": 0})
            else:
                pipe.hincrby(key, "consec_fail", 1)
                pipe.hset(key, "last_fail_at", now)
                if error is not None:
                    pipe.hset(key, "last_error", error[:200])
            if probe_ms is not None:
                pipe.hset(key, "last_probe_ms", f"{probe_ms:.1f}")
            pipe.expire(key, ttl)
            await pipe.execute()

    async def get_inbound_health(self, agent_id: str) -> dict[str, object] | None:
        key = _INBOUND_KEY.format(agent_id=agent_id)
        raw = await self._redis.hgetall(key)
        if not raw:
            return None
        data = {_as_str(k): _as_str(v) for k, v in raw.items()}
        out: dict[str, object] = {}
        for field in ("last_ok_at", "last_fail_at", "last_error"):
            if data.get(field):
                out[field] = data[field]
        if data.get("consec_fail") is not None:
            out["consec_fail"] = int(data["consec_fail"])
        if data.get("last_probe_ms") is not None:
            out["last_probe_ms"] = float(data["last_probe_ms"])
        return out or None

    # ``mark_offline_stale`` deliberately removed alongside the
    # heartbeat watchdog. Online-ness is now a function over the Redis
    # ``alive`` TTL (single source of truth), so the column-flipping
    # sweeper is no longer needed. See ``AgentService._filter_by_status``.
    # The ``ix_agents_status_online_agent_id`` partial index that
    # supported this method's keyset scan is retained for one release
    # cycle; Phase 2 drops it with a migration alongside the
    # ``Agent.status`` column itself.
