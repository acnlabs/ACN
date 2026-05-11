"""Reputation Repository Interface (Saga v0.1, off-chain reputation).

Contract for ``reputation_events`` — the off-chain container that stores
per-task feedback / validation rows produced by the settlement saga.
``ReputationService`` (producer) and ``ReputationQueryService`` (reader)
both depend on this interface, not on the SQLAlchemy model, so the
service layer stays free of ``infrastructure.persistence.postgres``.

Why a dedicated DTO ``ReputationEvent`` rather than reusing the ORM model:
the worker's ``reputation_write`` step is allowed to run on a deployment
that has *no* PostgreSQL session in flight — e.g. the same code path
could later support a v0.2 chain-write adapter. Returning ORM objects
would leak SQLAlchemy state into ``services.reputation_service``.

Why ``record`` rather than ``create`` or ``insert``:
the operation is **idempotent**. Calling ``record`` twice with the same
``(agent_id, task_id, kind)`` returns the existing row, not a duplicate
and not an error. ``record`` describes the intent ("ensure this fact is
recorded"); the alternatives suggest a new row every call which would be
wrong for a retried saga step.

Why the interface exposes an optional ``session`` parameter on ``record``:
when the worker writes a reputation row as part of the
``reputation_write`` step, it does so under its own short-lived session
(the worker controls its transaction boundary). Future callers that want
to record reputation as part of a larger transaction (e.g. dispute
arbitration that simultaneously refunds and downgrades reputation) pass
their session in. ``session=None`` => open + commit a fresh one.
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

# Allowed values for ReputationEvent.kind. Validated in
# ``ReputationService`` rather than the DTO so the persistence layer can
# rehydrate historical rows with extended values without raising.
REPUTATION_KIND_FEEDBACK = "feedback"
REPUTATION_KIND_VALIDATION = "validation"
REPUTATION_KINDS = (REPUTATION_KIND_FEEDBACK, REPUTATION_KIND_VALIDATION)


class ReputationEvent(BaseModel):
    """One row in ``reputation_events``, abstracted from the ORM.

    Producers build this object and pass it to :meth:`IReputationRepository.record`.
    The repository fills ``id`` / ``created_at`` from DB defaults — callers
    should leave them unset and accept whatever the returned event reports.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    agent_id: str = Field(
        ...,
        description="Agent being reviewed / validated. Indexed.",
    )
    task_id: str = Field(
        ...,
        description="Originating task. Indexed.",
    )
    kind: str = Field(
        ...,
        description=(
            "'feedback' (creator reviews assignee) or 'validation' "
            "(third-party attestation). v0.1 emits feedback only."
        ),
    )
    signer: str = Field(
        ...,
        description=(
            "Who issued the event. NOT NULL — anonymous reputation is "
            "meaningless for Sybil resistance and dispute review."
        ),
    )

    score: int | None = Field(
        default=None,
        description=(
            "v0.1 always None — task review has approve/reject but no "
            "graded score input. Reserved for v0.2 0-100 score."
        ),
    )
    evidence_uri: str | None = Field(
        default=None,
        description="Optional off-chain pointer (image, IPFS, signed JSON URL).",
    )
    attestation: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Validation signed-JSON payload. ``feedback`` rows leave this None; "
            "``validation`` rows carry the validator's signed proof."
        ),
    )
    event_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-event metadata, JSONB column. v0.1 carries the smoke_test "
            "flag copied from ``task.metadata`` at write time so reputation "
            "queries can filter without joining ``tasks``."
        ),
    )

    # Filled by the repository on read; callers writing should leave None.
    id: int | None = None
    created_at: datetime | None = None


# =============================================================================
# Interface
# =============================================================================


class IReputationRepository(ABC):
    """Abstract contract for the reputation_events store.

    All read paths default to ``include_smoke_test=False`` so production
    reputation summaries are never polluted by smoke-test rows. Tests and
    forensic tools can opt in explicitly.
    """

    # ---------------------------------------------------------------------
    # Producer side
    # ---------------------------------------------------------------------

    @abstractmethod
    async def record(
        self,
        event: ReputationEvent,
        *,
        session: AsyncSession | None = None,
    ) -> ReputationEvent:
        """Insert one reputation event, idempotent on
        ``(agent_id, task_id, kind)``.

        Args:
            event: The event to record.
            session: Outer transaction to participate in. When passed, the
                implementation MUST NOT commit or open a new session — it
                rolls back / commits with the caller. When None, the
                implementation opens + commits its own session.

        Returns:
            The persisted event (with ``id`` and ``created_at`` populated
            by the database). If a row with the same
            ``(agent_id, task_id, kind)`` already existed, returns that
            existing row — making this call a safe no-op for worker
            retries. Callers can detect "was this a new write?" by
            comparing the returned ``id`` to None on input (the input
            never carries an id, the output always does).
        """
        ...

    # ---------------------------------------------------------------------
    # Consumer / read side
    # ---------------------------------------------------------------------

    @abstractmethod
    async def list_for_agent(
        self,
        agent_id: str,
        *,
        kind: str | None = None,
        include_smoke_test: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ReputationEvent]:
        """List reputation events for ``agent_id``, newest first.

        Args:
            agent_id: Target agent.
            kind: Optional filter — ``'feedback'`` or ``'validation'``.
                None returns both.
            include_smoke_test: When False (default), filters out rows
                whose ``event_metadata->>'smoke_test'`` is true. Production
                reads use the default; smoke / ops tools pass True.
            limit: Page size cap.
            offset: Page offset.

        Returns:
            Events ordered by ``created_at DESC``.
        """
        ...

    @abstractmethod
    async def count_for_agent(
        self,
        agent_id: str,
        *,
        kind: str | None = None,
        include_smoke_test: bool = False,
    ) -> int:
        """Count reputation events for ``agent_id``.

        Same filtering semantics as :meth:`list_for_agent`. Cheap enough
        to call on every reputation summary fetch; uses the
        ``ix_reputation_events_agent_id`` index.
        """
        ...

    @abstractmethod
    async def count_kind_since(
        self,
        kind: str,
        since: datetime,
        *,
        include_smoke_test: bool = False,
    ) -> int:
        """Count reputation events of ``kind`` created at or after
        ``since``. Used by the daily reconciliation job to compare
        against the saga completion count.

        Args:
            kind: ``'feedback'`` or ``'validation'``. Filtering at the
                DB layer keeps the query indexable.
            since: Lower bound on ``created_at`` (UTC). For reputation
                rows the create time IS the close time — they are
                inserted once at saga step three and never updated.
            include_smoke_test: Same semantics as the other read
                methods. Defaults to False so production reconciliation
                only sees real traffic.

        Returns:
            Count of matching rows. The
            ``ix_reputation_events_created_at`` index makes this O(log
            n) plus the matching set.
        """
        ...

    @abstractmethod
    async def list_for_task(
        self,
        task_id: str,
        *,
        include_smoke_test: bool = True,
    ) -> list[ReputationEvent]:
        """List all events for a single task. Used by ops / replay.

        Defaults ``include_smoke_test=True`` because the only caller of this
        method today is forensic / smoke-backfill tooling that needs to see
        smoke rows.
        """
        ...
