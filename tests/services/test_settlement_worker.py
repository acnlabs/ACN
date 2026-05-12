"""Step-handler tests for ``SettlementWorker`` (Saga v0.1, Todo 6).

Scope:

1. ``_step_escrow_release`` — read-before-write idempotency, multi vs
   single dispatch, state-transition driving, error envelope -> retry
   raise.
2. ``_step_reward_distribute`` — currently a logged no-op; smoke that
   it never raises and never touches the escrow client.
3. ``_step_reputation_write`` — happy path, smoke-flag propagation
   through ``payload['metadata']``, and missing-collaborator failure.
4. ``_execute_step`` dispatcher — unknown step name raises.

What these tests deliberately do NOT cover:

* The outer poll / janitor loops, ``claim_batch`` / ``mark_done`` /
  ``mark_retry`` plumbing — those are Todo 4 territory and live in
  the integration suite (Todo 8).
* Real ``escrow_client`` / ``reputation_service`` behaviour — those
  have their own focused suites (``test_reputation_service.py``,
  Backend-side escrow tests). The worker contract is what matters
  here, so we mock the collaborators with strict signature checks.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.interfaces.escrow_provider import (
    EscrowDetailResult,
    IEscrowProvider,
    ReleaseResult,
)
from acn.core.interfaces.settlement_outbox_repository import (
    ISettlementOutboxRepository,
    SettlementEvent,
)
from acn.services.reputation_service import ReputationService
from acn.services.settlement_worker import (
    SettlementWorker,
    StepHandlerError,
)

# =============================================================================
# Fixtures
# =============================================================================


def _build_event(
    *,
    task_id: str = "task-1",
    creator_id: str | None = "user-creator",
    assignee_id: str | None = "agent-worker",
    reward: str = "100",
    is_multi: bool = False,
    metadata: dict[str, Any] | None = None,
    step_status: dict[str, str] | None = None,
) -> SettlementEvent:
    """Build a SettlementEvent that looks like what the producer
    side commits — i.e. ``payload`` includes the snapshot fields
    Todo 6 added (``use_escrow`` / ``is_multi`` / ``metadata``).
    """
    payload: dict[str, Any] = {
        "task_id": task_id,
        "creator_id": creator_id,
        "assignee_id": assignee_id,
        "payment_task_id": None,
        "reward": reward,
        "reward_currency": "credits",
        "task_title": "test",
        "approver_id": creator_id,
        "review_notes": None,
        "use_escrow": True,
        "is_multi": is_multi,
        "metadata": metadata if metadata is not None else {},
    }
    return SettlementEvent(
        event_id=f"evt-{task_id}",
        task_id=task_id,
        trigger="review_pass",
        payload=payload,
        step_status=step_status
        or {
            "escrow_release": "pending",
            "reward_distribute": "pending",
            "reputation_write": "pending",
        },
    )


def _build_worker(
    *,
    escrow: IEscrowProvider | None = None,
    reputation: ReputationService | None = None,
) -> SettlementWorker:
    """Construct a worker bound to a no-op outbox mock.

    The step handlers do not touch the outbox directly — that's
    ``_process_event`` / ``_handle_step_failure`` territory — so a
    plain MagicMock is enough to satisfy the constructor's type
    contract.
    """
    outbox = MagicMock(spec=ISettlementOutboxRepository)
    return SettlementWorker(
        outbox=outbox,
        escrow_provider=escrow,
        reputation_service=reputation,
    )


# =============================================================================
# _step_escrow_release
# =============================================================================


@pytest.mark.asyncio
async def test_escrow_release_single_locked_drives_submit_and_release() -> None:
    """Single-participant escrow in ``locked`` status:
    submit_v2 then release. Both succeed -> handler returns.
    """
    escrow = AsyncMock(spec=IEscrowProvider)
    escrow.get_by_task.return_value = EscrowDetailResult(
        success=True, escrow_id="esc-1", status="locked"
    )
    escrow.submit_v2.return_value = EscrowDetailResult(
        success=True, escrow_id="esc-1", status="submitted"
    )
    escrow.release.return_value = ReleaseResult(
        success=True, agent_amount=85.0, acn_amount=3.0, provider_amount=12.0
    )

    worker = _build_worker(escrow=escrow)
    await worker._step_escrow_release(_build_event(is_multi=False))

    escrow.submit_v2.assert_awaited_once_with("esc-1")
    escrow.release.assert_awaited_once()
    call = escrow.release.await_args
    assert call.kwargs["creator_user_id"] == "user-creator"
    assert call.kwargs["agent_owner_user_id"] == "agent-worker"
    assert call.kwargs["amount"] == 100.0
    escrow.accept_v2.assert_not_awaited()
    escrow.release_partial.assert_not_awaited()


@pytest.mark.asyncio
async def test_escrow_release_single_in_progress_skips_submit() -> None:
    """``in_progress`` also requires submit_v2 (Backend v1 release
    rejects rows that haven't been submitted)."""
    escrow = AsyncMock(spec=IEscrowProvider)
    escrow.get_by_task.return_value = EscrowDetailResult(
        success=True, escrow_id="esc-1", status="in_progress"
    )
    escrow.submit_v2.return_value = EscrowDetailResult(
        success=True, escrow_id="esc-1", status="submitted"
    )
    escrow.release.return_value = ReleaseResult(success=True)

    worker = _build_worker(escrow=escrow)
    await worker._step_escrow_release(_build_event(is_multi=False))

    escrow.submit_v2.assert_awaited_once_with("esc-1")
    escrow.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_escrow_release_single_submitted_skips_submit() -> None:
    """Already-submitted escrow goes straight to ``release``."""
    escrow = AsyncMock(spec=IEscrowProvider)
    escrow.get_by_task.return_value = EscrowDetailResult(
        success=True, escrow_id="esc-1", status="submitted"
    )
    escrow.release.return_value = ReleaseResult(success=True)

    worker = _build_worker(escrow=escrow)
    await worker._step_escrow_release(_build_event(is_multi=False))

    escrow.submit_v2.assert_not_awaited()
    escrow.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_escrow_release_multi_locked_drives_accept_then_release_partial() -> None:
    """Multi-participant: locked -> accept_v2 -> release_partial."""
    escrow = AsyncMock(spec=IEscrowProvider)
    escrow.get_by_task.return_value = EscrowDetailResult(
        success=True, escrow_id="esc-1", status="locked"
    )
    escrow.accept_v2.return_value = EscrowDetailResult(
        success=True, escrow_id="esc-1", status="in_progress"
    )
    escrow.release_partial.return_value = ReleaseResult(success=True, agent_amount=85.0)

    worker = _build_worker(escrow=escrow)
    await worker._step_escrow_release(_build_event(is_multi=True))

    escrow.accept_v2.assert_awaited_once()
    accept_call = escrow.accept_v2.await_args
    assert accept_call.kwargs["escrow_id"] == "esc-1"
    assert accept_call.kwargs["assignee_id"] == "agent-worker"
    escrow.release_partial.assert_awaited_once()
    rp = escrow.release_partial.await_args
    assert rp.kwargs["escrow_id"] == "esc-1"
    assert rp.kwargs["recipient_id"] == "agent-worker"
    assert rp.kwargs["amount"] == 100.0
    escrow.submit_v2.assert_not_awaited()
    escrow.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_escrow_release_multi_in_progress_skips_accept() -> None:
    """Multi-participant escrow already in_progress: jump to
    release_partial without re-accepting."""
    escrow = AsyncMock(spec=IEscrowProvider)
    escrow.get_by_task.return_value = EscrowDetailResult(
        success=True, escrow_id="esc-1", status="in_progress"
    )
    escrow.release_partial.return_value = ReleaseResult(success=True)

    worker = _build_worker(escrow=escrow)
    await worker._step_escrow_release(_build_event(is_multi=True))

    escrow.accept_v2.assert_not_awaited()
    escrow.release_partial.assert_awaited_once()


@pytest.mark.asyncio
async def test_escrow_release_idempotent_when_already_released() -> None:
    """The heart of the read-before-write design: if Backend reports
    status='released', the handler short-circuits without calling
    release again. This is what saves us from re-paying on retry,
    since Backend's release endpoint has no idempotency_key."""
    escrow = AsyncMock(spec=IEscrowProvider)
    escrow.get_by_task.return_value = EscrowDetailResult(
        success=True, escrow_id="esc-1", status="released"
    )

    worker = _build_worker(escrow=escrow)
    await worker._step_escrow_release(_build_event())

    escrow.release.assert_not_awaited()
    escrow.release_partial.assert_not_awaited()
    escrow.submit_v2.assert_not_awaited()
    escrow.accept_v2.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["rejected", "refunded"])
