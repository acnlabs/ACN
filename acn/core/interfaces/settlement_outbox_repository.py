"""Settlement Outbox Repository Interface (Saga v0.1).

Contract for the transactional outbox that backs the settlement saga
described in ``acn/docs/_drafts/settlement-saga-design.md``. Producer
writes one row per ``complete_task`` invocation **in the same
transaction** that flips ``tasks.status``; consumer is
``SettlementWorker`` which picks rows up out-of-band.

Concurrency model — short-lock + state machine + janitor sweep
--------------------------------------------------------------
``claim_batch`` does NOT hold a row lock across the worker's IO. It
runs a short transaction that:

1. ``SELECT ... FOR UPDATE SKIP LOCKED`` rows where
   ``state IN ('pending', 'retrying') AND next_attempt_at <= now``.
2. ``UPDATE ... SET state='paying', updated_at=now`` on those rows.
3. ``COMMIT`` (lock released).

The worker then runs its escrow / reward / reputation IO **without
holding any row lock** and calls ``mark_done`` / ``mark_retry`` /
``mark_dead`` afterwards. A parallel worker calling ``claim_batch``
sees ``state='paying'`` and naturally skips those rows.

Trade-off: a worker crash between step 3 and ``mark_*`` leaves the
row stuck in ``paying``. ``sweep_stuck_paying`` is the recovery
hatch — call it periodically (worker janitor loop runs every 30s)
with ``older_than=now - 5min`` to reset stuck rows back to
``retrying`` so a fresh worker can re-pick them. Eventual
consistency is preserved because the downstream business steps
(escrow release / reward distribute / reputation write) are
implemented with their own idempotency keys; double-execution after
sweep is safe.

Why not hold the row lock across the worker's IO instead? Two
reasons: (a) the IO can take seconds and PgBouncer / Railway will
kill idle connections, leaving zombie locks the server only detects
on TCP keepalive; (b) reasoning about lock heritage across worker
crashes is harder than reasoning about a state-machine sweep.

Why the interface exposes an optional ``session`` parameter on ``enqueue``
-------------------------------------------------------------------------
``enqueue`` MUST participate in the producer's outer transaction —
if the outer transaction rolls back (e.g. CAS save lost a race),
the outbox INSERT also rolls back. So callers in
``task_service.complete_task`` pass their session in; the
implementation MUST NOT commit or open a new session in that case.

When ``session=None`` (e.g. backfill scripts, tests) the
implementation may open its own session and commit.

Worker-side write paths (``mark_done`` / ``mark_retry`` /
``mark_dead`` / ``update_step_status``) open their own short-lived
sessions; they have no outer transactional context to reuse.

The interface exposes the DTO ``SettlementEvent`` rather than the
SQLAlchemy model class, so the worker / service layers don't depend
on ``infrastructure.persistence.postgres``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# =============================================================================
# DTOs
# =============================================================================


class SettlementEvent(BaseModel):
    """One outbox row, abstracted away from the SQLAlchemy model.

    Producers build this object and pass it to :meth:`enqueue`. The
    repository fills ``id`` / ``created_at`` / ``updated_at`` from the
    DB defaults; callers should leave them unset.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    event_id: str = Field(
        ...,
        description=(
            "Business idempotency key, typically uuid5(NS, 'task:trigger'). "
            "UNIQUE at the DB layer — repeat enqueue is silently dropped."
        ),
    )
    task_id: str
    trigger: str = Field(
        ...,
        description="Why the event was emitted. v0.1: 'review_pass' only.",
    )
    payload: dict[str, Any] = Field(
        ...,
        description=(
            "All context the worker needs to execute settlement without "
            "re-reading the (possibly mutated) task row. MUST be "
            "JSON-serialisable — stored as JSONB."
        ),
    )

    # The fields below are set by the repository / worker — producers
    # should not pass them in.
    id: int | None = None
    state: str = "pending"
    # Three-step v0.1 saga: every event starts with all steps pending.
    # Producers that legitimately want to short-circuit a step (e.g.
    # task with reward=0 → mark ``reward_distribute='skipped'`` up
    # front) should construct the full dict explicitly; do not rely
    # on partial dicts being auto-completed by the repository.
    step_status: dict[str, str] = Field(
        default_factory=lambda: {
            "escrow_release": "pending",
            "reward_distribute": "pending",
            "reputation_write": "pending",
        }
    )
    attempts: int = 0
    last_error: str | None = None
    next_attempt_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# =============================================================================
# Interface
# =============================================================================


