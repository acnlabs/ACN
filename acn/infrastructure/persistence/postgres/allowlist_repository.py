"""Postgres Implementation of the allowlist source-of-truth (Phase 2 PR #2).

Storage layout: ``agent_allowlist(owner_id, target_id, created_at,
reason)`` — see ``models.py:AgentAllowlistModel`` for the SQL
declaration including foreign keys to ``agents.agent_id`` with
``ON DELETE CASCADE`` (agent unregistration auto-cleans).

This module owns the durable side of the dual-layer storage; the
Redis SET cache lives in
``acn/infrastructure/persistence/redis/allowlist_repository.py``
and the dual-write ordering policy lives in
``acn/services/allowlist_service.py``. Repository methods here are
atomic w.r.t. PG only; concurrency control between PG and Redis is
the service layer's job.

Reads use ``ON CONFLICT DO NOTHING`` semantics for ``add`` so the
service layer can rely on the row-count to detect the idempotent
re-add path. Removes use ``DELETE ... WHERE`` and report whether a
row was actually deleted.
"""

from __future__ import annotations

import structlog  # type: ignore[import-untyped]
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ....core.exceptions import AllowlistCapacityExceededError
from ....core.interfaces import AllowlistEntry, IAllowlistRepository
from .models import AgentAllowlistModel

logger = structlog.get_logger()


# Postgres SQLSTATE for ``check_violation`` — raised by the per-owner
# capacity trigger (``trg_agent_allowlist_capacity``, migration
# ``f6a7b8c9d0e1``) when an INSERT would exceed ``MAX_ALLOWLIST_SIZE``.
# We map the raw IntegrityError to the domain exception here so the
# service / route layers don't have to know about pgcodes — they just
# catch ``AllowlistCapacityExceededError``.
_PG_CHECK_VIOLATION = "23514"


class PostgresAllowlistRepository(IAllowlistRepository):
    """Postgres-backed allowlist repository — durable source of truth.

    Args:
        session_factory: async sessionmaker. Same pattern as
            ``PostgresAgentRepository`` so wiring stays uniform.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def add(
        self,
        owner_id: str,
        target_id: str,
        reason: str | None = None,
    ) -> bool:
        """Insert (owner_id, target_id) row.

        Uses Postgres-specific ``INSERT ... ON CONFLICT DO NOTHING``
        so a concurrent duplicate does not raise IntegrityError —
        instead the row count is 0 and the service layer surfaces
        the idempotent path. We could use generic
        ``INSERT IGNORE`` but Postgres-specific dialect is the
        natural fit; this codebase is Postgres-only on the durable
        side anyway (Redis-only repos exist for caches).

        **Race-safe capacity (PR #2 v3 review P1-A1)**: a
        ``BEFORE INSERT`` trigger (``trg_agent_allowlist_capacity``)
        re-checks ``count(*) < MAX_ALLOWLIST_SIZE`` inside a
        per-owner ``pg_advisory_xact_lock`` so two concurrent
        ``add()`` calls cannot both pass the service-layer pre-check
        and end up writing 501. The trigger raises
        ``ERRCODE 23514 (check_violation)`` which we catch and
        re-raise as the domain ``AllowlistCapacityExceededError``.
        The service-layer pre-check still runs — it short-circuits
        the common case without paying for an INSERT/rollback round
        trip; the trigger is purely a tail-risk safety net.

        Returns:
            True if the row is new (inserted), False if it already
            existed (idempotent — the route layer still responds 200
            and the service skips the capacity check).

        Raises:
            AllowlistCapacityExceededError: only when the
                race-safety trigger fires (i.e. the service-layer
                pre-check missed a concurrent INSERT). Service /
                route layers handle both pre-check and trigger paths
                uniformly via this single exception type.
        """
        stmt = (
            pg_insert(AgentAllowlistModel)
            .values(owner_id=owner_id, target_id=target_id, reason=reason)
            .on_conflict_do_nothing(index_elements=["owner_id", "target_id"])
        )
        async with self._session_factory() as session:
            try:
                result = await session.execute(stmt)
                await session.commit()
                return result.rowcount > 0
            except IntegrityError as exc:
                # ``exc.orig`` is the underlying DB-API exception
                # (asyncpg or psycopg). Both expose the SQLSTATE on
                # ``.sqlstate`` (asyncpg) or ``.pgcode`` (psycopg).
                pgcode = getattr(exc.orig, "sqlstate", None) or getattr(
                    exc.orig, "pgcode", None
                )
                if pgcode == _PG_CHECK_VIOLATION:
                    await session.rollback()
                    raise AllowlistCapacityExceededError(
                        f"Allowlist capacity reached for {owner_id!r}; "
                        f"remove some entries first"
                    ) from exc
                # Some other constraint blew up — let it propagate.
                raise

    async def remove(self, owner_id: str, target_id: str) -> bool:
        """Delete the row if present.

        Returns True if a row was actually removed, False if no
        such row existed. Drives the route's idempotent
        repeat-DELETE → 200 behaviour.
        """
        stmt = delete(AgentAllowlistModel).where(
            AgentAllowlistModel.owner_id == owner_id,
            AgentAllowlistModel.target_id == target_id,
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0

    async def is_member(self, owner_id: str, target_id: str) -> bool:
        """Direct PG check — bypasses cache.

        This path is the cold-start / PG-fallback branch invoked by
        ``RedisAllowlistRepository`` when its cache misses. It is
        NOT the steady-state inbound check — under nominal load
        every check is served from the Redis SET. Keeping the PG
        version cheap (``LIMIT 1``, primary-key seek) bounds cold
        cost on cache rebuild.
        """
        stmt = select(AgentAllowlistModel.target_id).where(
            AgentAllowlistModel.owner_id == owner_id,
            AgentAllowlistModel.target_id == target_id,
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None

    async def list_targets(
        self,
        owner_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AllowlistEntry]:
        """List entries (most-recent first) — used by the owner GET API."""
        stmt = (
            select(
                AgentAllowlistModel.target_id,
                AgentAllowlistModel.created_at,
                AgentAllowlistModel.reason,
            )
            .where(AgentAllowlistModel.owner_id == owner_id)
            .order_by(AgentAllowlistModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = result.all()
        return [
            AllowlistEntry(
                target_id=row.target_id,
                created_at=row.created_at,
                reason=row.reason,
            )
            for row in rows
        ]

    async def count_for_owner(self, owner_id: str) -> int:
        """Number of allowlist rows owned by ``owner_id`` (capacity check)."""
        stmt = select(func.count()).where(
            AgentAllowlistModel.owner_id == owner_id
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return int(result.scalar() or 0)

    async def list_target_ids(self, owner_id: str) -> list[str]:
        """Return just the target ids — used by the cache rebuild path.

        Not on the ``IAllowlistRepository`` interface because it is
        a PG-specific helper used to feed the Redis ``pg_loader``;
        the Redis layer doesn't need ``created_at`` / ``reason``
        for its SET. Kept as a public method (no leading
        underscore) so the cache rebuild wiring in ``api.py`` can
        bind to it via a one-liner closure.
        """
        stmt = select(AgentAllowlistModel.target_id).where(
            AgentAllowlistModel.owner_id == owner_id
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [row[0] for row in result.all()]