async def test_escrow_release_fast_fails_on_terminal_escrow_status(
    terminal_status: str,
) -> None:
    """Backend reports the escrow is in a terminal failure state
    (creator rejected the submission, or funds were already
    refunded). The worker MUST raise rather than try to release
    again — no number of retries can bring the funds back from
    those states, so fast-failing surfaces the inconsistency to
    operators instead of burning 12 backoff cycles before DLQ.
    """
    escrow = AsyncMock(spec=IEscrowProvider)
    escrow.get_by_task.return_value = EscrowDetailResult(
        success=True, escrow_id="esc-1", status=terminal_status
    )
    worker = _build_worker(escrow=escrow)
    with pytest.raises(StepHandlerError, match=terminal_status):
        await worker._step_escrow_release(_build_event())
    escrow.release.assert_not_awaited()
    escrow.release_partial.assert_not_awaited()
    escrow.submit_v2.assert_not_awaited()
    escrow.accept_v2.assert_not_awaited()


@pytest.mark.asyncio
async def test_escrow_release_raises_when_provider_not_injected() -> None:
    """``escrow_provider=None`` with an event that needs escrow -> raise
    so the row goes to retry/DLQ. Tests pre-mark the step skipped to
    avoid this in normal flow."""
    worker = _build_worker(escrow=None)
    with pytest.raises(StepHandlerError, match="escrow_provider"):
        await worker._step_escrow_release(_build_event())


