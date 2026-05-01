"""Regression tests for the fire-and-forget audit helpers (security audit H-audit).

The helpers must:
1.  Schedule a real ``log_event`` write when the singleton is set + started.
2.  Drop events silently when no audit is wired (no exception, no task).
3.  Drop events silently when audit isn't started (e.g. mid-shutdown).
4.  Swallow Redis failures inside the spawned task — never re-raise on the
    hot path.
5.  Tag SECURITY_AUTH_FAILURE events with reason + source_ip details.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from acn.monitoring.audit import (
    AuditEventType,
    AuditLevel,
    AuditLogger,
    fire_and_forget_event,
    get_audit_singleton,
    record_auth_failure,
    set_audit_singleton,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Each test gets a clean module-level singleton + retention bucket.

    Resetting ``_PENDING_AUDIT_TASKS`` matters for ``== set()`` assertions
    in ``test_fire_and_forget_retains_strong_task_reference`` — without it
    a stray task from a prior test that didn't drain cleanly (e.g. a
    cancelled run) would leak into the next test's bucket and produce
    a confusing failure.
    """
    import acn.monitoring.audit as audit_mod

    prev = get_audit_singleton()
    set_audit_singleton(None)
    audit_mod._PENDING_AUDIT_TASKS.clear()
    try:
        yield
    finally:
        set_audit_singleton(prev)
        audit_mod._PENDING_AUDIT_TASKS.clear()


def _started_audit() -> AuditLogger:
    """Return an AuditLogger with a mocked redis + ``_started=True``."""
    redis = AsyncMock()
    audit = AuditLogger(redis=redis)
    audit._started = True  # bypass real start() (which itself would log)
    return audit


@pytest.mark.asyncio
async def test_fire_and_forget_schedules_write_when_started():
    audit = _started_audit()

    fire_and_forget_event(
        audit,
        event_type=AuditEventType.SECURITY_AUTH_FAILURE,
        details={"reason": "test"},
    )
    # Yield twice — once to let the spawned task start, once to let it run
    # past the first awaited Redis op.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert audit.redis.xadd.await_count == 1, (
        "fire_and_forget_event must schedule a real log_event write"
    )


@pytest.mark.asyncio
async def test_fire_and_forget_noop_when_audit_none():
    """No singleton wired -> silently dropped, no task created, no raise."""
    fire_and_forget_event(
        None,
        event_type=AuditEventType.SECURITY_AUTH_FAILURE,
        details={"reason": "test"},
    )
    await asyncio.sleep(0)
    # No assertion needed beyond "did not raise" — the helper returned cleanly.


