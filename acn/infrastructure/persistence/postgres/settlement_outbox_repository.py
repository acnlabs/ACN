"""PostgreSQL implementation of ISettlementOutboxRepository (Saga v0.1).

Owns the ``settlement_outbox`` table. See the interface module
docstring for the full concurrency contract; this file implements it
on PostgreSQL. Highlights:

- Producer: ``INSERT ... ON CONFLICT (event_id) DO NOTHING`` — silent
  idempotent enqueue. ``enqueue`` participates in the caller's
  ``AsyncSession`` when one is passed (so the outbox row commits or
  rolls back atomically with the producer's CAS update); otherwise
  it opens and commits its own short-lived session.

- Consumer ``claim_batch``: short-lock pattern. A single transaction
  ``SELECT ... FOR UPDATE SKIP LOCKED`` -> ``UPDATE state='paying'``
  -> ``COMMIT``. Lock is released before the worker does its IO;
  parallel workers see ``state='paying'`` and skip naturally.

- Janitor ``sweep_stuck_paying``: resets ``state='paying'`` rows
  whose ``updated_at`` is older than the caller's threshold back to
  ``retrying`` — recovery hatch for worker crashes.

- ``mark_done`` / ``mark_retry`` / ``mark_dead`` /
  ``update_step_status``: each opens its own short-lived session
  (no outer transaction to compose with — the worker holds no row
  lock by design).
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import structlog
from sqlalchemy import bindparam, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ....core.interfaces.settlement_outbox_repository import (
    ISettlementOutboxRepository,
    SettlementEvent,
)
from .models import SettlementOutboxModel

logger = structlog.get_logger()


def _model_to_event(row: SettlementOutboxModel) -> SettlementEvent:
    return SettlementEvent(
        id=row.id,
        event_id=str(row.event_id),
        task_id=row.task_id,
        trigger=row.trigger,
        payload=row.payload,
        state=row.state,
        step_status=row.step_status or {},
        attempts=row.attempts,
        last_error=row.last_error,
        next_attempt_at=row.next_attempt_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PostgresSettlementOutboxRepository(ISettlementOutboxRepository):
    """PostgreSQL-backed settlement outbox.

    Constructor takes an ``async_sessionmaker`` so each method can open
    its own short-lived session when no outer session is provided. The
    factory is reused across calls — connections come from the pool.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _session_scope(self, session: AsyncSession | None):
        """Yield ``session`` if passed (no commit / no close) — caller
        owns the transaction. Otherwise open + commit + close ourselves.

        Justification for the no-commit branch: the whole point of the
        outer-session path is that the outbox INSERT lives in the same
        ACID transaction as the producer's CAS save. If we committed
        early here, the saga's atomicity guarantee evaporates.
        """
        if session is not None:
            yield session
            return
        async with self._session_factory() as own_session:
            yield own_session
            await own_session.commit()

    # ------------------------------------------------------------------
    # Producer side
    # ------------------------------------------------------------------

    async def enqueue(
        self,
        event: SettlementEvent,
        *,
        session: AsyncSession | None = None,
    ) -> bool:
        """Insert with ON CONFLICT DO NOTHING on ``event_id``.

        Returns True if a row was inserted, False if a duplicate
        ``event_id`` was silently rejected (idempotent re-enqueue —
        producer can retry safely).
        """
        async with self._session_scope(session) as sess:
            stmt = (
                pg_insert(SettlementOutboxModel)
                .values(
                    event_id=event.event_id,
                    task_id=event.task_id,
                    trigger=event.trigger,
                    payload=event.payload,
                    state="pending",
                    # ``step_status`` default lives on the DTO
                    # (``SettlementEvent.step_status``) so producers
                    # always see a fully-populated three-step dict;
                    # we transparently pass through whatever the
                    # producer constructed.
                    step_status=event.step_status,
                    attempts=0,
                    # Default next_attempt_at = now() so the worker
                    # picks the row up on its next poll without delay.
                )
                .on_conflict_do_nothing(index_elements=["event_id"])
            )
            result = await sess.execute(stmt)
            inserted = result.rowcount == 1
            if not inserted:
                logger.info(
                    "settlement_outbox_duplicate_enqueue",
                    event_id=event.event_id,
                    task_id=event.task_id,
                )
            return inserted

    # ------------------------------------------------------------------
    # Consumer side
    # ------------------------------------------------------------------

    async def claim_batch(
        self,
        *,
        limit: int,
        now: datetime,
    ) -> list[SettlementEvent]:
        """Select up to ``limit`` rows eligible for execution and
        mark them ``state='paying'`` to release the row lock.

        Design choice: rather than holding the row lock across the
        worker's network calls (escrow release, reward distribute,
        reputation write) — which can each take seconds — we briefly
        lock, **flip ``state`` to a non-claimable value**, then commit
        and let the worker do its IO unlocked. The worker re-claims
        the row by ``event_id`` when calling ``mark_done`` etc., and
        a parallel worker would see ``state='paying'`` and skip.

        This is safer in practice than long-held row locks for two
        reasons:
        1. asyncpg / PgBouncer / Railway middleware will silently
           kill idle connections at the 5-10 min mark; a held row
           lock then becomes a zombie until the server detects.
        2. Worker crashes leave behind ``state='paying'`` rows that
           can be swept by a janitor (or detected by ``updated_at``
           drift) — simpler than reasoning about lock heritage.

        The trade-off: in the ``paying`` window a process crash leaves
        the row stuck — it won't be re-picked until manual reset
        (``UPDATE ... SET state='retrying' WHERE state='paying'
        AND updated_at < now() - interval '5 minutes'``). This is
        documented in the DLQ runbook (plan §7).
        """
        async with self._session_factory() as sess:
            # SKIP LOCKED is the standard pattern for concurrent
            # outbox workers. ORDER BY next_attempt_at means oldest
            # ready rows are processed first.
            select_stmt = (
                select(SettlementOutboxModel)
                .where(
                    SettlementOutboxModel.state.in_(("pending", "retrying")),
                    SettlementOutboxModel.next_attempt_at <= now,
                )
                .order_by(SettlementOutboxModel.next_attempt_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            result = await sess.execute(select_stmt)
            rows: Sequence[SettlementOutboxModel] = result.scalars().all()
            if not rows:
                await sess.rollback()
                return []

            event_ids = [str(r.event_id) for r in rows]
            await sess.execute(
                update(SettlementOutboxModel)
                .where(SettlementOutboxModel.event_id.in_(event_ids))
                .values(state="paying", updated_at=datetime.now(UTC))
            )
            await sess.commit()

            return [_model_to_event(r) for r in rows]

    async def mark_done(self, event_id: str) -> None:
        async with self._session_factory() as sess:
            await sess.execute(
                update(SettlementOutboxModel)
                .where(SettlementOutboxModel.event_id == event_id)
                .values(
                    state="done",
                    last_error=None,
                    updated_at=datetime.now(UTC),
                )
            )
            await sess.commit()

    async def mark_retry(
        self,
        event_id: str,
        *,
        error: str,
        next_attempt_at: datetime,
    ) -> None:
        async with self._session_factory() as sess:
            # attempts++ happens atomically here, not in Python — keeps
            # the counter correct under parallel worker / re-entrant
            # claim scenarios.
            await sess.execute(
                update(SettlementOutboxModel)
                .where(SettlementOutboxModel.event_id == event_id)
                .values(
                    state="retrying",
                    attempts=SettlementOutboxModel.attempts + 1,
                    last_error=error,
                    next_attempt_at=next_attempt_at,
                    updated_at=datetime.now(UTC),
                )
            )
            await sess.commit()

    async def mark_dead(self, event_id: str, *, error: str) -> None:
        async with self._session_factory() as sess:
            await sess.execute(
                update(SettlementOutboxModel)
                .where(SettlementOutboxModel.event_id == event_id)
                .values(
                    state="dead",
                    attempts=SettlementOutboxModel.attempts + 1,
                    last_error=error,
                    updated_at=datetime.now(UTC),
                )
            )
            await sess.commit()
            logger.warning(
                "settlement_outbox_event_dead",
                event_id=event_id,
                last_error=error[:500],  # cap for log line size
            )

    async def update_step_status(
        self,
        event_id: str,
        *,
        step: str,
        status: str,
    ) -> None:
        """JSONB patch — use ``jsonb_set`` so we don't have to read,
        mutate in Python, write back (and risk a lost-update race).

        ``updated_at`` is passed from the Python side (not ``now()``)
        for consistency with the other write paths: tests that
        ``freeze_time`` can pin all timestamps in one place.
        """
        async with self._session_factory() as sess:
            await sess.execute(
                text(
                    """
                    UPDATE settlement_outbox
                    SET step_status = jsonb_set(
                            COALESCE(step_status, '{}'::jsonb),
                            :path,
                            to_jsonb(:status::text),
                            true
                        ),
                        updated_at = :updated_at
                    WHERE event_id = :event_id
                    """
                ).bindparams(
                    bindparam("path", value=[step]),
                    bindparam("status", value=status),
                    bindparam("event_id", value=event_id),
                    bindparam("updated_at", value=datetime.now(UTC)),
                )
            )
            await sess.commit()

    # ------------------------------------------------------------------
    # Janitor
    # ------------------------------------------------------------------

    async def sweep_stuck_paying(self, *, older_than: datetime) -> int:
        """Reset ``paying`` rows older than ``older_than`` back to
        ``retrying`` so they get re-picked. See interface docstring.
        """
        async with self._session_factory() as sess:
            result = await sess.execute(
                update(SettlementOutboxModel)
                .where(
                    SettlementOutboxModel.state == "paying",
                    SettlementOutboxModel.updated_at < older_than,
                )
                .values(
                    state="retrying",
                    last_error=text("COALESCE(last_error, '') || ' [swept after paying timeout]'"),
                    updated_at=datetime.now(UTC),
                )
            )
            await sess.commit()
            n = result.rowcount or 0
            if n > 0:
                logger.warning(
                    "settlement_outbox_swept_stuck_paying",
                    n=n,
                    older_than=older_than.isoformat(),
                )
            return n

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    async def count_by_state(self) -> dict[str, int]:
        """Cheap histogram used by Prometheus gauges. Group-by-state is
        O(states) — the table's partial index doesn't cover this query
        but it scans state cardinality (~5) so it's still cheap up to
        a few million rows.

        Always returns all five canonical states with zero defaults so
        downstream metric exporters can index ``counts[state]`` without
        KeyError when a state happens to be empty.
        """
        async with self._session_factory() as sess:
            result = await sess.execute(
                select(
                    SettlementOutboxModel.state,
                    func.count().label("n"),
                ).group_by(SettlementOutboxModel.state)
            )
            counts: dict[str, int] = {
                "pending": 0,
                "paying": 0,
                "retrying": 0,
                "done": 0,
                "dead": 0,
            }
            for row in result.all():
                counts[row.state] = row.n
            return counts

    async def count_done_since(
        self,
        since: datetime,
        *,
        trigger: str | None = None,
    ) -> int:
        """Count ``state='done'`` rows whose ``updated_at >= since``.

        The combined predicate (state + updated_at) is well-served by
        scanning the small ``state='done'`` partition first; we don't
        need a composite index for the daily reconciliation cadence.
        Add ``WHERE trigger=...`` last because trigger cardinality is
        low (v0.1 only emits ``'review_pass'``) and most callers will
        pass it.
        """
        async with self._session_factory() as sess:
            stmt = (
                select(func.count())
                .select_from(SettlementOutboxModel)
                .where(SettlementOutboxModel.state == "done")
                .where(SettlementOutboxModel.updated_at >= since)
            )
            if trigger is not None:
                stmt = stmt.where(SettlementOutboxModel.trigger == trigger)
            return int((await sess.execute(stmt)).scalar_one() or 0)