@pytest.mark.asyncio
async def test_escrow_release_raises_when_assignee_missing() -> None:
    """Producer should have marked the step ``skipped`` when there's
    no assignee; reaching this handler is a payload bug."""
    escrow = AsyncMock(spec=IEscrowProvider)
    worker = _build_worker(escrow=escrow)
    with pytest.raises(StepHandlerError, match="assignee_id"):
        await worker._step_escrow_release(_build_event(assignee_id=None))
    escrow.get_by_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_escrow_release_raises_when_creator_missing() -> None:
    escrow = AsyncMock(spec=IEscrowProvider)
    worker = _build_worker(escrow=escrow)
    with pytest.raises(StepHandlerError, match="creator_id"):
        await worker._step_escrow_release(_build_event(creator_id=None))


@pytest.mark.asyncio
async def test_escrow_release_raises_on_invalid_reward_value() -> None:
    """``reward`` is a string in payload (JSON-serialisable Decimal
    round-trip); non-numeric strings should DLQ rather than crash."""
    escrow = AsyncMock(spec=IEscrowProvider)
    worker = _build_worker(escrow=escrow)
    with pytest.raises(StepHandlerError, match="invalid reward"):
        await worker._step_escrow_release(_build_event(reward="not-a-number"))


@pytest.mark.asyncio
async def test_escrow_release_raises_on_non_positive_reward() -> None:
    escrow = AsyncMock(spec=IEscrowProvider)
    worker = _build_worker(escrow=escrow)
    with pytest.raises(StepHandlerError, match="non-positive"):
        await worker._step_escrow_release(_build_event(reward="0"))


