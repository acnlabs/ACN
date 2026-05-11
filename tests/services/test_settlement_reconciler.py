"""Unit tests for ``SettlementReconciler`` (Todo 9c).

These tests use the in-memory fakes from ``_settlement_fakes.py`` plus
a recording metrics collector so we can assert:

- The reconciler computes the correct ``saga_done - feedback`` delta
  for a trailing window.
- Smoke-test reputation rows are excluded by default (mirroring the
  production query contract).
- The metric is emitted even when the delta is zero (so dashboards
  can prove the job is alive).
- Metrics failures never propagate (a Redis blip cannot crash cron).
- The window boundary is exclusive on the lower edge — rows older
  than ``now - window_seconds`` are filtered out.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from acn.core.interfaces.reputation_repository import (
    REPUTATION_KIND_FEEDBACK,
    REPUTATION_KIND_VALIDATION,
    ReputationEvent,
)
from acn.core.interfaces.settlement_outbox_repository import SettlementEvent
from acn.services.settlement_reconciler import (
    ReconcileResult,
    SettlementReconciler,
)
from tests.services._settlement_fakes import (
    FakeReputationRepository,
    FakeSettlementOutboxRepository,
)

# =============================================================================
# Test doubles
# =============================================================================


class _RecordingMetrics:
    """Minimal stand-in for ``MetricsCollector``. Only the calls
    ``SettlementReconciler`` exercises are implemented.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.gauges: list[tuple[str, float, dict[str, str] | None]] = []
        self._fail = fail

    async def set_gauge(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
    ) -> None:
        if self._fail:
            raise RuntimeError("redis down")
        self.gauges.append((name, value, labels))


class _ManualClock:
    """Deterministic clock. The reconciler computes
    ``now`` exactly once per ``run_once`` so this is enough.
    """

    def __init__(self, t: datetime) -> None:
        self._t = t

    def __call__(self) -> datetime:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t = self._t + timedelta(seconds=seconds)


# =============================================================================
# Helpers
# =============================================================================


def _make_done_row(
    outbox: FakeSettlementOutboxRepository,
    *,
    event_id: str,
    task_id: str,
    trigger: str,
    updated_at: datetime,
) -> None:
    """Inject a ``state='done'`` row at a fixed ``updated_at``.

    The fakes don't expose a public setter for ``state``/``updated_at``
    (the production path always goes through ``claim_batch``+
    ``mark_done``), so we bypass via the internal ``_rows`` dict.
    Tests own this coupling intentionally; we're testing the
    reconciler's window/filter logic, not the state machine.
    """
    payload: dict[str, Any] = {}
    outbox._rows[event_id] = {  # noqa: SLF001 — test helper
        "event_id": event_id,
        "task_id": task_id,
        "trigger": trigger,
        "payload": payload,
        "state": "done",
        "step_status": {
            "escrow_release": "skipped",
            "reward_distribute": "skipped",
            "reputation_write": "done",
        },
        "attempts": 1,
        "last_error": None,
        "next_attempt_at": None,
        "created_at": updated_at,
        "updated_at": updated_at,
        "_paying_since": None,
    }


async def _make_feedback(
    repo: FakeReputationRepository,
    *,
    agent_id: str,
    task_id: str,
    created_at: datetime,
    smoke_test: bool = False,
) -> None:
    """Insert a feedback row, then back-date its ``created_at`` so
    window tests can place rows on either side of the boundary.

    Why back-date instead of injecting via ``_rows``: the fake's
    ``record`` produces a real ``ReputationEvent`` with the
    idempotency contract enforced; we want that fidelity in tests.
    """
    event = ReputationEvent(
        agent_id=agent_id,
        task_id=task_id,
        kind=REPUTATION_KIND_FEEDBACK,
        signer=f"signer:{agent_id}",
        event_metadata={"smoke_test": True} if smoke_test else {},
    )
    stored = await repo.record(event)
    # Replace with a back-dated copy. The fake stores ReputationEvent
    # objects in a list, so we patch in place.
    idx = repo._rows.index(stored)  # noqa: SLF001 — test helper
    repo._rows[idx] = stored.model_copy(update={"created_at": created_at})  # noqa: SLF001


# =============================================================================
# Tests
# =============================================================================


