"""End-to-end saga orchestration tests.

These tests ride ``FakeSettlementOutboxRepository`` +
``FakeReputationRepository`` so saga state machine transitions are
exercised under realistic semantics without needing a real
PostgreSQL. The PG implementations are tested separately under
``tests/integration/`` against a live database.

The 5 cases here cover plan §6.1 single-process semantics:

1. Transient escrow failure → retry → succeed → mark_done
2. Permanent failure → mark_dead after ``max_attempts`` + DLQ fired
3. Duplicate enqueue → UNIQUE silently dropped, no double payment
4. Worker mid-saga crash → resumes from ``step_status`` after restart
5. Smoke task → reputation row stamped + filtered from default reads

(Two more cases — multi-replica ``SKIP LOCKED`` and same-transaction
atomicity — live in ``tests/integration/test_settlement_outbox_pg.py``
because they need a real PG to be meaningful.)

Worker driving model: tests call ``worker._process_event`` /
``claim_batch`` directly instead of starting the poll loop, so
time / backoff / replica behaviour is deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from acn.core.interfaces.escrow_provider import (
    EscrowDetailResult,
    IEscrowProvider,
    ReleaseResult,
)
from acn.core.interfaces.reputation_repository import (
    REPUTATION_KIND_FEEDBACK,
)
from acn.core.interfaces.settlement_outbox_repository import SettlementEvent
from acn.services.reputation_service import ReputationService
from acn.services.settlement_worker import SettlementWorker

from ._settlement_fakes import (
    FakeReputationRepository,
    FakeSettlementOutboxRepository,
)

# =============================================================================
# Fixtures
# =============================================================================


_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _event(
    *,
    event_id: str = "evt-1",
    task_id: str = "task-1",
    metadata: dict[str, Any] | None = None,
    step_status: dict[str, str] | None = None,
) -> SettlementEvent:
    """Producer-shaped event: use_escrow=True, single-participant,
    100 ap_points reward, assignee + creator both present. This is
    the mainline production saga input.
    """
    return SettlementEvent(
        event_id=event_id,
        task_id=task_id,
        trigger="review_pass",
        payload={
            "task_id": task_id,
            "creator_id": "user-creator",
            "assignee_id": "agent-worker",
            "payment_task_id": None,
            "reward": "100",
            "reward_currency": "ap_points",
            "task_title": "title",
            "approver_id": "user-creator",
            "review_notes": None,
            "use_escrow": True,
            "is_multi": False,
            "metadata": metadata if metadata is not None else {},
        },
        step_status=step_status
        or {
            "escrow_release": "pending",
            "reward_distribute": "pending",
            "reputation_write": "pending",
        },
    )


def _happy_escrow_mock() -> AsyncMock:
    """An escrow mock that returns ``submitted -> released`` for
    the standard single-participant flow."""
    escrow = AsyncMock(spec=IEscrowProvider)
    escrow.get_by_task.return_value = EscrowDetailResult(
        success=True, escrow_id="esc-1", status="submitted"
    )
    escrow.release.return_value = ReleaseResult(
        success=True, agent_amount=85.0, acn_amount=3.0, provider_amount=12.0
    )
    return escrow


class _ManualClock:
    """Monotonic clock the test drives manually so backoff windows
    are deterministic. ``advance(seconds)`` jumps the clock forward
    by the supplied amount."""

    def __init__(self, t0: datetime = _T0) -> None:
        self.now = t0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _build_worker(
    *,
    outbox: FakeSettlementOutboxRepository,
    escrow: IEscrowProvider | None,
    reputation: ReputationService | None,
    clock: _ManualClock,
    max_attempts: int = 12,
    dlq_alert_webhook: str | None = None,
) -> SettlementWorker:
    return SettlementWorker(
        outbox=outbox,
        escrow_provider=escrow,
        reputation_service=reputation,
        poll_interval_sec=0.0,
        batch_size=10,
        max_attempts=max_attempts,
        backoff_base_sec=1.0,
        backoff_max_sec=60.0,
        janitor_interval_sec=60.0,
        janitor_stuck_threshold_sec=300.0,
        dlq_alert_webhook=dlq_alert_webhook,
        clock=clock,
    )


# =============================================================================
# 1. Transient escrow failure → retry → succeed
# =============================================================================


@pytest.mark.asyncio
async def test_transient_escrow_failure_retries_to_success() -> None:
    """``escrow.get_by_task`` fails once (HTTP 504 simulated), then
    succeeds on retry. The saga must:
      - mark_retry after the first attempt, with backoff scheduled
      - re-claim the row once the backoff window passes
      - complete via mark_done without double-paying
    """
    outbox = FakeSettlementOutboxRepository()
    escrow = AsyncMock(spec=IEscrowProvider)
    # First call to get_by_task: backend 504. Second call: success.
    escrow.get_by_task.side_effect = [
        EscrowDetailResult(success=False, error="HTTP 504 gateway timeout"),
        EscrowDetailResult(success=True, escrow_id="esc-1", status="submitted"),
    ]
    escrow.release.return_value = ReleaseResult(success=True)

    reputation_repo = FakeReputationRepository()
    reputation = ReputationService(reputation_repo)
    clock = _ManualClock()
    worker = _build_worker(outbox=outbox, escrow=escrow, reputation=reputation, clock=clock)

    event = _event()
    assert await outbox.enqueue(event) is True

    # First claim → transient failure.
    batch = await outbox.claim_batch(limit=10, now=clock())
    assert len(batch) == 1
    await worker._process_event(batch[0])

    row = outbox.get_row(event.event_id)
    assert row["state"] == "retrying", "transient failure must schedule a retry"
    assert row["attempts"] == 1
    assert "504" in (row["last_error"] or "")
    # release must NOT have been called — only the read leg fired.
    escrow.release.assert_not_awaited()

    # Advance past the backoff window.
    backoff = (row["next_attempt_at"] - clock()).total_seconds()
    clock.advance(backoff + 1)

    # Second claim → succeeds.
    batch = await outbox.claim_batch(limit=10, now=clock())
    assert len(batch) == 1
    await worker._process_event(batch[0])

    row = outbox.get_row(event.event_id)
    assert row["state"] == "done"
    assert row["step_status"] == {
        "escrow_release": "done",
        "reward_distribute": "done",
        "reputation_write": "done",
    }
    # release fired exactly once across the two attempts.
    escrow.release.assert_awaited_once()
    # Reputation also persisted exactly once.
    assert await reputation_repo.count_for_agent("agent-worker") == 1


# =============================================================================
# 2. Permanent failure → mark_dead + DLQ webhook fired
# =============================================================================


@pytest.mark.asyncio
async def test_permanent_failure_marks_dead_after_max_attempts() -> None:
    """Every get_by_task returns 500. After ``max_attempts`` rounds
    the worker marks the row dead and POSTs the DLQ webhook with
    the failure context."""
    outbox = FakeSettlementOutboxRepository()
    escrow = AsyncMock(spec=IEscrowProvider)
    escrow.get_by_task.return_value = EscrowDetailResult(
        success=False, error="HTTP 500 internal server error"
    )

    captured_payloads: list[dict[str, Any]] = []

    def _record_post(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(__import__("json").loads(request.content))
        return httpx.Response(204)

    # Patch the worker's lazy httpx client with a MockTransport so
    # the DLQ webhook never leaves the test process.
    transport = httpx.MockTransport(_record_post)
    clock = _ManualClock()
    worker = _build_worker(
        outbox=outbox,
        escrow=escrow,
        reputation=None,
        clock=clock,
        max_attempts=3,
        dlq_alert_webhook="https://dlq.example/alert",
    )
    # Force the worker to use our mock transport: pre-create the
    # client with the transport hooked in. The worker uses
    # ``self._dlq_http_client`` if already non-None, so this is the
    # documented injection point.
    worker._dlq_http_client = httpx.AsyncClient(transport=transport, timeout=5.0)

    event = _event()
    await outbox.enqueue(event)

    # Drive 3 attempts (max_attempts=3 means the 3rd attempt is dead).
    for i in range(3):
        batch = await outbox.claim_batch(limit=10, now=clock())
        assert len(batch) == 1, f"attempt {i} did not re-claim"
        await worker._process_event(batch[0])
        row = outbox.get_row(event.event_id)
        if row["state"] == "dead":
            break
        # Skip past the scheduled backoff window for the next attempt.
        clock.advance((row["next_attempt_at"] - clock()).total_seconds() + 1)

    row = outbox.get_row(event.event_id)
    assert row["state"] == "dead"
    assert row["attempts"] == 3
    assert "500" in (row["last_error"] or "")
    # DLQ webhook fired exactly once on mark_dead.
    assert len(captured_payloads) == 1, "DLQ webhook should fire once on dead"
    body = captured_payloads[0]
    assert body["event_id"] == event.event_id
    assert body["task_id"] == event.task_id
    assert "500" in body["last_error"]


# =============================================================================
# 3. Duplicate enqueue → UNIQUE silent skip, no double payment
# =============================================================================


@pytest.mark.asyncio
async def test_duplicate_enqueue_does_not_double_pay() -> None:
    """A retried producer call (e.g. network timeout caused ACN to
    retry ``complete_task``) tries to enqueue the same event_id
    again. The second enqueue returns False, the worker still only
    processes ONE row, and escrow.release fires exactly once.
    """
    outbox = FakeSettlementOutboxRepository()
    escrow = _happy_escrow_mock()
    reputation_repo = FakeReputationRepository()
    reputation = ReputationService(reputation_repo)
    clock = _ManualClock()
    worker = _build_worker(outbox=outbox, escrow=escrow, reputation=reputation, clock=clock)

    event = _event()
    assert await outbox.enqueue(event) is True
    # Second enqueue with same event_id → silent skip.
    assert await outbox.enqueue(event) is False

    batch = await outbox.claim_batch(limit=10, now=clock())
    assert len(batch) == 1, "UNIQUE constraint must drop the duplicate row"
    await worker._process_event(batch[0])

    escrow.release.assert_awaited_once()
    assert await reputation_repo.count_for_agent("agent-worker") == 1


# =============================================================================
# 4. Worker mid-saga crash → resume from step_status on restart
# =============================================================================


@pytest.mark.asyncio
async def test_worker_resume_skips_already_done_steps() -> None:
    """Simulate the worker dying after escrow_release succeeded but
    before reputation_write could run. On the next claim cycle:
      - step_status['escrow_release'] is 'done' → worker skips it
        without calling escrow.release a second time
      - reputation_write step still runs
    This is the heart of the saga-resume contract.
    """
    outbox = FakeSettlementOutboxRepository()
    escrow = _happy_escrow_mock()
    reputation_repo = FakeReputationRepository()
    reputation = ReputationService(reputation_repo)
    clock = _ManualClock()
    worker = _build_worker(outbox=outbox, escrow=escrow, reputation=reputation, clock=clock)

    # Producer enqueues with the FIRST step already marked done —
    # this simulates "worker crashed AFTER update_step_status flipped
    # escrow_release to done but BEFORE mark_done on the row".
    event = _event(
        step_status={
            "escrow_release": "done",
            "reward_distribute": "pending",
            "reputation_write": "pending",
        }
    )
    await outbox.enqueue(event)

    batch = await outbox.claim_batch(limit=10, now=clock())
    await worker._process_event(batch[0])

    # No second release call — the worker honored the saved
    # step_status.
    escrow.release.assert_not_awaited()
    escrow.get_by_task.assert_not_awaited()
    # Reputation did run.
    assert await reputation_repo.count_for_agent("agent-worker") == 1
    row = outbox.get_row(event.event_id)
    assert row["state"] == "done"


# =============================================================================
# 5. Smoke task → reputation stamped + filtered from default reads
# =============================================================================


@pytest.mark.asyncio
async def test_smoke_task_reputation_is_isolated_from_production_reads() -> None:
    """The producer set ``smoke_test=True`` on ``task.metadata``;
    the worker must propagate it via ``task_metadata`` so the
    reputation row carries ``event_metadata['smoke_test']=True``.
    Default read paths (``include_smoke_test=False``) must NOT
    return that row.
    """
    outbox = FakeSettlementOutboxRepository()
    escrow = _happy_escrow_mock()
    reputation_repo = FakeReputationRepository()
    reputation = ReputationService(reputation_repo)
    clock = _ManualClock()
    worker = _build_worker(outbox=outbox, escrow=escrow, reputation=reputation, clock=clock)

    event = _event(metadata={"smoke_test": True, "extra": "ignored"})
    await outbox.enqueue(event)

    batch = await outbox.claim_batch(limit=10, now=clock())
    await worker._process_event(batch[0])

    # Stamped: list_for_task (which defaults to include_smoke_test=
    # True for forensic tooling) returns the row, and it carries
    # the smoke flag.
    forensic_rows = await reputation_repo.list_for_task(event.task_id, include_smoke_test=True)
    assert len(forensic_rows) == 1
    assert forensic_rows[0].event_metadata.get("smoke_test") is True
    assert forensic_rows[0].kind == REPUTATION_KIND_FEEDBACK

    # Production reads filter it out.
    assert await reputation_repo.count_for_agent("agent-worker") == 0
    production_rows = await reputation_repo.list_for_agent("agent-worker")
    assert production_rows == []

    # Ops can opt in with include_smoke_test=True.
    assert (await reputation_repo.count_for_agent("agent-worker", include_smoke_test=True)) == 1