@pytest.mark.asyncio
async def test_escrow_release_raises_when_get_by_task_fails() -> None:
    """Backend transient failure on the read leg -> retry (raise)."""
    escrow = AsyncMock(spec=IEscrowProvider)
    escrow.get_by_task.return_value = EscrowDetailResult(
        success=False, error="HTTP 503 service unavailable"
    )
    worker = _build_worker(escrow=escrow)
    with pytest.raises(StepHandlerError, match="get_by_task"):
        await worker._step_escrow_release(_build_event())


@pytest.mark.asyncio
async def test_escrow_release_raises_when_backend_has_no_escrow_row() -> None:
    """``use_escrow=True`` on ACN side but Backend says no row -> config
    drift; fail loud so DLQ flags it for a human."""
    escrow = AsyncMock(spec=IEscrowProvider)
    escrow.get_by_task.return_value = EscrowDetailResult(success=True, escrow_id=None, status=None)
    worker = _build_worker(escrow=escrow)
    with pytest.raises(StepHandlerError, match="no escrow_id"):
        await worker._step_escrow_release(_build_event())


@pytest.mark.asyncio
async def test_escrow_release_propagates_release_partial_failure() -> None:
    """Multi-participant release_partial failure -> retry. The
    handler does NOT swallow the error and mark done."""
    escrow = AsyncMock(spec=IEscrowProvider)
    escrow.get_by_task.return_value = EscrowDetailResult(
        success=True, escrow_id="esc-1", status="in_progress"
    )
    escrow.release_partial.return_value = ReleaseResult(success=False, error="insufficient_funds")
    worker = _build_worker(escrow=escrow)
    with pytest.raises(StepHandlerError, match="release_partial"):
        await worker._step_escrow_release(_build_event(is_multi=True))


@pytest.mark.asyncio
async def test_escrow_release_propagates_release_failure() -> None:
    """Single-participant release failure -> retry."""
    escrow = AsyncMock(spec=IEscrowProvider)
    escrow.get_by_task.return_value = EscrowDetailResult(
        success=True, escrow_id="esc-1", status="submitted"
    )
    escrow.release.return_value = ReleaseResult(success=False, error="agent_wallet_missing")
    worker = _build_worker(escrow=escrow)
    with pytest.raises(StepHandlerError, match="release failed"):
        await worker._step_escrow_release(_build_event(is_multi=False))


@pytest.mark.asyncio
async def test_escrow_release_propagates_submit_v2_failure() -> None:
    """submit_v2 failure aborts before release is called."""
    escrow = AsyncMock(spec=IEscrowProvider)
    escrow.get_by_task.return_value = EscrowDetailResult(
        success=True, escrow_id="esc-1", status="locked"
    )
    escrow.submit_v2.return_value = EscrowDetailResult(success=False, error="invalid_transition")
    worker = _build_worker(escrow=escrow)
    with pytest.raises(StepHandlerError, match="submit_v2"):
        await worker._step_escrow_release(_build_event(is_multi=False))
    escrow.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_escrow_release_propagates_accept_v2_failure() -> None:
    """Multi-participant accept_v2 failure aborts before release_partial."""
    escrow = AsyncMock(spec=IEscrowProvider)
    escrow.get_by_task.return_value = EscrowDetailResult(
        success=True, escrow_id="esc-1", status="locked"
    )
    escrow.accept_v2.return_value = EscrowDetailResult(success=False, error="invalid_state")
    worker = _build_worker(escrow=escrow)
    with pytest.raises(StepHandlerError, match="accept_v2"):
        await worker._step_escrow_release(_build_event(is_multi=True))
    escrow.release_partial.assert_not_awaited()


# =============================================================================
# _step_reward_distribute
# =============================================================================


@pytest.mark.asyncio
async def test_reward_distribute_is_noop_and_never_raises() -> None:
    """v0.1 reward distribution is folded into escrow.release; this
    step just logs so the saga step_status moves to ``done``."""
    worker = _build_worker(escrow=None, reputation=None)
    await worker._step_reward_distribute(_build_event())


# =============================================================================
# _step_reputation_write
# =============================================================================