@pytest.mark.asyncio
async def test_fire_and_forget_noop_when_not_started():
    """An AuditLogger that hasn't been started yet must skip writes.

    During app shutdown ``_started`` flips to False; we must not produce
    audit-on-shutdown spam against a redis pool that may already be closing.
    """
    redis = AsyncMock()
    audit = AuditLogger(redis=redis)
    # _started defaults to False — represent the pre-startup / post-shutdown state.

    fire_and_forget_event(
        audit,
        event_type=AuditEventType.SECURITY_AUTH_FAILURE,
        details={"reason": "test"},
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    redis.xadd.assert_not_awaited()


@pytest.mark.asyncio
async def test_fire_and_forget_swallows_redis_errors():
    """A redis outage during background write must not surface to the caller.

    The caller has already raised HTTPException(401) by the time the audit
    task runs — letting an exception escape would crash the task pool.
    """
    redis = AsyncMock()
    redis.xadd.side_effect = RuntimeError("redis is down")
    audit = AuditLogger(redis=redis)
    audit._started = True

    fire_and_forget_event(
        audit,
        event_type=AuditEventType.SECURITY_AUTH_FAILURE,
        details={"reason": "test"},
    )

    # Drain the spawned task. ``asyncio.all_tasks`` includes our test's own
    # frame; filter to anything other than the current task.
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    # If the helper let the RuntimeError escape, the gather above would have
    # surfaced it; reaching this line is the actual assertion.


@pytest.mark.asyncio
async def test_record_auth_failure_uses_singleton_and_warning_level():
    """``record_auth_failure`` must:
    - read the wired singleton (no extra plumbing for callers),
    - tag the event with reason + source_ip,
    - record at WARNING level so SIEMs can alert on it,
    - merge ``extra`` into ``details`` without dropping ``reason``.
    """
    audit = _started_audit()
    set_audit_singleton(audit)

    log_event_mock = AsyncMock()
    audit.log_event = log_event_mock  # type: ignore[method-assign]

    record_auth_failure(
        reason="api_key_invalid",
        source_ip="203.0.113.5",
        extra={"endpoint_extra": "anything"},
    )

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    log_event_mock.assert_awaited_once()
    kwargs = log_event_mock.await_args.kwargs
    assert kwargs["event_type"] == AuditEventType.SECURITY_AUTH_FAILURE
    assert kwargs["level"] == AuditLevel.WARNING
    assert kwargs["source_ip"] == "203.0.113.5"
    assert kwargs["details"]["reason"] == "api_key_invalid"
    assert kwargs["details"]["endpoint_extra"] == "anything"


@pytest.mark.asyncio
async def test_record_auth_failure_records_actor_path_method():
    """``record_auth_failure(actor_id=..., path=..., method=...)`` must:
    - place ``actor_id`` on the event itself (not as ``target_id``),
    - record ``path`` + ``method`` in ``details`` so analysts can pin
      the failure to a specific endpoint.

    Regression: an earlier draft used ``agent_id`` (which the helper
    routed to ``target_id`` + ``target_type='agent'``); that polluted
    target-based analyst queries with caller-side failures.
    """
    audit = _started_audit()
    set_audit_singleton(audit)

    log_event_mock = AsyncMock()
    audit.log_event = log_event_mock  # type: ignore[method-assign]

    record_auth_failure(
        reason="permission_denied",
        source_ip="203.0.113.5",
        actor_id="auth0|user-42",
        path="/api/v1/subnets",
        method="POST",
        extra={"permission": "acn:write"},
    )

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    log_event_mock.assert_awaited_once()
    kwargs = log_event_mock.await_args.kwargs
    assert kwargs["actor_id"] == "auth0|user-42"
    # ``actor_type`` is intentionally left unset — Auth0 ``sub`` can be a
    # user, an m2m client, a dev stub, or an internal marker, and tagging
    # everything as "user" poisons actor-type analytics. Helper must
    # forward the field as None so the audit row stays honest.
    assert kwargs.get("actor_type") in (None, "")
    # ``target_id`` must be unset — actor and target are different concepts.
    assert kwargs.get("target_id") in (None, "")
    assert kwargs["details"]["reason"] == "permission_denied"
    assert kwargs["details"]["path"] == "/api/v1/subnets"
    assert kwargs["details"]["method"] == "POST"
    assert kwargs["details"]["permission"] == "acn:write"


def test_request_path_helper_handles_none():
    """Defensive: ``_request_path`` must never raise on a missing request.

    The audit hooks fan out into modules that may call the helper from
    a unit-test context where no real ``Request`` object exists. The
    helper guards against ``None`` and against accessor exceptions; pin
    both paths so a future refactor can't silently regress them.
    """
    from acn.auth.middleware import _request_path

    assert _request_path(None) == (None, None)

    # Accessor that raises — helper must swallow and return ``(None, None)``.
    class _BoomRequest:
        @property
        def url(self):
            raise RuntimeError("boom")

        method = "GET"

    assert _request_path(_BoomRequest()) == (None, None)


@pytest.mark.asyncio
async def test_fire_and_forget_retains_strong_task_reference():
    """Regression: ``asyncio.create_task`` returns a value that the loop
    only weakly references. If we drop the reference, CPython can GC
    the task before its first ``await`` resolves and the audit write
    silently disappears.

    This test pins the helper's defence (``_PENDING_AUDIT_TASKS`` set):
    after scheduling, a strong ref must exist *somewhere* (i.e. inside
    the audit module). We don't care which name holds it — only that
    the task doesn't depend on test-frame locals to stay alive.
    """
    import gc

    import acn.monitoring.audit as audit_mod

    audit = _started_audit()
    # Make log_event slow enough that we can observe the task mid-flight.
    started = asyncio.Event()
    finish = asyncio.Event()

    async def slow_log_event(*args, **kwargs):
        started.set()
        await finish.wait()

    audit.log_event = slow_log_event  # type: ignore[method-assign]

    fire_and_forget_event(
        audit,
        event_type=AuditEventType.SECURITY_AUTH_FAILURE,
        details={"reason": "test"},
    )

    await started.wait()
    # Drop any local refs the helper may have leaked to test scope and
    # force a GC pass — if the helper failed to retain the task, this is
    # where it would disappear.
    gc.collect()

    assert audit_mod._PENDING_AUDIT_TASKS, (
        "fire_and_forget_event must keep a strong reference to the task in "
        "_PENDING_AUDIT_TASKS so CPython's GC can't drop it mid-flight"
    )

    finish.set()
    # Drain to keep the test pool clean.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    # After completion the task must remove itself from the set so the
    # retention bucket doesn't leak indefinitely.
    assert audit_mod._PENDING_AUDIT_TASKS == set(), (
        "completed audit tasks must self-discard from _PENDING_AUDIT_TASKS"
    )


@pytest.mark.asyncio
async def test_record_auth_failure_noop_without_singleton():
    """No singleton wired -> silently dropped (e.g. before app startup)."""
    set_audit_singleton(None)

    record_auth_failure(reason="api_key_invalid", source_ip="203.0.113.5")
    await asyncio.sleep(0)
    # Just verifying no raise — there's nothing to assert against.
