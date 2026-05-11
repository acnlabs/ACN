"""SettlementWorker — async consumer of the ``settlement_outbox`` table.

Purpose
-------
The producer side (``task_service.complete_task`` saga path) atomically
commits ``tasks.status`` and ``settlement_outbox.event`` in one
transaction; the worker side defined in this module drives those events
to terminal state asynchronously. See
``acn/docs/_drafts/settlement-saga-design.md`` for the design and
``.cursor/plans/settlement_saga_mvp_*.plan.md`` for the rollout plan.

What this worker does
---------------------
- Polls ``outbox.claim_batch`` every ``poll_interval_sec`` to pick up
  ready events (``pending`` or ``retrying`` with
  ``next_attempt_at <= now``). The repo flips them to ``state='paying'``
  inside a short transaction so they don't get re-claimed by other
  worker replicas (see ``PostgresSettlementOutboxRepository.claim_batch``).
- For each claimed event, runs the three saga steps in order
  (``escrow_release`` → ``reward_distribute`` → ``reputation_write``),
  then ``mark_done`` / ``mark_retry`` / ``mark_dead`` based on the
  outcome.
- Runs a janitor loop every ``janitor_interval_sec`` that calls
  ``outbox.sweep_stuck_paying`` to recover rows whose worker crashed
  mid-step.
- Exposes ``start()`` / ``stop()`` lifecycle methods that the FastAPI
  lifespan in ``acn.api`` wires up.

Step handler implementations (Todo 6)
-------------------------------------
- ``_step_escrow_release``: read-before-write. ``escrow.get_by_task``
  returns the canonical status; ``status == 'released'`` short-circuits
  for idempotency (Backend has no idempotency_key parameter today). For
  ``status in ('rejected', 'refunded')`` the step fails fast — those
  are terminal failure states from which no number of retries can
  release funds, so we want the row to DLQ quickly rather than
  burn 12 backoff cycles. For non-terminal rows, drive the required
  transitions then call ``release`` (single-participant) or
  ``release_partial`` (multi-participant) per ``payload['is_multi']``.
- ``_step_reward_distribute``: logged no-op at v0.1. The producer
  side (``task_service._build_review_pass_event``) sets this step to
  ``pending`` whenever ``has_reward`` holds (ap_points + positive
  amount + assignee), independent of ``use_escrow``. Two valid
  on-chain semantics get the step to this handler:
    * ``use_escrow=True`` — the agent / ACN / provider three-way
      split already settled inside ``escrow.release`` /
      ``release_partial``. There is nothing left to move.
    * ``use_escrow=False`` — ap_points reward is pure off-chain
      bookkeeping in v0.1; legacy ``_distribute_reward`` returns
      ``{success: True, via: 'off_chain'}`` without moving funds.
      The worker matches that no-op semantics.
  The named step survives in ``step_status`` so a future on-chain
  reward distributor can slot in without changing the schema.
- ``_step_reputation_write``: records a single feedback row in
  ``reputation_events`` keyed by ``(agent_id, task_id, kind)`` —
  the DB unique constraint makes the call idempotent. v0.1 leaves
  ``score`` / ``evidence_uri`` unset; the route-level
  ``POST /feedback`` is the path that carries those today.

``settlement_worker_enabled`` defaults to FALSE in ``acn.config``
until the saga path has been verified in staging. The legacy
synchronous path in ``task_service.complete_task`` is still active
under that flag — see plan §6 Todo 7 for the cleanup PR that
retires it.

Concurrency model
-----------------
- The poll loop and the janitor loop run as two separate
  ``asyncio.Task`` instances inside the same event loop. They share
  the same ``ISettlementOutboxRepository``, which opens a fresh
  ``AsyncSession`` per method call from the underlying
  ``async_sessionmaker``; nothing is shared between the two loops
  except the connection pool itself. (``AsyncSession`` is NOT
  thread-safe — it's bound to whichever task created it — so the
  "fresh session per call" pattern is what keeps the two loops out
  of each other's way.)
- Multiple worker REPLICAS (separate processes) can run concurrently
  thanks to ``SELECT ... FOR UPDATE SKIP LOCKED`` inside
  ``claim_batch``. ACN is single-replica on Railway today, but the
  worker is correct under N replicas with zero code changes.
- ``stop()`` signals both tasks via ``asyncio.Event``, awaits them
  with a timeout, and cancels stragglers — graceful within reason
  but bounded so a deploy doesn't hang.

Failure handling
----------------
- Step handler raises -> ``mark_retry`` with exponential backoff.
  Attempts incremented atomically server-side.
- ``attempts >= max_attempts`` (default 12) -> ``mark_dead``, optional
  DLQ webhook POST with event details.
- The poll loop itself catches exceptions broadly and logs them
  (``worker_poll_loop_error``) so a transient DB hiccup doesn't kill
  the loop; otherwise settlement would stall indefinitely.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import structlog

from ..core.interfaces.escrow_provider import IEscrowProvider
from ..core.interfaces.settlement_outbox_repository import (
    ISettlementOutboxRepository,
    SettlementEvent,
)
from ..monitoring.metrics import MetricsCollector
from .reputation_service import ReputationService

logger = structlog.get_logger()


class StepHandlerError(RuntimeError):
    """Raised by a step handler when an external call fails.

    The poll loop catches every ``Exception`` already and feeds it to
    ``_handle_step_failure``; this subclass exists only to give the
    error log a unique class name (``StepHandlerError`` vs generic
    ``RuntimeError``) so an operator scanning the worker log can tell
    at a glance whether the failure originated *inside* a step
    handler (almost always retry-worthy: backend 5xx, transient
    network) or from the outbox plumbing (rare: DB outage, schema
    drift). Both still go through the same exponential backoff +
    DLQ path.
    """


# Stable list of saga steps, evaluated by the worker in order.
# v0.1 keeps them as named string keys in ``step_status`` so the
# repository's JSONB patch (``update_step_status``) doesn't need an
# enum migration to expand the saga later.
_SAGA_STEPS: tuple[str, ...] = (
    "escrow_release",
    "reward_distribute",
    "reputation_write",
)


class SettlementWorker:
    """Asyncio worker that drains ``settlement_outbox``.

    Lifecycle: ``await start()`` to spawn the loops, ``await stop()``
    to drain them gracefully. Not safe to start twice on the same
    instance — construct a fresh worker per process.
    """

    def __init__(
        self,
        outbox: ISettlementOutboxRepository,
        *,
        escrow_provider: IEscrowProvider | None = None,
        reputation_service: ReputationService | None = None,
        metrics_collector: MetricsCollector | None = None,
        poll_interval_sec: float = 1.0,
        batch_size: int = 10,
        max_attempts: int = 12,
        backoff_base_sec: float = 2.0,
        backoff_max_sec: float = 900.0,
        janitor_interval_sec: float = 30.0,
        janitor_stuck_threshold_sec: float = 300.0,
        dlq_alert_webhook: str | None = None,
        # Clock injection for testability — production passes the
        # default ``datetime.now(UTC)`` factory.
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Construct a worker bound to the outbox repository and the
        side-effect collaborators.

        Args:
            outbox: ``ISettlementOutboxRepository`` for ``claim_batch``
                / ``mark_*`` / ``update_step_status`` / janitor sweep.
            escrow_provider: ``IEscrowProvider`` invoked by
                ``_step_escrow_release``. ``None`` is a valid wiring
                for Redis-only deployments that have no escrow
                feature at all; in that case any event whose
                ``step_status['escrow_release']`` is ``pending`` will
                raise ``StepHandlerError`` and DLQ after the attempts
                ceiling. The producer side gates on
                ``task.use_escrow`` (and ``has_reward``) already, so
                under correct config — Redis-only deployments never
                set ``use_escrow=True`` — ``None`` here is fine and
                no events with ``escrow_release=pending`` ever
                appear in the outbox.
            reputation_service: ``ReputationService`` for
                ``_step_reputation_write``. Idempotency lives in the
                repository via the ``(agent_id, task_id, kind)``
                unique constraint, so retrying a feedback write is
                a no-op at the row level. ``None`` skips the
                reputation step entirely (test fixtures).
            metrics_collector: Optional ``MetricsCollector`` used to
                emit the saga's four Prometheus signals
                (``acn_settlement_outbox_count``,
                ``acn_settlement_attempts_total``,
                ``acn_settlement_steps_total``,
                ``acn_latency_seconds{operation="settlement_step_*"}``).
                When ``None`` the worker runs unchanged but emits no
                metrics — used by tests that don't care about
                instrumentation, or by minimal deployments that
                haven't wired Redis-backed metrics yet. Failures
                inside metrics emission are caught and logged so a
                Redis blip never propagates into the saga state
                machine.
        """
        self._outbox = outbox
        self._escrow = escrow_provider
        self._reputation = reputation_service
        self._metrics = metrics_collector
        self._poll_interval_sec = poll_interval_sec
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._backoff_base_sec = backoff_base_sec
        self._backoff_max_sec = backoff_max_sec
        self._janitor_interval_sec = janitor_interval_sec
        self._janitor_stuck_threshold = timedelta(seconds=janitor_stuck_threshold_sec)
        self._dlq_alert_webhook = dlq_alert_webhook
        self._clock = clock or (lambda: datetime.now(UTC))

        # Lifecycle plumbing.
        self._stop_event: asyncio.Event | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._janitor_task: asyncio.Task[None] | None = None
        # Reused httpx client for DLQ webhook POSTs — opened lazily on
        # first alert so workers that never DLQ pay zero overhead.
        self._dlq_http_client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the poll + janitor tasks. Idempotent: second call is
        rejected (would otherwise create duplicate consumers per
        instance, which is a bug not a feature)."""
        if self._poll_task is not None:
            raise RuntimeError("SettlementWorker.start() called twice on same instance")
        self._stop_event = asyncio.Event()
        # Visible operator log: which collaborators are wired and
        # which are not. A staging engineer flipping
        # ``SETTLEMENT_WORKER_ENABLED=true`` should see this line
        # and immediately tell whether they're getting REAL
        # settlement (escrow + reputation injected) or partial
        # (e.g. reputation only because escrow client isn't
        # configured). Warning level so it survives the default
        # filter.
        logger.warning(
            "settlement_worker_starting",
            escrow_provider_wired=self._escrow is not None,
            reputation_service_wired=self._reputation is not None,
            metrics_wired=self._metrics is not None,
            poll_interval_sec=self._poll_interval_sec,
            batch_size=self._batch_size,
            max_attempts=self._max_attempts,
            janitor_interval_sec=self._janitor_interval_sec,
        )
        self._poll_task = asyncio.create_task(self._run_poll_loop(), name="settlement-worker-poll")
        self._janitor_task = asyncio.create_task(
            self._run_janitor_loop(), name="settlement-worker-janitor"
        )

    async def stop(self, *, timeout: float = 10.0) -> None:
        """Signal both loops to stop, await them up to ``timeout`` s
        each, then cancel stragglers. Safe to call when not started.
        Closes the lazy DLQ httpx client to release sockets.
        """
        if self._stop_event is None:
            return
        self._stop_event.set()
        for task in (self._poll_task, self._janitor_task):
            if task is None:
                continue
            try:
                await asyncio.wait_for(task, timeout=timeout)
            except TimeoutError:
                logger.warning(
                    "settlement_worker_stop_timeout",
                    task=task.get_name(),
                    timeout=timeout,
                )
                task.cancel()
                # Don't await the cancellation — best effort; the
                # lifespan cleanup is on a global deadline.
            except asyncio.CancelledError:
                # Already cancelled externally (e.g. lifespan shutdown
                # cascade). Treat as clean exit.
                pass
            except Exception as exc:  # noqa: BLE001 — log all teardown errors loudly
                logger.error(
                    "settlement_worker_stop_error",
                    task=task.get_name(),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        self._poll_task = None
        self._janitor_task = None
        self._stop_event = None
        if self._dlq_http_client is not None:
            try:
                await self._dlq_http_client.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.error("settlement_worker_dlq_client_close_error", error=str(exc))
            self._dlq_http_client = None

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _run_poll_loop(self) -> None:
        """Main consumer loop. Survives every exception kind except
        the stop signal — ``settlement_outbox`` must not stall on a
        single bad row or a transient DB blip.
        """
        assert self._stop_event is not None  # noqa: S101 — narrows for type checker
        while not self._stop_event.is_set():
            try:
                events = await self._outbox.claim_batch(
                    limit=self._batch_size,
                    now=self._clock(),
                )
            except Exception as exc:  # noqa: BLE001
                # claim_batch failures are almost always transient
                # (PgBouncer reconnect, DB restart). Log and sleep
                # one poll cycle; do NOT advance any event state.
                logger.error(
                    "settlement_worker_claim_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                await self._sleep_with_cancel(self._poll_interval_sec)
                continue
            if not events:
                # Quiet period — sleep then re-poll. Skip the sleep
                # if a non-empty batch came back so a backlog drains
                # at peak throughput.
                await self._sleep_with_cancel(self._poll_interval_sec)
                continue
            for event in events:
                # The stop check inside the inner loop matters: a
                # batch of 10 events with slow step handlers could
                # otherwise extend shutdown by tens of seconds.
                if self._stop_event.is_set():
                    break
                await self._process_event(event)

    async def _process_event(self, event: SettlementEvent) -> None:
        """Run the saga step pipeline for one event and update the
        outbox state machine accordingly.

        Step semantics:
        - Steps whose ``step_status`` is already ``done`` or
          ``skipped`` are no-ops (worker resumed mid-saga, or the
          producer marked the step inapplicable up front).
        - Steps whose status is ``pending`` are dispatched via
          ``_execute_step`` to the concrete handler.
        - Any step raising ``Exception`` triggers ``mark_retry`` with
          exponential backoff. Repeated failures up to
          ``max_attempts`` end in ``mark_dead`` plus optional
          webhook alert.
        """
        try:
            for step in _SAGA_STEPS:
                current = event.step_status.get(step, "pending")
                if current in ("done", "skipped"):
                    # Producer pre-marked this step inapplicable, or
                    # a previous worker run already finished it. We
                    # still record a metric so dashboards can see
                    # how often steps short-circuit — useful for
                    # spotting producer gating drift (e.g. spike in
                    # ``escrow_release{result=skipped}`` after a
                    # config change).
                    await self._metric_inc_step(step=step, result=current)
                    continue
                step_started_at = self._clock()
                try:
                    await self._execute_step(event, step)
                except Exception:
                    await self._metric_inc_step(step=step, result="fail")
                    raise
                else:
                    await self._metric_inc_step(step=step, result="ok")
                finally:
                    latency = (self._clock() - step_started_at).total_seconds()
                    await self._metric_observe_step_latency(step=step, latency_seconds=latency)
                await self._outbox.update_step_status(event.event_id, step=step, status="done")
            await self._outbox.mark_done(event.event_id)
            await self._metric_inc_attempts(result="success")
            logger.info(
                "settlement_outbox_event_done",
                event_id=event.event_id,
                task_id=event.task_id,
            )
        except Exception as exc:  # noqa: BLE001
            await self._handle_step_failure(event, exc)

    async def _execute_step(self, event: SettlementEvent, step: str) -> None:
        """Dispatch one saga step to its concrete handler.

        Each handler:

        - Must be idempotent under retries. Concretely: a successful
          first run followed by a worker crash before
          ``update_step_status`` flips the step to ``done`` will
          replay this exact same call on the next claim. The handler
          MUST short-circuit (without raising) when it detects the
          side effect already landed (escrow already ``released``;
          reputation row already present via the
          ``(agent_id, task_id, kind)`` unique constraint).
        - Raises on any error it wants the outbox to retry. The
          retry/dead decision and backoff live in
          ``_handle_step_failure`` — handlers do not implement their
          own retry loops.
        - Reads everything it needs from ``event.payload``. We do NOT
          re-fetch the ``tasks`` row from inside the worker: the
          payload was committed as a snapshot in the same
          transaction that flipped the task to ``COMPLETED`` (see
          ``task_service._build_review_pass_event``), so it cannot
          have drifted out from under us.

        Unknown ``step`` values raise — protects against a producer
        adding a step to ``_SAGA_STEPS`` without updating the worker.
        """
        if step == "escrow_release":
            await self._step_escrow_release(event)
        elif step == "reward_distribute":
            await self._step_reward_distribute(event)
        elif step == "reputation_write":
            await self._step_reputation_write(event)
        else:  # pragma: no cover — defensive guard for future steps
            raise StepHandlerError(f"unknown saga step: {step!r}")

    # ------------------------------------------------------------------
    # Step handlers
    # ------------------------------------------------------------------

    async def _step_escrow_release(self, event: SettlementEvent) -> None:
        """Release the locked escrow back to the assignee.

        Read-before-write design (plan §3):

        1. ``escrow.get_by_task`` returns the canonical status.
           Treated as the source of truth — Backend, not ACN, owns
           the escrow state machine.
        2. ``status == 'released'`` short-circuits as a success —
           the side effect landed on a previous worker run, no
           write needed. This is the heart of the idempotency
           story: the Backend escrow endpoint has no
           ``idempotency_key`` parameter (audit gap noted in
           plan), so we lean on the ``released`` terminal state.
        3. ``status in ('rejected', 'refunded')`` short-circuits
           as a FAILURE — these are terminal states from which no
           amount of retrying can release funds. Raising
           immediately lets the attempts ceiling drag the row to
           DLQ on the next ``mark_retry`` without burning the
           usual 12 backoff cycles.
        4. Otherwise drive escrow through the required transitions
           (``locked`` → ``in_progress`` → ``submitted``) before
           calling ``release`` / ``release_partial``. Multi-vs-
           single is read from ``payload['is_multi']`` — frozen at
           enqueue time so we don't need to refetch the task.

        Any failure path raises ``StepHandlerError`` so the outer
        ``_handle_step_failure`` schedules a retry (or marks dead).
        A ``released`` short-circuit explicitly does NOT raise —
        it's the happy idempotent path.
        """
        if self._escrow is None:
            raise StepHandlerError(
                "escrow_release step requires an escrow_provider, "
                "but worker was constructed with escrow_provider=None"
            )

        payload = event.payload
        task_id = str(payload.get("task_id") or event.task_id)
        creator_id = payload.get("creator_id")
        assignee_id = payload.get("assignee_id")
        is_multi = bool(payload.get("is_multi", False))
        reward_str = payload.get("reward")

        if not assignee_id:
            # Producer should have marked this step ``skipped`` via
            # the ``task.assignee_id`` gating in
            # ``_build_review_pass_event``. If we reach here the
            # event is malformed — fail loudly, let the DLQ catch it.
            raise StepHandlerError(
                f"escrow_release: payload missing assignee_id for task {task_id}"
            )
        if not creator_id:
            raise StepHandlerError(f"escrow_release: payload missing creator_id for task {task_id}")
        try:
            amount = float(reward_str) if reward_str is not None else 0.0
        except (TypeError, ValueError) as exc:
            raise StepHandlerError(
                f"escrow_release: invalid reward {reward_str!r} for task {task_id}"
            ) from exc
        if amount <= 0:
            raise StepHandlerError(
                f"escrow_release: non-positive reward {amount} for task {task_id} "
                "— producer should have marked this step skipped"
            )

        info = await self._escrow.get_by_task(task_id)
        if not info.success:
            raise StepHandlerError(f"escrow.get_by_task failed for task {task_id}: {info.error}")

        status = (info.status or "").lower()
        if status == "released":
            logger.info(
                "settlement_step_escrow_release_idempotent_skip",
                event_id=event.event_id,
                task_id=task_id,
                escrow_id=info.escrow_id,
                reason="status_already_released",
            )
            return

        if status in ("rejected", "refunded"):
            # Terminal failure status — no further state transition
            # the worker can drive. We RAISE rather than silently
            # mark the step done, because:
            #   - The reputation_write step still pending in the
            #     same saga represents a legitimately completed
            #     task that didn't get paid (e.g. creator rejected
            #     review after submission). Letting the saga move
            #     forward would write a reputation row tied to a
            #     payment that never happened — misleading.
            #   - DLQ-ing surfaces the inconsistency to a human:
            #     either the task should have been refunded on the
            #     ACN side (and reputation suppressed), or the
            #     escrow shouldn't have been moved to the terminal
            #     state without ACN's knowledge. Both warrant
            #     attention.
            raise StepHandlerError(
                f"escrow_release: task {task_id} escrow in terminal "
                f"state {status!r}; ACN cannot release funds from "
                "this state — operator intervention required"
            )

        if info.escrow_id is None:
            # ``use_escrow=True`` was set on the task but Backend
            # has no escrow row. This is a config drift between
            # ACN and Backend — fail loudly so a human investigates.
            # Retrying won't help unless the Backend row magically
            # appears, so let the attempts ceiling DLQ it.
            raise StepHandlerError(
                f"escrow_release: task {task_id} has use_escrow=True but "
                "Backend returned no escrow_id"
            )

        if is_multi:
            # Multi-participant: per-completion partial release.
            # The escrow row stays IN_PROGRESS until depleted; we
            # only need ``locked → in_progress`` here.
            if status == "locked":
                accept = await self._escrow.accept_v2(
                    escrow_id=info.escrow_id,
                    assignee_id=str(assignee_id),
                    assignee_type="agent",
                )
                if not accept.success:
                    raise StepHandlerError(
                        f"escrow.accept_v2 failed for escrow {info.escrow_id}: {accept.error}"
                    )
            release = await self._escrow.release_partial(
                escrow_id=info.escrow_id,
                recipient_id=str(assignee_id),
                recipient_type="agent",
                amount=amount,
                notes=f"Settlement saga release_partial for task {task_id}",
            )
            if not release.success:
                raise StepHandlerError(
                    f"escrow.release_partial failed for escrow {info.escrow_id}: {release.error}"
                )
            logger.info(
                "settlement_step_escrow_release_partial_ok",
                event_id=event.event_id,
                task_id=task_id,
                escrow_id=info.escrow_id,
                agent_amount=release.agent_amount,
                acn_amount=release.acn_amount,
                provider_amount=release.provider_amount,
            )
            return

        # Single-participant: full release via v1 endpoint, which
        # also implicitly transitions ``locked → in_progress →
        # submitted → released`` in one shot on Backend side. We
        # still drive ``submit_v2`` first for safety: if the escrow
        # was opened via v2 lifecycle, Backend's v1 ``release``
        # rejects rows that haven't been submitted.
        if status in ("locked", "in_progress"):
            submit = await self._escrow.submit_v2(info.escrow_id)
            if not submit.success:
                raise StepHandlerError(
                    f"escrow.submit_v2 failed for escrow {info.escrow_id}: {submit.error}"
                )
        release = await self._escrow.release(
            creator_user_id=str(creator_id),
            agent_owner_user_id=str(assignee_id),
            task_id=task_id,
            amount=amount,
            description=f"Settlement saga release for task {task_id}",
        )
        if not release.success:
            raise StepHandlerError(f"escrow.release failed for task {task_id}: {release.error}")
        logger.info(
            "settlement_step_escrow_release_ok",
            event_id=event.event_id,
            task_id=task_id,
            escrow_id=info.escrow_id,
            agent_amount=release.agent_amount,
            acn_amount=release.acn_amount,
            provider_amount=release.provider_amount,
        )

    async def _step_reward_distribute(self, event: SettlementEvent) -> None:
        """v0.1: logged no-op step.

        Producer-side gating (``task_service._build_review_pass_event``)
        sets this step ``pending`` whenever ``has_reward`` holds:
        ``currency in (ap_points, points) AND reward > 0 AND
        assignee``. Critically, this is INDEPENDENT of ``use_escrow``
        — both branches reach this handler with different "no-op"
        semantics:

        Path A — ``use_escrow=True``:
            ``_step_escrow_release`` already ran and called
            ``escrow.release`` / ``escrow.release_partial``, which
            atomically split the amount into agent / ACN / provider
            wallets (see ``ReleaseResult`` in
            ``core.interfaces.escrow_provider``). Funds are already
            on the agent's wallet by the time this handler executes.
            The dedicated reward-distribute call would re-pay.

        Path B — ``use_escrow=False``:
            Off-chain reward bookkeeping. Legacy
            ``_distribute_reward`` returns
            ``{success: True, via: 'off_chain'}`` without moving any
            funds — ap_points reward without escrow is a conceptual
            credit in v0.1, not a transferred balance. The worker
            matches that no-op semantics so saga and legacy paths
            settle identically.

        Why not drop the step entirely? Because the ``step_status``
        JSONB shape is part of the outbox row schema; removing a
        key would invalidate in-flight events on a deploy. Keeping
        it ``pending`` here also reserves a slot for a future
        on-chain reward distributor (separate from escrow.release)
        without producer-side schema churn.

        Log line ``reason`` is deliberately phrased so a log search
        ("reward_distribute_noop") returns one operator-readable
        explanation that covers BOTH paths — auditors who only see
        this line should NOT conclude "money was definitely moved
        in escrow.release". They should consult the event payload's
        ``use_escrow`` field (kept as a diagnostic on the outbox row)
        or the preceding ``settlement_step_escrow_release_ok`` log.
        """
        logger.info(
            "settlement_step_reward_distribute_noop",
            event_id=event.event_id,
            task_id=event.task_id,
            use_escrow=bool(event.payload.get("use_escrow", False)),
            reason=(
                "v0.1 reward settlement: escrow.release handles money "
                "movement when use_escrow=True; off-chain credit only "
                "when use_escrow=False"
            ),
        )

    async def _step_reputation_write(self, event: SettlementEvent) -> None:
        """Record a feedback row in ``reputation_events``.

        The current data model uses a single ``feedback`` row per
        ``(agent_id, task_id)`` — the unique constraint makes
        ``record_feedback`` idempotent at the DB level (``ON
        CONFLICT DO NOTHING`` returning the original row). So a
        worker retry between this call's success and the
        ``update_step_status`` flip is safe.

        ``score`` and ``evidence_uri`` are not populated at v0.1:
        the saga is triggered by a creator review-pass, which
        doesn't carry a numeric rating in the current product
        flow. A future review UI can attach those into the event
        payload; the route-level ``POST /feedback`` handler
        already accepts them today.

        ``task_metadata`` propagates the ``smoke_test`` flag if
        the producer set it, so reputation reads can filter out
        smoke traffic by default — see ``IReputationRepository``
        ``include_smoke_test`` parameter.
        """
        if self._reputation is None:
            # Two valid wirings can reach here:
            #   (a) Production wired everything except reputation
            #       (PG outbox + escrow but no reputation service).
            #       In that case skipping silently would lose the
            #       reputation signal forever. Better to fail and
            #       force an operator to either disable the step or
            #       wire the service.
            #   (b) Tests intentionally pass ``reputation_service=
            #       None`` to focus on escrow paths. Tests pre-mark
            #       this step ``skipped`` in ``step_status`` so it
            #       never reaches here.
            raise StepHandlerError(
                "reputation_write step requires a reputation_service, "
                "but worker was constructed with reputation_service=None"
            )

        payload = event.payload
        task_id = str(payload.get("task_id") or event.task_id)
        creator_id = payload.get("creator_id")
        assignee_id = payload.get("assignee_id")
        raw_metadata = payload.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}

        if not assignee_id:
            raise StepHandlerError(
                f"reputation_write: payload missing assignee_id for task {task_id}"
            )
        if not creator_id:
            raise StepHandlerError(
                f"reputation_write: payload missing creator_id for task {task_id}"
            )

        # No ``session=`` argument: the worker has no outer
        # transaction context (it's running outside any FastAPI
        # request scope), so ReputationService will open its own
        # session and commit. That's intentional — the step's
        # success/failure boundary is "the row is persisted",
        # which lines up exactly with the service-owned session.
        # The outbox step_status flip happens in a SEPARATE
        # transaction afterwards (``update_step_status``), which
        # is fine because the repository write is idempotent under
        # retries via the ``(agent_id, task_id, kind)`` constraint.
        await self._reputation.record_feedback(
            agent_id=str(assignee_id),
            task_id=task_id,
            signer=str(creator_id),
            score=None,
            evidence_uri=None,
            task_metadata=metadata,
        )
        logger.info(
            "settlement_step_reputation_write_ok",
            event_id=event.event_id,
            task_id=task_id,
            agent_id=assignee_id,
            signer=creator_id,
        )

    async def _handle_step_failure(self, event: SettlementEvent, exc: BaseException) -> None:
        """Decide retry vs dead based on attempts, with exponential
        backoff. The mark_retry / mark_dead calls themselves are
        independent transactions in the outbox repository, so a
        failure here doesn't roll back any successful per-step
        progress that ``_process_event`` already flushed."""
        # ``event.attempts`` is the value the worker observed at
        # claim time; ``mark_retry`` increments server-side. So the
        # "next" attempt count is ``attempts + 1`` for the dead-vs-
        # retry decision.
        next_attempt_count = event.attempts + 1
        # Hard cap on the error string written to PG.
        # ``settlement_outbox.last_error`` is TEXT (no length limit),
        # but unbounded exception messages — long stack-trace dumps,
        # full HTTP response bodies on 4xx, etc. — bloat the row,
        # hurt page packing, and flood SQL consoles. 2 KB is well
        # above any informative exception message and well below
        # anywhere PG's row layout starts to care.
        last_error = f"{type(exc).__name__}: {exc}"[:2000]
        if next_attempt_count >= self._max_attempts:
            try:
                await self._outbox.mark_dead(event.event_id, error=last_error)
            except Exception as repo_exc:  # noqa: BLE001
                logger.error(
                    "settlement_worker_mark_dead_failed",
                    event_id=event.event_id,
                    error=str(repo_exc),
                )
                return
            await self._metric_inc_attempts(result="dead")
            await self._alert_dlq(event, last_error)
            return

        # Backoff schedule (matches settlement-saga-design.md §5):
        # first retry waits ``base`` seconds, then doubles each step
        # until ``backoff_max_sec``. With defaults base=2 / max=900
        # and 12 attempts this gives 2 → 4 → 8 → 16 → 32 → 64 →
        # 128 → 256 → 512 → 900 → 900 → 900 (cap), summing to
        # ≈ 1.3 h of self-healing before DLQ.
        #
        # The exponent is ``next_attempt_count - 1`` (not
        # ``next_attempt_count``) so the FIRST retry is exactly
        # ``base`` seconds, not ``base * 2``. Off-by-one here would
        # mean a backend that recovers in 1-2s (typical for a quick
        # restart) is needlessly held back to the 4s second-attempt
        # window.
        backoff_sec = min(
            self._backoff_base_sec * (2 ** (next_attempt_count - 1)),
            self._backoff_max_sec,
        )
        next_at = self._clock() + timedelta(seconds=backoff_sec)
        try:
            await self._outbox.mark_retry(
                event.event_id,
                error=last_error,
                next_attempt_at=next_at,
            )
        except Exception as repo_exc:  # noqa: BLE001
            logger.error(
                "settlement_worker_mark_retry_failed",
                event_id=event.event_id,
                error=str(repo_exc),
            )
            return
        await self._metric_inc_attempts(result="retry")
        logger.warning(
            "settlement_worker_event_retry_scheduled",
            event_id=event.event_id,
            task_id=event.task_id,
            attempts=next_attempt_count,
            backoff_sec=backoff_sec,
            next_attempt_at=next_at.isoformat(),
            last_error=last_error[:500],
        )

    # ------------------------------------------------------------------
    # Janitor loop
    # ------------------------------------------------------------------

    async def _run_janitor_loop(self) -> None:
        """Periodically resurrect rows stuck in ``state='paying'``
        because their worker crashed mid-step, and refresh the
        outbox-state Prometheus gauge.

        Why pair the two activities here rather than in a dedicated
        loop: the janitor is already on the slow cadence (default
        30s) that matches an operator dashboard refresh rate; an
        additional async task purely for ``set_gauge`` would be
        overkill. ``count_by_state`` is a cheap O(states) query
        against the indexed ``state`` column.

        Gauge refresh failures are caught inside
        ``_metric_refresh_state_gauges`` so they cannot crash the
        sweep half of this loop. See
        ``ISettlementOutboxRepository.sweep_stuck_paying``.
        """
        assert self._stop_event is not None  # noqa: S101
        while not self._stop_event.is_set():
            await self._sleep_with_cancel(self._janitor_interval_sec)
            if self._stop_event.is_set():
                break
            try:
                threshold = self._clock() - self._janitor_stuck_threshold
                n = await self._outbox.sweep_stuck_paying(older_than=threshold)
                if n > 0:
                    logger.warning(
                        "settlement_worker_janitor_swept",
                        n=n,
                        older_than=threshold.isoformat(),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "settlement_worker_janitor_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            # Refresh outbox-state gauge regardless of whether
            # sweep_stuck_paying succeeded — operators want the
            # state distribution even when the sweep half hits a
            # transient DB error.
            await self._metric_refresh_state_gauges()

    # ------------------------------------------------------------------
    # Metrics helpers
    # ------------------------------------------------------------------

    async def _metric_inc_attempts(self, *, result: str) -> None:
        """Increment ``acn_settlement_attempts_total{result=...}``.

        ``result`` is one of ``success`` / ``retry`` / ``dead``.
        Failures inside metrics are swallowed so a Redis hiccup
        cannot corrupt the saga state machine — operators would
        already see ``settlement_worker_*`` log lines.
        """
        if self._metrics is None:
            return
        try:
            await self._metrics.inc_counter("settlement_attempts_total", labels={"result": result})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "settlement_metrics_inc_failed",
                metric="settlement_attempts_total",
                result=result,
                error=str(exc),
            )

    async def _metric_inc_step(self, *, step: str, result: str) -> None:
        """Increment ``acn_settlement_steps_total{step, result}``.

        ``result`` is one of ``ok`` / ``fail`` / ``skipped``.
        """
        if self._metrics is None:
            return
        try:
            await self._metrics.inc_counter(
                "settlement_steps_total",
                labels={"step": step, "result": result},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "settlement_metrics_inc_failed",
                metric="settlement_steps_total",
                step=step,
                result=result,
                error=str(exc),
            )

    async def _metric_observe_step_latency(self, *, step: str, latency_seconds: float) -> None:
        """Record one step's wall-clock duration into
        ``acn_latency_seconds{operation="settlement_step_<step>"}``.

        Reusing the existing histogram (rather than declaring a new
        ``acn_settlement_step_duration_seconds``) keeps this PR
        free of MetricsCollector schema churn; see
        ``MetricsCollector.METRICS['acn_latency_seconds']`` for the
        bucket choice rationale.
        """
        if self._metrics is None:
            return
        try:
            await self._metrics.observe_latency(f"settlement_step_{step}", latency_seconds)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "settlement_metrics_observe_failed",
                metric="acn_latency_seconds",
                step=step,
                error=str(exc),
            )

    async def _metric_refresh_state_gauges(self) -> None:
        """Update ``acn_settlement_outbox_count{state}`` for the five
        canonical states. Called from the janitor loop so it runs
        on a slow cadence (default 30s) — perfect for an operator
        dashboard, low overhead.

        ``count_by_state`` returns all five states with explicit
        zeros (interface contract on ``count_by_state``), so the
        gauge always reflects the full histogram without stale
        keys lingering for a state that has since drained to zero.
        """
        if self._metrics is None:
            return
        try:
            counts = await self._outbox.count_by_state()
        except Exception as exc:  # noqa: BLE001
            logger.warning("settlement_metrics_count_failed", error=str(exc))
            return
        for state, count in counts.items():
            try:
                await self._metrics.set_gauge(
                    "settlement_outbox_count",
                    float(count),
                    labels={"state": state},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "settlement_metrics_set_failed",
                    metric="settlement_outbox_count",
                    state=state,
                    error=str(exc),
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _sleep_with_cancel(self, seconds: float) -> None:
        """Sleep ``seconds`` but wake up early if stop signal fires.

        ``asyncio.Event.wait()`` blocks forever waiting for set();
        we race it against an ``asyncio.sleep`` to get
        "sleep-or-stop-whichever-first" semantics. The losing task
        is cancelled so we don't leak a background coroutine.
        """
        assert self._stop_event is not None  # noqa: S101
        stop_wait = asyncio.create_task(self._stop_event.wait())
        timer = asyncio.create_task(asyncio.sleep(seconds))
        try:
            await asyncio.wait({stop_wait, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (stop_wait, timer):
                if not task.done():
                    task.cancel()
                    # Swallow the CancelledError so the loop body
                    # continues cleanly.
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass

    async def _alert_dlq(self, event: SettlementEvent, error: str) -> None:
        """Fire-and-forget HTTP POST to the operator-configured DLQ
        webhook. Best-effort: a webhook failure must not propagate
        into the worker loop — the row is already marked dead.

        Why ``event.attempts + 1`` in the reported count:
        ``event.attempts`` is the value the in-memory event carried
        at claim time — i.e. BEFORE the just-completed attempt. The
        preceding ``mark_dead`` call incremented attempts server-side
        (PG row now reports ``attempts + 1``), but the in-memory
        ``event`` is not re-fetched (avoiding another SQL just for
        a log field). The ``+1`` reconciles the two so the webhook
        payload matches what an operator querying the dead row
        would see.
        """
        if not self._dlq_alert_webhook:
            logger.error(
                "settlement_outbox_event_dead_no_webhook",
                event_id=event.event_id,
                task_id=event.task_id,
                # See method docstring for the ``+1`` rationale.
                attempts=event.attempts + 1,
                last_error=error[:500],
            )
            return
        try:
            if self._dlq_http_client is None:
                self._dlq_http_client = httpx.AsyncClient(timeout=5.0)
            await self._dlq_http_client.post(
                self._dlq_alert_webhook,
                json={
                    "event_id": event.event_id,
                    "task_id": event.task_id,
                    "trigger": event.trigger,
                    # See method docstring for the ``+1`` rationale.
                    "attempts": event.attempts + 1,
                    "last_error": error[:1000],
                    "step_status": event.step_status,
                },
            )
            logger.info(
                "settlement_outbox_dlq_webhook_sent",
                event_id=event.event_id,
                task_id=event.task_id,
            )
        except Exception as exc:  # noqa: BLE001 — webhook failure is non-critical
            # When the webhook is misconfigured (dead Slack link, rate
            # limit, typo), this log line is effectively the operator's
            # last visible signal that an event died — the event itself
            # is already marked dead in the outbox row, but pulling it
            # out for triage means another SQL query. Inline the dead
            # event's task / attempts / underlying error here so the
            # operator can act on a single log row.
            logger.error(
                "settlement_outbox_dlq_webhook_failed",
                event_id=event.event_id,
                task_id=event.task_id,
                attempts=event.attempts + 1,
                last_error=error[:500],
                webhook_error=str(exc),
                webhook_error_type=type(exc).__name__,
            )
            # Also bump the generic errors_total counter so the
            # operator's "anything broken?" panel lights up even if
            # the DLQ-specific webhook is the broken thing.
            if self._metrics is not None:
                try:
                    await self._metrics.inc_error_count(
                        error_type="dlq_webhook_failed",
                        component="settlement_worker",
                    )
                except Exception:  # noqa: BLE001 — never let metrics block teardown
                    pass