@pytest.mark.asyncio
async def test_reputation_write_calls_record_feedback_with_snapshot() -> None:
    """Happy path: assignee, signer, task_id flow from payload to
    ReputationService unchanged. ``score`` / ``evidence_uri`` are
    intentionally None at v0.1."""
    reputation = AsyncMock(spec=ReputationService)
    worker = _build_worker(reputation=reputation)
    await worker._step_reputation_write(_build_event())

    reputation.record_feedback.assert_awaited_once()
    kwargs = reputation.record_feedback.await_args.kwargs
    assert kwargs["agent_id"] == "agent-worker"
    assert kwargs["task_id"] == "task-1"
    assert kwargs["signer"] == "user-creator"
    assert kwargs["score"] is None
    assert kwargs["evidence_uri"] is None
    assert kwargs["task_metadata"] == {}


@pytest.mark.asyncio
async def test_reputation_write_propagates_smoke_test_flag() -> None:
    """The producer copies ``task.metadata`` into the payload
    verbatim; the worker must forward it so ``ReputationService``
    can stamp the smoke_test column. The default read path filters
    those out — see IReputationRepository.include_smoke_test.
    """
    reputation = AsyncMock(spec=ReputationService)
    worker = _build_worker(reputation=reputation)
    await worker._step_reputation_write(_build_event(metadata={"smoke_test": True, "x": "y"}))
    kwargs = reputation.record_feedback.await_args.kwargs
    assert kwargs["task_metadata"] == {"smoke_test": True, "x": "y"}


@pytest.mark.asyncio
async def test_reputation_write_raises_when_service_not_injected() -> None:
    worker = _build_worker(reputation=None)
    with pytest.raises(StepHandlerError, match="reputation_service"):
        await worker._step_reputation_write(_build_event())


@pytest.mark.asyncio
async def test_reputation_write_raises_when_assignee_missing() -> None:
    reputation = AsyncMock(spec=ReputationService)
    worker = _build_worker(reputation=reputation)
    with pytest.raises(StepHandlerError, match="assignee_id"):
        await worker._step_reputation_write(_build_event(assignee_id=None))
    reputation.record_feedback.assert_not_awaited()


@pytest.mark.asyncio
async def test_reputation_write_raises_when_creator_missing() -> None:
    reputation = AsyncMock(spec=ReputationService)
    worker = _build_worker(reputation=reputation)
    with pytest.raises(StepHandlerError, match="creator_id"):
        await worker._step_reputation_write(_build_event(creator_id=None))


@pytest.mark.asyncio
async def test_reputation_write_propagates_service_exception() -> None:
    """A repository failure (e.g. PG timeout) bubbles up unchanged so
    the outbox state machine retries. We do NOT catch + reraise as
    StepHandlerError here — the underlying error type is more
    informative on the way to the DLQ log."""
    reputation = AsyncMock(spec=ReputationService)
    reputation.record_feedback.side_effect = RuntimeError("pg conn drop")
    worker = _build_worker(reputation=reputation)
    with pytest.raises(RuntimeError, match="pg conn drop"):
        await worker._step_reputation_write(_build_event())


# =============================================================================
# _execute_step dispatcher
# =============================================================================


@pytest.mark.asyncio
async def test_execute_step_dispatches_to_named_handlers() -> None:
    """Smoke that the dispatcher actually calls each branch."""
    escrow = AsyncMock(spec=IEscrowProvider)
    escrow.get_by_task.return_value = EscrowDetailResult(
        success=True, escrow_id="esc-1", status="released"
    )
    reputation = AsyncMock(spec=ReputationService)
    worker = _build_worker(escrow=escrow, reputation=reputation)
    event = _build_event()

    await worker._execute_step(event, "escrow_release")
    await worker._execute_step(event, "reward_distribute")
    await worker._execute_step(event, "reputation_write")

    escrow.get_by_task.assert_awaited_once()
    reputation.record_feedback.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_step_unknown_step_raises() -> None:
    """Guard against a producer adding a step to ``_SAGA_STEPS``
    without updating the worker."""
    worker = _build_worker()
    with pytest.raises(StepHandlerError, match="unknown saga step"):
        await worker._execute_step(_build_event(), "does_not_exist")