@pytest.mark.asyncio
async def test_zero_delta_emits_gauge_and_logs_info() -> None:
    """The happy path: 3 saga done rows and 3 feedback rows, all
    inside the window. Delta should be 0 and the gauge must still
    be emitted so dashboards can distinguish "delta=0" from "job
    not running".
    """
    outbox = FakeSettlementOutboxRepository()
    rep = FakeReputationRepository()
    metrics = _RecordingMetrics()
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    clock = _ManualClock(now)

    for i in range(3):
        _make_done_row(
            outbox,
            event_id=f"ev-{i}",
            task_id=f"task-{i}",
            trigger="review_pass",
            updated_at=now - timedelta(hours=1),
        )
        await _make_feedback(
            rep,
            agent_id=f"agent-{i}",
            task_id=f"task-{i}",
            created_at=now - timedelta(hours=1),
        )

    reconciler = SettlementReconciler(
        outbox=outbox,
        reputation=rep,
        metrics_collector=metrics,
        clock=clock,
    )

    result = await reconciler.run_once(window_seconds=86_400)

    assert isinstance(result, ReconcileResult)
    assert result.saga_done_count == 3
    assert result.feedback_count == 3
    assert result.delta == 0
    assert metrics.gauges == [("settlement_reconcile_delta", 0.0, None)]


@pytest.mark.asyncio
async def test_positive_delta_when_reputation_is_missing() -> None:
    """3 saga rows reached done but only 2 feedback rows landed —
    matches the failure mode Todo 9c is designed to detect.
    """
    outbox = FakeSettlementOutboxRepository()
    rep = FakeReputationRepository()
    metrics = _RecordingMetrics()
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)

    for i in range(3):
        _make_done_row(
            outbox,
            event_id=f"ev-{i}",
            task_id=f"task-{i}",
            trigger="review_pass",
            updated_at=now - timedelta(hours=2),
        )
    for i in range(2):
        await _make_feedback(
            rep,
            agent_id=f"agent-{i}",
            task_id=f"task-{i}",
            created_at=now - timedelta(hours=2),
        )

    reconciler = SettlementReconciler(
        outbox=outbox,
        reputation=rep,
        metrics_collector=metrics,
        clock=_ManualClock(now),
    )

    result = await reconciler.run_once(window_seconds=86_400)
    assert result.saga_done_count == 3
    assert result.feedback_count == 2
    assert result.delta == 1
    assert metrics.gauges == [("settlement_reconcile_delta", 1.0, None)]


@pytest.mark.asyncio
async def test_window_excludes_rows_older_than_since() -> None:
    """Rows older than ``now - window`` are filtered out — they
    belong to a previous window's report.
    """
    outbox = FakeSettlementOutboxRepository()
    rep = FakeReputationRepository()
    metrics = _RecordingMetrics()
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    window = 3600  # one-hour window for sharp boundary testing

    # Inside window: 1h boundary - 5min = inside (since=now-1h, this row
    # at now-55min is after since).
    _make_done_row(
        outbox,
        event_id="inside",
        task_id="task-inside",
        trigger="review_pass",
        updated_at=now - timedelta(minutes=55),
    )
    await _make_feedback(
        rep,
        agent_id="agent-inside",
        task_id="task-inside",
        created_at=now - timedelta(minutes=55),
    )

    # Outside window: at now-2h, before since=now-1h.
    _make_done_row(
        outbox,
        event_id="outside",
        task_id="task-outside",
        trigger="review_pass",
        updated_at=now - timedelta(hours=2),
    )
    await _make_feedback(
        rep,
        agent_id="agent-outside",
        task_id="task-outside",
        created_at=now - timedelta(hours=2),
    )

    reconciler = SettlementReconciler(
        outbox=outbox,
        reputation=rep,
        metrics_collector=metrics,
        clock=_ManualClock(now),
    )

    result = await reconciler.run_once(window_seconds=window)
    assert result.saga_done_count == 1
    assert result.feedback_count == 1
    assert result.delta == 0


@pytest.mark.asyncio
async def test_trigger_filter_excludes_non_review_pass() -> None:
    """The reconciler asks the outbox for ``trigger='review_pass'``
    only. A hypothetical future trigger (e.g. ``'dispute_refund'``)
    would not be counted on the saga side, so it must not be
    counted on the reputation side either.
    """
    outbox = FakeSettlementOutboxRepository()
    rep = FakeReputationRepository()
    metrics = _RecordingMetrics()
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)

    _make_done_row(
        outbox,
        event_id="review",
        task_id="task-review",
        trigger="review_pass",
        updated_at=now - timedelta(minutes=10),
    )
    _make_done_row(
        outbox,
        event_id="refund",
        task_id="task-refund",
        trigger="dispute_refund",
        updated_at=now - timedelta(minutes=10),
    )
    await _make_feedback(
        rep,
        agent_id="agent-review",
        task_id="task-review",
        created_at=now - timedelta(minutes=10),
    )

    reconciler = SettlementReconciler(
        outbox=outbox,
        reputation=rep,
        metrics_collector=metrics,
        clock=_ManualClock(now),
    )

    result = await reconciler.run_once(window_seconds=86_400)
    assert result.saga_done_count == 1  # only review_pass counted
    assert result.feedback_count == 1
    assert result.delta == 0


