"""Settlement reconciliation job (Saga v0.1, plan §6.3).

Purpose
-------
The settlement saga writes two side effects that *must* stay in lockstep:

1. ``settlement_outbox`` rows transition to ``state='done'`` after the
   final step (``reputation_write``) succeeds.
2. ``reputation_events`` rows are inserted by that same final step.

If the worker ever drops an event mid-saga without rolling back the
``state='done'`` mark (impossible by design, but the worker is the
only writer of that boolean), or if the producer silently mints
``reputation_write='skipped'`` events (intentional for refund flows,
but should match a known small fraction), the two counts diverge.

This reconciler computes the delta over a sliding window and exposes
it as a Prometheus gauge. Operators page on any non-zero value
sustained for more than one window.

Why a separate service, not part of the worker
----------------------------------------------
The worker runs per replica and per minute — it cannot answer
"in the last 24 hours, did the saga's two effects match?". A
dedicated cron job that runs daily (or every ``window_seconds``)
and reads point-in-time totals is the cleanest signal. Keeping it
outside the worker also means a worker crash cannot mask a
divergence: the reconciler reads the same tables independently.

Standing audit, not a one-shot gate
-----------------------------------
This reconciler was originally introduced as the gate for the
double-write cleanup (Todo 7, completed). After that PR landed
the saga became the sole writer of settlement side effects, and
``acn_settlement_reconcile_delta`` became the standing audit
between the two persistent tables. A non-zero delta is now the
first signal of saga drift in production — either a saga step
silently failed without DLQ, or a reputation write was lost.

Testability
-----------
``SettlementReconciler.run_once`` takes both repositories and a
``clock`` callable, so tests can fix ``now`` and assert the
emitted metric value deterministically. See
``tests/services/test_settlement_reconciler.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from ..core.interfaces.reputation_repository import IReputationRepository
    from ..core.interfaces.settlement_outbox_repository import (
        ISettlementOutboxRepository,
    )
    from ..monitoring.metrics import MetricsCollector

logger = structlog.get_logger()


class ReconcileResult(BaseModel):
    """Outcome of one reconciliation window. Returned by
    :meth:`SettlementReconciler.run_once` so callers (and tests)
    can introspect; the metric emission is a side effect.
    """

    since: datetime = Field(..., description="Lower bound (UTC) of the window.")
    until: datetime = Field(..., description="Upper bound (UTC) of the window.")
    saga_done_count: int = Field(
        ...,
        description=(
            "Number of outbox rows that reached ``state='done'`` with "
            "``trigger='review_pass'`` inside the window."
        ),
    )
    feedback_count: int = Field(
        ...,
        description=(
            "Number of ``reputation_events`` rows with ``kind='feedback'`` "
            "(``include_smoke_test=False``) created inside the window."
        ),
    )

    @property
    def delta(self) -> int:
        """``saga_done_count - feedback_count``. Zero means the saga's
        two persistent side effects are in lockstep for the window.
        Positive values mean reputation rows are missing; negative
        means the saga somehow under-counted (should be impossible —
        triage as a producer bug).
        """
        return self.saga_done_count - self.feedback_count


class SettlementReconciler:
    """Cross-checks ``settlement_outbox`` against ``reputation_events``.

    Usage:

    .. code-block:: python

        reconciler = SettlementReconciler(
            outbox=outbox_repo,
            reputation=reputation_repo,
            metrics_collector=metrics,
        )
        result = await reconciler.run_once(window_seconds=86_400)

    Schedule ``run_once`` from a cron / async loop on a cadence
    matching the window — daily is the default in the API lifespan.
    """

    def __init__(
        self,
        *,
        outbox: ISettlementOutboxRepository,
        reputation: IReputationRepository,
        metrics_collector: MetricsCollector | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Build a reconciler.

        Args:
            outbox: Outbox repository — the reconciler uses
                ``count_done_since`` with ``trigger='review_pass'``.
            reputation: Reputation repository — the reconciler uses
                ``count_kind_since`` with ``kind='feedback'``.
            metrics_collector: Optional metrics sink. When ``None``
                the result is still logged and returned but no
                gauge is published. Mirrors the same conditional
                wiring as ``SettlementWorker`` so tests can opt out.
            clock: Time source for the "now" upper bound of each
                window. Defaults to ``datetime.now(UTC)``. Tests
                pass a deterministic clock so ``since/until`` are
                reproducible.
        """
        self._outbox = outbox
        self._reputation = reputation
        self._metrics = metrics_collector
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run_with_retry(
        self,
        *,
        window_seconds: int = 86_400,
        max_retries: int = 3,
        retry_backoff_sec: float = 300.0,
    ) -> ReconcileResult | None:
        """``run_once`` wrapped with bounded short retries for
        transient DB / metrics blips.

        Why this lives in the reconciler (not the lifespan loop):
        the reconciler interval in production is 24 hours, so a
        single PG hiccup at the moment of ``run_once`` would lose
        an entire day of reconciliation signal. A 5-minute retry
        with 3 attempts catches the common failure modes (PgBouncer
        idle-connection-kill, transient pool exhaustion) without
        creating a thundering herd against a truly broken DB. After
        ``max_retries`` we give up and the next scheduled tick
        retries from scratch.

        Args:
            window_seconds: Same as :meth:`run_once`.
            max_retries: Total attempts (including the first). 3 is
                empirically enough for transient PG blips while
                bounded enough that an extended outage doesn't
                burn 24 hours of retries.
            retry_backoff_sec: Constant delay between attempts.
                Fixed (not exponential) because the upstream
                interval already provides the long timescale.

        Returns:
            The :class:`ReconcileResult` from a successful attempt,
            or ``None`` if every attempt failed (the caller's
            scheduler should wait for the next interval rather than
            tightening the loop).
        """
        last_exc: BaseException | None = None
        for attempt in range(1, max_retries + 1):
            try:
                return await self.run_once(window_seconds=window_seconds)
            except Exception as exc:  # noqa: BLE001 — keep retry loop alive
                last_exc = exc
                logger.warning(
                    "settlement_reconcile_attempt_failed",
                    attempt=attempt,
                    max_retries=max_retries,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                if attempt < max_retries:
                    await asyncio.sleep(retry_backoff_sec)
        logger.error(
            "settlement_reconcile_exhausted",
            max_retries=max_retries,
            last_error=str(last_exc) if last_exc else None,
        )
        return None

    async def run_once(self, *, window_seconds: int = 86_400) -> ReconcileResult:
        """Compute the saga / reputation count delta over the trailing
        window and publish it to the metrics sink.

        Args:
            window_seconds: Width of the trailing window in seconds.
                Default 24h matches the daily cron cadence used in
                production. Tests pass shorter windows.

        Returns:
            :class:`ReconcileResult` for the window. The caller may
            inspect ``result.delta`` to log custom alerts; the
            reconciler itself only logs at WARNING level when the
            delta is non-zero.
        """
        until = self._clock()
        since = until - timedelta(seconds=window_seconds)

        # The two count queries run sequentially (not gather()) so a
        # failure in one is reported with the partial context of the
        # other already in scope. The cost is negligible — both
        # complete in tens of ms on the indexed tables.
        saga_done = await self._outbox.count_done_since(since, trigger="review_pass")
        feedback = await self._reputation.count_kind_since(
            "feedback", since, include_smoke_test=False
        )

        result = ReconcileResult(
            since=since,
            until=until,
            saga_done_count=saga_done,
            feedback_count=feedback,
        )

        if result.delta == 0:
            logger.info(
                "settlement_reconcile_ok",
                since=since.isoformat(),
                until=until.isoformat(),
                window_seconds=window_seconds,
                count=saga_done,
            )
        else:
            # The reconciler does NOT page directly — the operator's
            # alert is driven off ``acn_settlement_reconcile_delta``
            # in Prometheus. This log line is the human-readable
            # twin so triage runbooks have something to search for.
            logger.warning(
                "settlement_reconcile_delta",
                since=since.isoformat(),
                until=until.isoformat(),
                window_seconds=window_seconds,
                saga_done=saga_done,
                feedback=feedback,
                delta=result.delta,
            )

        # Emit the gauge unconditionally — operators want to see the
        # zero value too so they can confirm the job is alive.
        if self._metrics is not None:
            try:
                await self._metrics.set_gauge(
                    "settlement_reconcile_delta",
                    float(result.delta),
                )
            except Exception as exc:  # noqa: BLE001 — metrics never crash the job
                logger.warning(
                    "settlement_reconcile_metric_failed",
                    error=str(exc),
                )

        return result