# =============================================================================
# Metrics emission helpers (Todo 9b)
# =============================================================================
#
# We test the four ``_metric_*`` helpers in isolation because the
# orchestration path (``_process_event``) is already covered by the
# saga integration tests in ``test_settlement_saga.py``. The point of
# this section is to lock down the contract that metrics failures
# NEVER propagate and that the four helpers route to the correct
# MetricsCollector methods with the schema-compatible label sets.


class _RecordingMetrics:
    """Minimal recording stand-in for ``MetricsCollector``. Captures
    method + args; can be primed to raise to exercise the
    swallow-and-log branches.
    """

    def __init__(self, *, raise_on: str | None = None) -> None:
        self.counters: list[tuple[str, dict[str, str] | None]] = []
        self.gauges: list[tuple[str, float, dict[str, str] | None]] = []
        self.latencies: list[tuple[str, float]] = []
        self.errors: list[tuple[str, str]] = []
        self._raise_on = raise_on

    async def inc_counter(
        self,
        name: str,
        value: int = 1,
        labels: dict[str, str] | None = None,
    ) -> None:
        if self._raise_on == "inc_counter":
            raise RuntimeError("redis down")
        self.counters.append((name, labels))

    async def set_gauge(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
    ) -> None:
        if self._raise_on == "set_gauge":
            raise RuntimeError("redis down")
        self.gauges.append((name, value, labels))

    async def observe_latency(self, operation: str, seconds: float) -> None:
        if self._raise_on == "observe_latency":
            raise RuntimeError("redis down")
        self.latencies.append((operation, seconds))

    async def inc_error_count(self, error_type: str, component: str) -> None:
        if self._raise_on == "inc_error_count":
            raise RuntimeError("redis down")
        self.errors.append((error_type, component))


@pytest.mark.asyncio
async def test_metric_inc_attempts_routes_to_counter() -> None:
    """``acn_settlement_attempts_total`` is the headline saga
    success/retry/dead counter. Make sure the helper sends the
    label key the schema expects (``result``) and uses the
    short name ``settlement_attempts_total`` (collector prepends
    ``acn_``).
    """
    metrics = _RecordingMetrics()
    outbox = MagicMock(spec=ISettlementOutboxRepository)
    worker = SettlementWorker(outbox=outbox, metrics_collector=metrics)  # type: ignore[arg-type]

    await worker._metric_inc_attempts(result="success")
    await worker._metric_inc_attempts(result="retry")
    await worker._metric_inc_attempts(result="dead")

    assert metrics.counters == [
        ("settlement_attempts_total", {"result": "success"}),
        ("settlement_attempts_total", {"result": "retry"}),
        ("settlement_attempts_total", {"result": "dead"}),
    ]


@pytest.mark.asyncio
async def test_metric_inc_step_routes_with_step_and_result() -> None:
    """``acn_settlement_steps_total`` carries two labels — guard
    against accidental schema drift."""
    metrics = _RecordingMetrics()
    outbox = MagicMock(spec=ISettlementOutboxRepository)
    worker = SettlementWorker(outbox=outbox, metrics_collector=metrics)  # type: ignore[arg-type]

    await worker._metric_inc_step(step="escrow_release", result="ok")
    await worker._metric_inc_step(step="reputation_write", result="fail")
    await worker._metric_inc_step(step="reward_distribute", result="skipped")

    assert metrics.counters == [
        ("settlement_steps_total", {"step": "escrow_release", "result": "ok"}),
        ("settlement_steps_total", {"step": "reputation_write", "result": "fail"}),
        ("settlement_steps_total", {"step": "reward_distribute", "result": "skipped"}),
    ]