@pytest.mark.asyncio
async def test_smoke_test_feedback_excluded() -> None:
    """Smoke rows must not pollute the production delta — same
    contract as ``ReputationQueryService``.
    """
    outbox = FakeSettlementOutboxRepository()
    rep = FakeReputationRepository()
    metrics = _RecordingMetrics()
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)

    # 1 prod saga done + 1 prod feedback (delta=0 expected).
    _make_done_row(
        outbox,
        event_id="prod",
        task_id="task-prod",
        trigger="review_pass",
        updated_at=now - timedelta(minutes=10),
    )
    await _make_feedback(
        rep,
        agent_id="agent-prod",
        task_id="task-prod",
        created_at=now - timedelta(minutes=10),
    )
    # Plus 1 smoke feedback (should NOT count, otherwise delta=-1).
    await _make_feedback(
        rep,
        agent_id="agent-smoke",
        task_id="task-smoke",
        created_at=now - timedelta(minutes=10),
        smoke_test=True,
    )

    reconciler = SettlementReconciler(
        outbox=outbox,
        reputation=rep,
        metrics_collector=metrics,
        clock=_ManualClock(now),
    )
    result = await reconciler.run_once(window_seconds=86_400)
    assert result.saga_done_count == 1
    assert result.feedback_count == 1
    assert result.delta == 0


@pytest.mark.asyncio
async def test_validation_kind_does_not_pollute_feedback_count() -> None:
    """Validation rows are a different ``kind`` and live alongside
    feedback in ``reputation_events``. The reconciler must only
    count feedback so future validation workflows don't inflate
    the count.
    """
    outbox = FakeSettlementOutboxRepository()
    rep = FakeReputationRepository()
    metrics = _RecordingMetrics()
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)

    _make_done_row(
        outbox,
        event_id="ev",
        task_id="task-1",
        trigger="review_pass",
        updated_at=now - timedelta(minutes=10),
    )
    await _make_feedback(
        rep,
        agent_id="agent-1",
        task_id="task-1",
        created_at=now - timedelta(minutes=10),
    )
    # Insert a validation row — must NOT count.
    validation = ReputationEvent(
        agent_id="agent-1",
        task_id="task-1",
        kind=REPUTATION_KIND_VALIDATION,
        signer="validator",
        attestation={"verdict": "ok"},
    )
    stored = await rep.record(validation)
    idx = rep._rows.index(stored)  # noqa: SLF001
    rep._rows[idx] = stored.model_copy(  # noqa: SLF001
        update={"created_at": now - timedelta(minutes=10)}
    )

    reconciler = SettlementReconciler(
        outbox=outbox,
        reputation=rep,
        metrics_collector=metrics,
        clock=_ManualClock(now),
    )
    result = await reconciler.run_once(window_seconds=86_400)
    assert result.feedback_count == 1
    assert result.delta == 0


@pytest.mark.asyncio
async def test_metrics_failure_does_not_crash_job() -> None:
    """A Redis blip during ``set_gauge`` must not propagate — the
    operator's "job alive" signal would die with it. Reconciler
    must still return the result so cron can keep going.
    """
    outbox = FakeSettlementOutboxRepository()
    rep = FakeReputationRepository()
    metrics = _RecordingMetrics(fail=True)
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)

    _make_done_row(
        outbox,
        event_id="ev",
        task_id="task-1",
        trigger="review_pass",
        updated_at=now - timedelta(minutes=10),
    )
    await _make_feedback(
        rep,
        agent_id="agent-1",
        task_id="task-1",
        created_at=now - timedelta(minutes=10),
    )

    reconciler = SettlementReconciler(
        outbox=outbox,
        reputation=rep,
        metrics_collector=metrics,
        clock=_ManualClock(now),
    )
    # No exception even though metrics raises.
    result = await reconciler.run_once(window_seconds=86_400)
    assert result.delta == 0


