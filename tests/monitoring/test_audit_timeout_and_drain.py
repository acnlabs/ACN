"""Regression tests for P2-#1: per-call timeout + shutdown drain.

Two new defences on the fire-and-forget audit path:

1. ``_safe_write`` wraps the underlying ``log_event`` in
   ``asyncio.wait_for(..., timeout=2)``. Without this, a hung Redis call
   pins one ``_PENDING_AUDIT_TASKS`` entry indefinitely; under credential
   stuffing + Redis stall, the set grows unbounded.

2. ``drain_pending_audit_tasks(timeout)`` lets lifespan teardown await
   in-flight writes for a grace window before the loop closes them. We
   want events that were *already accepted* not to vanish on graceful
   restart, while still bounding shutdown time.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

import acn.monitoring.audit as audit_mod
from acn.monitoring.audit import (
    AuditEventType,
    AuditLogger,
    drain_pending_audit_tasks,
    fire_and_forget_event,
    get_audit_singleton,
    set_audit_singleton,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    prev = get_audit_singleton()
    set_audit_singleton(None)
    audit_mod._PENDING_AUDIT_TASKS.clear()
    try:
        yield
    finally:
        set_audit_singleton(prev)
        audit_mod._PENDING_AUDIT_TASKS.clear()


def _started_audit() -> AuditLogger:
    redis = AsyncMock()
    audit = AuditLogger(redis=redis)
    audit._started = True
    return audit


@pytest.mark.asyncio
async def test_safe_write_timeout_caps_runaway_log_event(monkeypatch):
    """A log_event that hangs forever must be capped by ``wait_for``.

    Drives the timeout config down to a tiny value so the test runs in
    ms; the production constant is intentionally larger.
    """
    monkeypatch.setattr(audit_mod, "_AUDIT_WRITE_TIMEOUT_S", 0.05)

    audit = _started_audit()

    async def _hangs(*args, **kwargs):
        await asyncio.sleep(10)  # would dominate test runtime without timeout

    audit.log_event = _hangs  # type: ignore[method-assign]

    started = time.monotonic()
    fire_and_forget_event(
        audit,
        event_type=AuditEventType.SECURITY_AUTH_FAILURE,
        details={"reason": "test"},
    )
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, (
        f"timeout failed to fire — task ran for {elapsed:.2f}s, "
        "indicates the helper is no longer wrapping log_event in wait_for"
    )
    assert audit_mod._PENDING_AUDIT_TASKS == set(), (
        "timed-out task must self-discard from _PENDING_AUDIT_TASKS"
    )


@pytest.mark.asyncio
async def test_safe_write_normal_path_unchanged():
    """Fast log_event must still complete normally (timeout doesn't shadow happy path)."""
    audit = _started_audit()
    log_event_mock = AsyncMock()
    audit.log_event = log_event_mock  # type: ignore[method-assign]

    fire_and_forget_event(
        audit,
        event_type=AuditEventType.AGENT_REGISTERED,
        target_id="agent-1",
    )
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    log_event_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_drain_returns_zero_when_no_pending():
    drained, dropped = await drain_pending_audit_tasks(timeout=0.1)
    assert (drained, dropped) == (0, 0)


@pytest.mark.asyncio
async def test_drain_awaits_in_flight_writes_within_timeout():
    """Tasks already started must be awaited to completion.

    Pins the "graceful restart doesn't lose accepted events" property.
    """
    audit = _started_audit()

    completed = asyncio.Event()
    started_event = asyncio.Event()

    async def _fast_log(*args, **kwargs):
        started_event.set()
        await asyncio.sleep(0.05)
        completed.set()

    audit.log_event = _fast_log  # type: ignore[method-assign]

    fire_and_forget_event(
        audit,
        event_type=AuditEventType.SECURITY_AUTH_FAILURE,
        details={"reason": "test"},
    )
    await started_event.wait()
    assert audit_mod._PENDING_AUDIT_TASKS, "task must be tracked while running"

    drained, dropped = await drain_pending_audit_tasks(timeout=2.0)
    assert (drained, dropped) == (1, 0)
    assert completed.is_set(), "drain must give the task time to flush its write"


@pytest.mark.asyncio
async def test_drain_reports_dropped_when_timeout_exceeded():
    """A task slower than the drain budget shows up under ``dropped``.

    The drain must still return promptly so lifespan teardown isn't held
    hostage by one stuck task.
    """
    audit = _started_audit()

    async def _slow(*args, **kwargs):
        await asyncio.sleep(5)  # outlives the 0.1 s drain budget

    audit.log_event = _slow  # type: ignore[method-assign]

    fire_and_forget_event(
        audit,
        event_type=AuditEventType.SECURITY_AUTH_FAILURE,
        details={"reason": "test"},
    )
    # Yield so the task at least enters its first await.
    await asyncio.sleep(0)
    started = time.monotonic()
    drained, dropped = await drain_pending_audit_tasks(timeout=0.1)
    elapsed = time.monotonic() - started

    assert dropped == 1
    assert drained == 0
    assert elapsed < 0.5, (
        f"drain didn't honour its timeout — took {elapsed:.2f}s"
    )

    # Cleanup: cancel the still-running task so the test pool is clean.
    for t in list(audit_mod._PENDING_AUDIT_TASKS):
        t.cancel()
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