@pytest.mark.asyncio
async def test_metric_observe_step_latency_uses_settlement_step_prefix() -> None:
    """Latencies are emitted on the shared ``acn_latency_seconds``
    histogram with ``operation='settlement_step_<step>'``. Tying
    them to the existing histogram saves a metric declaration
    without confusing the operator: the prefix makes the saga
    rows easy to filter in Grafana.
    """
    metrics = _RecordingMetrics()
    outbox = MagicMock(spec=ISettlementOutboxRepository)
    worker = SettlementWorker(outbox=outbox, metrics_collector=metrics)  # type: ignore[arg-type]

    await worker._metric_observe_step_latency(step="escrow_release", latency_seconds=0.42)

    assert metrics.latencies == [("settlement_step_escrow_release", 0.42)]


@pytest.mark.asyncio
async def test_metric_refresh_state_gauges_emits_all_five_states() -> None:
    """``count_by_state`` always returns the five canonical states
    (with zero defaults); the gauge refresh must propagate all
    five so dashboards don't go stale on a drained state.
    """
    metrics = _RecordingMetrics()
    outbox = AsyncMock(spec=ISettlementOutboxRepository)
    outbox.count_by_state.return_value = {
        "pending": 1,
        "paying": 2,
        "retrying": 3,
        "done": 4,
        "dead": 0,
    }
    worker = SettlementWorker(outbox=outbox, metrics_collector=metrics)  # type: ignore[arg-type]

    await worker._metric_refresh_state_gauges()

    assert sorted(metrics.gauges, key=lambda x: x[2]["state"]) == sorted(
        [
            ("settlement_outbox_count", 1.0, {"state": "pending"}),
            ("settlement_outbox_count", 2.0, {"state": "paying"}),
            ("settlement_outbox_count", 3.0, {"state": "retrying"}),
            ("settlement_outbox_count", 4.0, {"state": "done"}),
            ("settlement_outbox_count", 0.0, {"state": "dead"}),
        ],
        key=lambda x: x[2]["state"],
    )


@pytest.mark.asyncio
async def test_metric_helpers_swallow_collector_errors() -> None:
    """A Redis blip during metric emission must NEVER propagate —
    it would otherwise turn into a step failure and risk DLQ-ing
    healthy events. Verify all four helpers eat the exception.
    """
    outbox = MagicMock(spec=ISettlementOutboxRepository)

    # Each variant raises in exactly one helper to ensure the
    # surrounding try/except blocks are tight.
    for which in ("inc_counter", "set_gauge", "observe_latency"):
        metrics = _RecordingMetrics(raise_on=which)
        worker = SettlementWorker(outbox=outbox, metrics_collector=metrics)  # type: ignore[arg-type]
        # All three helpers must not raise even though the
        # underlying collector is broken.
        await worker._metric_inc_attempts(result="success")
        await worker._metric_inc_step(step="x", result="ok")
        await worker._metric_observe_step_latency(step="x", latency_seconds=0.1)


@pytest.mark.asyncio
async def test_metric_refresh_swallows_count_by_state_error() -> None:
    """If ``count_by_state`` itself fails (PG blip) we log + skip
    the gauge update — must not crash the janitor loop."""
    metrics = _RecordingMetrics()
    outbox = AsyncMock(spec=ISettlementOutboxRepository)
    outbox.count_by_state.side_effect = RuntimeError("pg drop")
    worker = SettlementWorker(outbox=outbox, metrics_collector=metrics)  # type: ignore[arg-type]
    # Must not raise.
    await worker._metric_refresh_state_gauges()
    # No gauge written because count_by_state failed.
    assert metrics.gauges == []


@pytest.mark.asyncio
async def test_metrics_disabled_no_calls_attempted() -> None:
    """``metrics_collector=None`` is a valid wiring (no Redis env).
    Helpers must be true no-ops — verify they don't try to
    iterate / index a None.
    """
    outbox = MagicMock(spec=ISettlementOutboxRepository)
    worker = SettlementWorker(outbox=outbox, metrics_collector=None)
    # All four — no AttributeError on None.
    await worker._metric_inc_attempts(result="success")
    await worker._metric_inc_step(step="escrow_release", result="ok")
    await worker._metric_observe_step_latency(step="x", latency_seconds=0.1)
    await worker._metric_refresh_state_gauges()