@pytest.mark.asyncio
async def test_no_metrics_collector_runs_silently() -> None:
    """``metrics_collector=None`` is a valid wiring for tests and
    minimal deployments. The reconciler should compute and return
    the result without trying to publish anywhere.
    """
    outbox = FakeSettlementOutboxRepository()
    rep = FakeReputationRepository()
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)

    _make_done_row(
        outbox,
        event_id="ev",
        task_id="task-1",
        trigger="review_pass",
        updated_at=now - timedelta(minutes=10),
    )
    reconciler = SettlementReconciler(
        outbox=outbox,
        reputation=rep,
        metrics_collector=None,
        clock=_ManualClock(now),
    )
    result = await reconciler.run_once(window_seconds=86_400)
    assert result.saga_done_count == 1
    assert result.feedback_count == 0
    assert result.delta == 1


@pytest.mark.asyncio
async def test_run_with_retry_recovers_from_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single transient ``run_once`` failure (PG blip) must not
    burn a 24h reconciliation window — ``run_with_retry`` retries
    inside the same scheduler tick.
    """
    outbox = FakeSettlementOutboxRepository()
    rep = FakeReputationRepository()
    metrics = _RecordingMetrics()
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    _make_done_row(
        outbox,
        event_id="ev",
        task_id="task-1",
        trigger="review_pass",
        updated_at=now - timedelta(minutes=10),
    )
    await _make_feedback(
        rep,
        agent_id="agent-1",
        task_id="task-1",
        created_at=now - timedelta(minutes=10),
    )
    reconciler = SettlementReconciler(
        outbox=outbox,
        reputation=rep,
        metrics_collector=metrics,
        clock=_ManualClock(now),
    )

    # First call raises, second succeeds.
    calls = {"n": 0}
    real_run_once = reconciler.run_once

    async def flaky_run_once(*, window_seconds: int) -> ReconcileResult:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("pg blip")
        return await real_run_once(window_seconds=window_seconds)

    monkeypatch.setattr(reconciler, "run_once", flaky_run_once)

    # Zero backoff to keep the test fast.
    result = await reconciler.run_with_retry(
        window_seconds=86_400, max_retries=3, retry_backoff_sec=0.0
    )

    assert result is not None
    assert result.delta == 0
    assert calls["n"] == 2  # one fail + one success


@pytest.mark.asyncio
async def test_run_with_retry_returns_none_after_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If every retry fails, ``run_with_retry`` returns None rather
    than raising — the scheduler should wait for the next tick.
    """
    outbox = FakeSettlementOutboxRepository()
    rep = FakeReputationRepository()
    reconciler = SettlementReconciler(
        outbox=outbox,
        reputation=rep,
        metrics_collector=None,
        clock=_ManualClock(datetime(2026, 5, 5, tzinfo=UTC)),
    )

    calls = {"n": 0}

    async def always_fail(*, window_seconds: int) -> ReconcileResult:
        calls["n"] += 1
        raise RuntimeError("pg dead")

    monkeypatch.setattr(reconciler, "run_once", always_fail)

    result = await reconciler.run_with_retry(
        window_seconds=86_400, max_retries=3, retry_backoff_sec=0.0
    )

    assert result is None
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_pending_outbox_rows_are_not_counted() -> None:
    """Only ``state='done'`` rows count. The reconciler must not
    inflate by counting in-flight ``pending``/``paying``/``retrying``
    events — otherwise the delta would be spuriously positive
    every time the worker is mid-batch.
    """
    outbox = FakeSettlementOutboxRepository()
    rep = FakeReputationRepository()
    metrics = _RecordingMetrics()
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)

    # One done row.
    _make_done_row(
        outbox,
        event_id="done",
        task_id="task-done",
        trigger="review_pass",
        updated_at=now - timedelta(minutes=5),
    )
    await _make_feedback(
        rep,
        agent_id="agent-done",
        task_id="task-done",
        created_at=now - timedelta(minutes=5),
    )
    # One pending row (newly enqueued, hasn't been processed).
    await outbox.enqueue(
        SettlementEvent(
            event_id="pending",
            task_id="task-pending",
            trigger="review_pass",
            payload={},
        )
    )

    reconciler = SettlementReconciler(
        outbox=outbox,
        reputation=rep,
        metrics_collector=metrics,
        clock=_ManualClock(now),
    )
    result = await reconciler.run_once(window_seconds=86_400)
    assert result.saga_done_count == 1  # pending excluded
    assert result.delta == 0
