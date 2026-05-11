"""PostgreSQL implementation of IReputationRepository (Saga v0.1).

Owns the ``reputation_events`` table. See
``acn/acn/core/interfaces/reputation_repository.py`` for the contract;
this module implements it on PostgreSQL.

Highlights:

- Producer: ``INSERT ... ON CONFLICT (agent_id, task_id, kind) DO
  NOTHING RETURNING *`` to silently fold worker retries into one row.
  On conflict the RETURNING clause yields nothing, and the
  implementation falls back to a SELECT so the caller always gets back
  a fully-populated DTO (existing row).

- ``record`` participates in the caller's session when one is passed —
  used today by callers that bundle reputation writes with other DB
  changes inside an outer transaction (e.g. v1 dispute arbitration).
  In the v0.1 worker, ``reputation_write`` calls ``record`` with
  ``session=None`` because the worker has no outer transaction.

- Reads (``list_for_agent`` / ``count_for_agent`` / ``list_for_task``):
  filtered on ``event_metadata->>'smoke_test'`` to keep smoke rows
  out of production summaries by default. JSONB ``->>`` returns text,
  so the filter is ``(event_metadata->>'smoke_test') IS DISTINCT FROM
  'true'`` — IS DISTINCT FROM correctly handles NULL (no metadata
  flag at all) which the regular ``<>`` operator would skip.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import datetime

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ....core.interfaces.reputation_repository import (
    IReputationRepository,
    ReputationEvent,
)
from .models import ReputationEventModel

logger = structlog.get_logger()


def _model_to_event(row: ReputationEventModel) -> ReputationEvent:
    return ReputationEvent(
        id=row.id,
        agent_id=row.agent_id,
        task_id=row.task_id,
        kind=row.kind,
        score=row.score,
        evidence_uri=row.evidence_uri,
        signer=row.signer,
        attestation=row.attestation,
        event_metadata=row.event_metadata or {},
        created_at=row.created_at,
    )


# Sentinel for "filter smoke_test out". JSONB ``->>`` returns text; an
# event with no metadata flag yields NULL, so we use IS DISTINCT FROM
# (true-aware NULL handling) rather than ``<>`` which would drop NULL
# rows along with the smoke rows.
_NOT_SMOKE_TEST_CLAUSE = text(
    "(event_metadata->>'smoke_test') IS DISTINCT FROM 'true'"
)


class PostgresReputationRepository(IReputationRepository):
    """PostgreSQL-backed reputation_events store.

    Constructor takes an ``async_sessionmaker`` so each method can open
    a short-lived session when no outer session is provided.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _session_scope(self, session: AsyncSession | None):
        """Yield ``session`` if passed (no commit / no close); otherwise
        open + commit + close ourselves.

        Same rationale as ``PostgresSettlementOutboxRepository`` — when
        the caller passes a session in, we mustn't commit because the
        caller may roll back; when no outer session is provided we own
        the transaction end-to-end.
        """
        if session is not None:
            yield session
            return
        async with self._session_factory() as own_session:
            yield own_session
            await own_session.commit()

    async def _fetch_existing(
        self,
        sess: AsyncSession,
        agent_id: str,
        task_id: str,
        kind: str,
    ) -> ReputationEvent | None:
        """Read the row for the unique key. Used when INSERT ... ON
        CONFLICT DO NOTHING returns no rows (collision) and we still
        need to return a fully-populated DTO to the caller.
        """
        stmt = select(ReputationEventModel).where(
            ReputationEventModel.agent_id == agent_id,
            ReputationEventModel.task_id == task_id,
            ReputationEventModel.kind == kind,
        )
        row = (await sess.execute(stmt)).scalar_one_or_none()
        return _model_to_event(row) if row is not None else None

    # ------------------------------------------------------------------
    # Producer
    # ------------------------------------------------------------------

    async def record(
        self,
        event: ReputationEvent,
        *,
        session: AsyncSession | None = None,
    ) -> ReputationEvent:
        """Idempotent insert.

        Returns the persisted event (newly created OR pre-existing) so
        callers don't need to branch on insertion vs. duplicate. Worker
        retries land here repeatedly and see consistent behaviour.

        .. warning::

           **Outer-session callers**: if you pass ``session`` and later
           ``await session.rollback()``, the returned DTO will carry an
           ``id`` value drawn from the PostgreSQL sequence (sequences are
           NOT rolled back — that's a Postgres feature, not a bug), but
           the actual row will NOT exist in the database. Do NOT treat
           a non-None ``id`` as a "persisted" signal when you're
           composing this write inside a larger transaction; check for
           commit success at the outer layer.

           Self-session callers (``session=None``) are safe: this method
           commits the transaction before returning, so a non-None
           ``id`` always corresponds to a real row.
        """
        async with self._session_scope(session) as sess:
            stmt = (
                pg_insert(ReputationEventModel)
                .values(
                    agent_id=event.agent_id,
                    task_id=event.task_id,
                    kind=event.kind,
                    score=event.score,
                    evidence_uri=event.evidence_uri,
                    signer=event.signer,
                    attestation=event.attestation,
                    event_metadata=event.event_metadata or {},
                )
                .on_conflict_do_nothing(
                    index_elements=["agent_id", "task_id", "kind"],
                )
                .returning(ReputationEventModel)
            )
            result = await sess.execute(stmt)
            inserted_row = result.scalar_one_or_none()
            if inserted_row is not None:
                # Fresh insert — return the DB-populated row.
                #
                # WARNING for outer-session callers: ``inserted_row`` is
                # bound to ``sess``. If the caller subsequently rolls
                # back, the DTO we return remains a valid Python object
                # but its DB row is gone. Callers using outer sessions
                # MUST handle their own rollback semantics — they're the
                # ones who decided to compose the write into a wider
                # transaction.
                return _model_to_event(inserted_row)

            # Collision — fetch the existing row so the caller still
            # gets a populated DTO. The conflict means an earlier saga
            # attempt (or a concurrent worker) already wrote this exact
            # (agent_id, task_id, kind) — the worker's
            # ``reputation_write`` step is allowed to retry, that's the
            # whole reason we hold the unique constraint.
            existing = await self._fetch_existing(
                sess, event.agent_id, event.task_id, event.kind
            )
            if existing is None:
                # Defensive: ON CONFLICT said collision, but the row
                # isn't there on re-read. The only realistic cause is
                # a concurrent DELETE which v0.1 forbids (reputation
                # rows are immutable). Surface as an exception so an
                # operator notices.
                logger.error(
                    "reputation_record_phantom_conflict",
                    agent_id=event.agent_id,
                    task_id=event.task_id,
                    kind=event.kind,
                )
                raise RuntimeError(
                    "ON CONFLICT DO NOTHING reported a conflict but the "
                    "row is missing on re-read — "
                    "reputation_events rows must not be deleted"
                )
            logger.info(
                "reputation_record_idempotent_skip",
                agent_id=event.agent_id,
                task_id=event.task_id,
                kind=event.kind,
            )
            return existing

    # ------------------------------------------------------------------
    # Consumer / read side
    # ------------------------------------------------------------------

    async def list_for_agent(
        self,
        agent_id: str,
        *,
        kind: str | None = None,
        include_smoke_test: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ReputationEvent]:
        async with self._session_factory() as sess:
            stmt = (
                select(ReputationEventModel)
                .where(ReputationEventModel.agent_id == agent_id)
                .order_by(ReputationEventModel.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            if kind is not None:
                stmt = stmt.where(ReputationEventModel.kind == kind)
            if not include_smoke_test:
                stmt = stmt.where(_NOT_SMOKE_TEST_CLAUSE)
            result = await sess.execute(stmt)
            rows: Sequence[ReputationEventModel] = result.scalars().all()
            return [_model_to_event(r) for r in rows]

    async def count_for_agent(
        self,
        agent_id: str,
        *,
        kind: str | None = None,
        include_smoke_test: bool = False,
    ) -> int:
        async with self._session_factory() as sess:
            stmt = select(func.count()).select_from(ReputationEventModel).where(
                ReputationEventModel.agent_id == agent_id
            )
            if kind is not None:
                stmt = stmt.where(ReputationEventModel.kind == kind)
            if not include_smoke_test:
                stmt = stmt.where(_NOT_SMOKE_TEST_CLAUSE)
            return int((await sess.execute(stmt)).scalar_one() or 0)

    async def count_kind_since(
        self,
        kind: str,
        since: datetime,
        *,
        include_smoke_test: bool = False,
    ) -> int:
        """Count rows of one ``kind`` written at or after ``since``.

        Reads against the ``ix_reputation_events_created_at`` index;
        with ``kind`` (low-cardinality) as a final filter the planner
        typically picks an index range scan then drops the smoke-test
        / kind predicates as a post-filter. Cheap enough to run daily
        without paging.
        """
        async with self._session_factory() as sess:
            stmt = (
                select(func.count())
                .select_from(ReputationEventModel)
                .where(ReputationEventModel.kind == kind)
                .where(ReputationEventModel.created_at >= since)
            )
            if not include_smoke_test:
                stmt = stmt.where(_NOT_SMOKE_TEST_CLAUSE)
            return int((await sess.execute(stmt)).scalar_one() or 0)

    async def list_for_task(
        self,
        task_id: str,
        *,
        include_smoke_test: bool = True,
    ) -> list[ReputationEvent]:
        async with self._session_factory() as sess:
            stmt = (
                select(ReputationEventModel)
                .where(ReputationEventModel.task_id == task_id)
                .order_by(ReputationEventModel.created_at.asc())
            )
            if not include_smoke_test:
                stmt = stmt.where(_NOT_SMOKE_TEST_CLAUSE)
            result = await sess.execute(stmt)
            rows: Sequence[ReputationEventModel] = result.scalars().all()
            return [_model_to_event(r) for r in rows]
