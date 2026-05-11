"""In-memory fakes for settlement saga integration tests.

These are NOT mocks — they implement the full interface contract
faithfully so the worker exercises real state machine transitions
under test. The PG implementations
(``PostgresSettlementOutboxRepository`` /
``PostgresReputationRepository``) are tested separately under
``tests/integration/`` against a real database; the worker-side
saga tests in ``test_settlement_saga.py`` ride these fakes so they
don't need PG infrastructure to verify saga orchestration.

Contract notes (must match the abstract interfaces):

- ``FakeSettlementOutboxRepository`` is a 5-state machine
  (``pending`` / ``paying`` / ``retrying`` / ``done`` / ``dead``)
  with ``event_id`` UNIQUE and JSONB-style ``step_status`` patch
  semantics.
- ``FakeReputationRepository`` is idempotent on
  ``(agent_id, task_id, kind)`` and supports the
  ``include_smoke_test`` filter on every read.

Single-event-loop only: these fakes use plain dicts + a single
asyncio task ID per claim, so they are NOT safe under multiple
threads. The poll loop and janitor loop on ``SettlementWorker``
already run on the same event loop, so this is fine.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from acn.core.interfaces.reputation_repository import (
    IReputationRepository,
    ReputationEvent,
)
from acn.core.interfaces.settlement_outbox_repository import (
    ISettlementOutboxRepository,
    SettlementEvent,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# =============================================================================
# Outbox
# =============================================================================


class FakeSettlementOutboxRepository(ISettlementOutboxRepository):
    """In-memory faithful implementation of the outbox state machine.

    Stored rows are mutable copies of ``SettlementEvent``; each
    state-changing method updates the in-place dict, mimicking
    ``UPDATE settlement_outbox SET ...``. Repeat ``enqueue`` with
    the same ``event_id`` returns ``False`` (UNIQUE silently
    skipped) — the same semantic the PG layer implements via
    ``ON CONFLICT DO NOTHING``.
    """

    def __init__(self) -> None:
        # event_id -> mutable row dict. We store dicts (not the
        # SettlementEvent pydantic model) because the simulated
        # JSONB ``step_status`` patches must be in-place; pydantic
        # would copy-on-set and lose the reference.
        self._rows: dict[str, dict[str, Any]] = {}
        # Hook lets a test inject a chaos-monkey: e.g. raise on the
        # 3rd ``claim_batch`` to simulate PgBouncer drop. Defaults
        # to no-op.
        self.claim_batch_hook: Any = None

    # ---------------------------------------------------------------------
    # Producer side
    # ---------------------------------------------------------------------

    async def enqueue(
        self,
        event: SettlementEvent,
        *,
        session: AsyncSession | None = None,
    ) -> bool:
        # ``session`` is ignored — fakes don't have a transaction
        # boundary. The PG layer is what tests via outer session
        # rollback in ``tests/integration/``.
        if event.event_id in self._rows:
            return False
        now = datetime.now(UTC)
        self._rows[event.event_id] = {
            "event_id": event.event_id,
            "task_id": event.task_id,
            "trigger": event.trigger,
            "payload": deepcopy(event.payload),
            "state": "pending",
            "step_status": deepcopy(event.step_status),
            "attempts": 0,
            "last_error": None,
            "next_attempt_at": None,
            "created_at": now,
            "updated_at": now,
            # Internal field, not part of the interface — lets the
            # fake remember when ``state='paying'`` started so
            # ``sweep_stuck_paying`` can be deterministic.
            "_paying_since": None,
        }
        return True

    # ---------------------------------------------------------------------
    # Consumer side
    # ---------------------------------------------------------------------

    async def claim_batch(self, *, limit: int, now: datetime) -> list[SettlementEvent]:
        if self.claim_batch_hook is not None:
            # Allow tests to inject transient DB failures
            # (the hook can raise to simulate ``claim_batch_failed``).
            await self.claim_batch_hook(self)

        claimed: list[SettlementEvent] = []
        for row in self._rows.values():
            if len(claimed) >= limit:
                break
            if row["state"] not in ("pending", "retrying"):
                continue
            if row["next_attempt_at"] is not None and row["next_attempt_at"] > now:
                # Backoff window not yet elapsed.
                continue
            # Transition to paying. SKIP LOCKED behaviour:
            # concurrent claim_batch on the same instance would see
            # the row's state already flipped and skip it — but our
            # fake is single-event-loop so concurrent calls don't
            # interleave inside this loop. The "two-worker"
            # contract is exercised in the real PG integration
            # test suite.
            row["state"] = "paying"
            row["_paying_since"] = now
            row["updated_at"] = now
            claimed.append(self._row_to_event(row))
        return claimed

    async def mark_done(self, event_id: str) -> None:
        row = self._require(event_id)
        row["state"] = "done"
        row["updated_at"] = datetime.now(UTC)
        row["_paying_since"] = None

    async def mark_retry(
        self,
        event_id: str,
        *,
        error: str,
        next_attempt_at: datetime,
    ) -> None:
        row = self._require(event_id)
        row["state"] = "retrying"
        row["attempts"] = row["attempts"] + 1
        row["last_error"] = error
        row["next_attempt_at"] = next_attempt_at
        row["updated_at"] = datetime.now(UTC)
        row["_paying_since"] = None

    async def mark_dead(self, event_id: str, *, error: str) -> None:
        row = self._require(event_id)
        row["state"] = "dead"
        # PG repo increments attempts here too (mark_dead happens after
        # the worker decided this attempt was the last) — match it.
        row["attempts"] = row["attempts"] + 1
        row["last_error"] = error
        row["updated_at"] = datetime.now(UTC)
        row["_paying_since"] = None

    async def update_step_status(
        self,
        event_id: str,
        *,
        step: str,
        status: str,
    ) -> None:
        row = self._require(event_id)
        # JSONB patch semantic — overwrite one key in the dict.
        row["step_status"][step] = status
        row["updated_at"] = datetime.now(UTC)

    # ---------------------------------------------------------------------
    # Janitor / DLQ tools
    # ---------------------------------------------------------------------

    async def sweep_stuck_paying(self, *, older_than: datetime) -> int:
        swept = 0
        for row in self._rows.values():
            if row["state"] != "paying":
                continue
            paying_since = row["_paying_since"]
            if paying_since is None or paying_since >= older_than:
                continue
            # Resurrect: paying -> retrying without bumping
            # attempts, mirroring PG sweep semantics. The next
            # claim_batch will pick this up.
            row["state"] = "retrying"
            row["next_attempt_at"] = None
            row["updated_at"] = datetime.now(UTC)
            row["_paying_since"] = None
            swept += 1
        return swept

    # ---------------------------------------------------------------------
    # Observability
    # ---------------------------------------------------------------------

    async def count_by_state(self) -> dict[str, int]:
        # Always return the full canonical set so dashboards don't
        # show "this state vanished" when its count is zero.
        counts = dict.fromkeys(("pending", "paying", "retrying", "done", "dead"), 0)
        for row in self._rows.values():
            counts[row["state"]] += 1
        return counts

    async def count_done_since(
        self,
        since: datetime,
        *,
        trigger: str | None = None,
    ) -> int:
        n = 0
        for row in self._rows.values():
            if row["state"] != "done":
                continue
            if row["updated_at"] < since:
                continue
            if trigger is not None and row["trigger"] != trigger:
                continue
            n += 1
        return n

    # ---------------------------------------------------------------------
    # Test helpers (not part of the interface)
    # ---------------------------------------------------------------------

    def get_row(self, event_id: str) -> dict[str, Any]:
        """Inspect a row's full mutable state. Tests use this to
        assert step_status / attempts / last_error directly."""
        return self._rows[event_id]

    def all_rows(self) -> list[dict[str, Any]]:
        return list(self._rows.values())

    # ---------------------------------------------------------------------
    # Internals
    # ---------------------------------------------------------------------

    def _require(self, event_id: str) -> dict[str, Any]:
        if event_id not in self._rows:
            raise AssertionError(f"FakeSettlementOutboxRepository: event {event_id!r} not found")
        return self._rows[event_id]

    @staticmethod
    def _row_to_event(row: dict[str, Any]) -> SettlementEvent:
        # Copy fields the worker reads; everything else is internal.
        return SettlementEvent(
            event_id=row["event_id"],
            task_id=row["task_id"],
            trigger=row["trigger"],
            payload=deepcopy(row["payload"]),
            state=row["state"],
            step_status=deepcopy(row["step_status"]),
            attempts=row["attempts"],
            last_error=row["last_error"],
            next_attempt_at=row["next_attempt_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# =============================================================================
# Reputation
# =============================================================================


class FakeReputationRepository(IReputationRepository):
    """In-memory ``reputation_events`` store.

    Idempotent on the ``(agent_id, task_id, kind)`` triple — repeat
    ``record`` returns the existing row, matching the PG impl's
    ``ON CONFLICT DO NOTHING ... RETURNING`` semantic.

    Production reads default ``include_smoke_test=False`` so smoke
    rows do not pollute counts. This fake enforces the same
    contract.
    """

    def __init__(self) -> None:
        self._rows: list[ReputationEvent] = []
        self._next_id = 1

    async def record(
        self,
        event: ReputationEvent,
        *,
        session: AsyncSession | None = None,
    ) -> ReputationEvent:
        existing = self._find(event.agent_id, event.task_id, event.kind)
        if existing is not None:
            return existing
        stored = event.model_copy(
            update={
                "id": self._next_id,
                "created_at": datetime.now(UTC),
            }
        )
        self._next_id += 1
        self._rows.append(stored)
        return stored

    async def list_for_agent(
        self,
        agent_id: str,
        *,
        kind: str | None = None,
        include_smoke_test: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ReputationEvent]:
        rows = [r for r in self._rows if r.agent_id == agent_id]
        if kind is not None:
            rows = [r for r in rows if r.kind == kind]
        if not include_smoke_test:
            rows = [r for r in rows if not self._is_smoke(r)]
        rows = sorted(
            rows,
            key=lambda r: r.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return rows[offset : offset + limit]

    async def count_for_agent(
        self,
        agent_id: str,
        *,
        kind: str | None = None,
        include_smoke_test: bool = False,
    ) -> int:
        rows = [r for r in self._rows if r.agent_id == agent_id]
        if kind is not None:
            rows = [r for r in rows if r.kind == kind]
        if not include_smoke_test:
            rows = [r for r in rows if not self._is_smoke(r)]
        return len(rows)

    async def count_kind_since(
        self,
        kind: str,
        since: datetime,
        *,
        include_smoke_test: bool = False,
    ) -> int:
        n = 0
        for row in self._rows:
            if row.kind != kind:
                continue
            if row.created_at is None or row.created_at < since:
                continue
            if not include_smoke_test and self._is_smoke(row):
                continue
            n += 1
        return n

    async def list_for_task(
        self,
        task_id: str,
        *,
        include_smoke_test: bool = True,
    ) -> list[ReputationEvent]:
        rows = [r for r in self._rows if r.task_id == task_id]
        if not include_smoke_test:
            rows = [r for r in rows if not self._is_smoke(r)]
        return sorted(
            rows,
            key=lambda r: r.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )

    # ---------------------------------------------------------------------
    # Internals
    # ---------------------------------------------------------------------

    def _find(self, agent_id: str, task_id: str, kind: str) -> ReputationEvent | None:
        for row in self._rows:
            if row.agent_id == agent_id and row.task_id == task_id and row.kind == kind:
                return row
        return None

    @staticmethod
    def _is_smoke(row: ReputationEvent) -> bool:
        return bool(row.event_metadata.get("smoke_test"))