class ISettlementOutboxRepository(ABC):
    """Abstract contract for the settlement outbox.

    See module docstring for transactional semantics.
    """

    # ---------------------------------------------------------------------
    # Producer side
    # ---------------------------------------------------------------------

    @abstractmethod
    async def enqueue(
        self,
        event: SettlementEvent,
        *,
        session: AsyncSession | None = None,
    ) -> bool:
        """Insert a new outbox row.

        Args:
            event: The event to enqueue. ``event_id`` is the idempotency
                key and is unique at the DB layer.
            session: Outer transaction to participate in. When passed,
                the implementation MUST NOT commit or open a new session.
                When ``None``, the implementation opens its own session
                and commits — provided for the rare case the caller has
                no outer transaction (e.g. backfill scripts).

        Returns:
            True if the row was inserted, False if a row with the same
            ``event_id`` already existed (silent idempotent skip).
        """
        ...

    # ---------------------------------------------------------------------
    # Consumer side
    # ---------------------------------------------------------------------

    @abstractmethod
    async def claim_batch(self, *, limit: int, now: datetime) -> list[SettlementEvent]:
        """Pick up to ``limit`` ready-to-run events and transition them
        to ``state='paying'`` in a short transaction; row lock is then
        released so the worker can do its IO unlocked.

        Required implementation semantics (see module docstring for the
        full concurrency model):

        1. ``SELECT ... FOR UPDATE SKIP LOCKED`` rows with
           ``state IN ('pending', 'retrying') AND next_attempt_at <= now``.
        2. ``UPDATE ... SET state='paying'`` for those rows.
        3. Commit and return the claimed events.

        SKIP LOCKED lets parallel worker replicas avoid each other.
        After this call returns, the rows are NO LONGER locked at the
        database level — concurrency is enforced by the
        ``state='paying'`` filter in the next ``claim_batch``. A worker
        crash mid-IO leaves the row in ``paying`` until
        ``sweep_stuck_paying`` resurrects it.

        Args:
            limit: Maximum number of rows to claim in this batch.
            now: Current time, injected for testability. Rows whose
                ``next_attempt_at > now`` are skipped.

        Returns:
            A list of claimed events (up to ``limit``), each carrying
            its ``event_id``. Empty list when nothing is ready. The
            returned ``state`` field reflects the value *before*
            transition (worker doesn't care).
        """
        ...

    @abstractmethod
    async def mark_done(self, event_id: str) -> None:
        """Transition the row to terminal ``state='done'``.

        No state precondition is checked — the worker that owns the
        ``paying`` slot is trusted to call this exactly once. (If
        ``sweep_stuck_paying`` resurrected the row first, the second
        worker's eventual ``mark_done`` will simply re-mark a
        previously-completed row, which is harmless because the
        downstream side effects are idempotent.)
        """
        ...

    @abstractmethod
    async def mark_retry(
        self,
        event_id: str,
        *,
        error: str,
        next_attempt_at: datetime,
    ) -> None:
        """Transition the row to ``state='retrying'`` for re-pickup.

        Increments ``attempts`` atomically (server-side), records
        ``last_error``, sets ``next_attempt_at`` to the caller's
        backoff schedule.
        """
        ...

    @abstractmethod
    async def mark_dead(self, event_id: str, *, error: str) -> None:
        """Transition the row to terminal ``state='dead'`` (out of
        retries / non-retriable error). Operators must be alerted
        downstream; the row stays in the table for forensic / replay.
        """
        ...

    @abstractmethod
    async def update_step_status(
        self,
        event_id: str,
        *,
        step: str,
        status: str,
    ) -> None:
        """Patch one key in the ``step_status`` JSONB so the worker can
        resume mid-saga after a crash without redoing already-done steps.

        Args:
            step: One of ``escrow_release`` / ``reward_distribute`` /
                ``reputation_write``.
            status: One of ``done`` / ``pending`` / ``skipped``.
        """
        ...

    # ---------------------------------------------------------------------
    # Janitor / DLQ tools
    # ---------------------------------------------------------------------

    @abstractmethod
    async def sweep_stuck_paying(self, *, older_than: datetime) -> int:
        """Recover ``state='paying'`` rows whose worker crashed mid-saga.

        ``claim_batch`` flips eligible rows from ``pending``/``retrying``
        to ``paying`` and then releases the row lock so its IO can run
        unlocked (avoiding zombie row locks under PgBouncer/Railway
        idle-connection kills). The cost is that a crashed worker
        leaves rows stuck in ``paying``; ``sweep_stuck_paying`` resets
        them to ``retrying`` after a quiescence window so a fresh
        worker can re-pick them.

        Args:
            older_than: ``paying`` rows whose ``updated_at < older_than``
                are reset. Caller typically passes ``now - 5 minutes``.

        Returns:
            Number of rows resurrected.
        """
        ...

    # ---------------------------------------------------------------------
    # Observability
    # ---------------------------------------------------------------------

    @abstractmethod
    async def count_by_state(self) -> dict[str, int]:
        """Return a histogram ``{state: count}`` for Prometheus gauges.

        Implementations should index ``state`` so this is O(states).
        """
        ...

    @abstractmethod
    async def count_done_since(
        self,
        since: datetime,
        *,
        trigger: str | None = None,
    ) -> int:
        """Count rows that reached ``state='done'`` since ``since``.

        Used by the daily reconciliation job (see
        ``acn/services/settlement_reconciler.py``) to compare the saga
        completion count against the number of reputation events
        written in the same window — the two are equal under normal
        operation (saga is the sole writer). A non-zero delta is the
        first signal of saga drift in production.

        Args:
            since: Lower bound on ``updated_at`` (UTC). Implementations
                should use ``updated_at`` rather than ``created_at``
                because the row only flips to ``state='done'`` at the
                end of the saga; ``updated_at`` is the close timestamp
                of interest.
            trigger: Optional filter — e.g. ``'review_pass'`` to count
                only review-triggered settlements. ``None`` counts all
                triggers.

        Returns:
            Count of matching rows. Cheap: ``state`` is indexed and
            the predicate ``state='done'`` is highly selective.
        """
        ...