# =============================================================================
# Backoff schedule (regression test for B1)
# =============================================================================
#
# Locks the design-doc invariant "first retry waits ``base`` seconds,
# then doubles each step until ``backoff_max_sec``" against the actual
# code path in ``_handle_step_failure``. The previous implementation
# computed ``base * 2 ** next_attempt_count`` which produced 4s on the
# first retry — fine for resilience, but inconsistent with the design
# doc and worse for fast-recovering backends. This test is the lock so
# future refactors don't silently reintroduce the off-by-one.


class _FixedClock:
    """Returns a fixed time so we can compute the expected
    ``next_attempt_at`` deterministically.
    """

    def __init__(self, t: Any) -> None:
        self._t = t

    def __call__(self) -> Any:
        return self._t


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current_attempts, expected_backoff_sec",
    [
        # First retry (attempts in PG is 0 when claimed; worker bumps
        # next_attempt_count to 1; backoff exponent is 0 → 2*1 = 2s).
        (0, 2.0),
        (1, 4.0),
        (2, 8.0),
        (3, 16.0),
        (4, 32.0),
        (5, 64.0),
        (6, 128.0),
        (7, 256.0),
        (8, 512.0),
        # 9 onwards hits the 900s cap.
        (9, 900.0),
        (10, 900.0),
    ],
)
async def test_handle_step_failure_backoff_schedule_matches_design_doc(
    current_attempts: int, expected_backoff_sec: float
) -> None:
    """Verify the backoff schedule emitted by ``_handle_step_failure``
    matches the design doc §5: 2 → 4 → 8 → ... → cap at 900s.

    Captures the ``next_attempt_at`` from the ``mark_retry`` call,
    subtracts the fixed clock time, and asserts the delta equals the
    expected backoff for that attempt count.
    """
    from datetime import UTC, datetime

    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    outbox = AsyncMock(spec=ISettlementOutboxRepository)
    worker = SettlementWorker(
        outbox=outbox,
        backoff_base_sec=2.0,
        backoff_max_sec=900.0,
        max_attempts=12,
        clock=_FixedClock(now),
    )

    event = _build_event()
    # Override attempts to drive the schedule. ``event.attempts`` is the
    # value the worker observed at claim time; the next-attempt count
    # is ``attempts + 1``.
    event = event.model_copy(update={"attempts": current_attempts})

    await worker._handle_step_failure(event, RuntimeError("boom"))

    outbox.mark_retry.assert_awaited_once()
    next_attempt_at = outbox.mark_retry.await_args.kwargs["next_attempt_at"]
    actual_backoff = (next_attempt_at - now).total_seconds()
    assert actual_backoff == expected_backoff_sec, (
        f"With attempts={current_attempts}, expected backoff "
        f"{expected_backoff_sec}s, got {actual_backoff}s"
    )


@pytest.mark.asyncio
async def test_handle_step_failure_marks_dead_at_max_attempts() -> None:
    """On the ``max_attempts``-th failure (counting from 1), the row
    moves to ``state='dead'`` rather than mark_retry. Locks the
    "12 attempts × backoff ≈ 1.3h" budget from design doc §5.
    """
    from datetime import UTC, datetime

    outbox = AsyncMock(spec=ISettlementOutboxRepository)
    worker = SettlementWorker(
        outbox=outbox,
        max_attempts=12,
        clock=_FixedClock(datetime(2026, 5, 5, tzinfo=UTC)),
        # No webhook so we don't hit the httpx path.
        dlq_alert_webhook=None,
    )
    # current attempts=11 → next_attempt_count=12 → mark_dead
    event = _build_event().model_copy(update={"attempts": 11})
    await worker._handle_step_failure(event, RuntimeError("permanent"))

    outbox.mark_dead.assert_awaited_once()
    outbox.mark_retry.assert_not_awaited()
