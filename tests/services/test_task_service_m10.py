"""M10 security tests: Escrow-join consistency — compensating rollback.

Problem (M10)
-------------
``_join_task`` commits a Participation row atomically, then calls
``escrow.accept_v2`` **outside** that transaction.  If ``accept_v2``
fails the two subsystems diverge: the task has one active participant
but the escrow is still LOCKED.

Fix
---
On ``accept_v2`` failure the service now:
  1. calls ``task_pool.cancel_participation(pid, task_id)`` to undo
     the join (compensating action),
  2. logs an ERROR-level event,
  3. re-raises the original exception so the caller gets a 500 rather
     than silently succeeding with inconsistent data.

If the compensation call itself fails (double-failure), an additional
``join_rollback_failed_manual_reconciliation_required`` ERROR is logged
and the original exception is still propagated.

``get_by_task`` failures are treated as "no escrow found" (read-only
probe), so they do NOT trigger compensation; the join succeeds without
escrow activation.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from acn.core.entities.task import Task, TaskStatus
from acn.core.interfaces.task_repository import ITaskRepository
from acn.infrastructure.task_pool import TaskPool
from acn.services.task_service import TaskService

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_PLATFORM_CURRENCY = "credits"  # in PLATFORM_CURRENCIES (frozenset({"credits"}))


def _open_task(**overrides: Any) -> Task:
    """An open multi-participant task with a platform-currency reward."""
    defaults: dict[str, Any] = {
        "task_id": "task-m10",
        "creator_type": "human",
        "creator_id": "creator-1",
        "creator_name": "Creator",
        "title": "M10 test task",
        "description": "escrow consistency test",
        "reward": "10",
        "reward_currency": _PLATFORM_CURRENCY,
        "max_participants": 5,
        "status": TaskStatus.OPEN,
    }
    defaults.update(overrides)
    return Task(**defaults)


def _escrow_info(status: str = "locked", escrow_id: str = "esc-1") -> SimpleNamespace:
    return SimpleNamespace(success=True, escrow_id=escrow_id, status=status)


def _build_service(pool: AsyncMock, escrow: Any) -> TaskService:
    repo = AsyncMock(spec=ITaskRepository)
    return TaskService(
        repository=repo,
        task_pool=pool,
        escrow_client=escrow,
    )


# ---------------------------------------------------------------------------
# M10: escrow probe failure → join succeeds (read-only failure is non-fatal)
# ---------------------------------------------------------------------------


class TestM10EscrowProbeFailure:
    """If ``escrow.get_by_task`` raises, the join must still succeed
    (read-only failure — no state has changed in escrow)."""

    @pytest.mark.anyio
    async def test_join_succeeds_when_probe_fails(self):
        pool = AsyncMock(spec=TaskPool)
        pool.join_task = AsyncMock(return_value="pid-1")
        pool.cancel_participation = AsyncMock()

        escrow = AsyncMock()
        escrow.get_by_task.side_effect = RuntimeError("escrow down")

        task = _open_task()
        svc = _build_service(pool, escrow)

        # get_task call after join
        pool.repository = MagicMock()
        with patch.object(svc, "get_task", return_value=task):
            with patch.object(svc, "_notify_webhook", new=AsyncMock()):
                result_task, pid = await svc._join_task(task, "agent-1", "AgentOne")

        assert pid == "pid-1"
        # Compensation must NOT have been called (probe failure is non-fatal)
        pool.cancel_participation.assert_not_called()
        escrow.accept_v2.assert_not_called()


# ---------------------------------------------------------------------------
# M10: accept_v2 failure → compensation rolls back participation
# ---------------------------------------------------------------------------


class TestM10AcceptV2Failure:
    """If ``escrow.accept_v2`` raises after a successful join, the service
    must cancel the participation and re-raise the exception."""

    @pytest.mark.anyio
    async def test_participation_cancelled_on_accept_failure(self):
        pool = AsyncMock(spec=TaskPool)
        pool.join_task = AsyncMock(return_value="pid-rollback")
        pool.cancel_participation = AsyncMock()

        escrow = AsyncMock()
        escrow.get_by_task.return_value = _escrow_info(status="locked")
        escrow.accept_v2.side_effect = RuntimeError("escrow accept failed")

        task = _open_task()
        svc = _build_service(pool, escrow)

        with pytest.raises(RuntimeError, match="escrow accept failed"):
            with patch.object(svc, "get_task", return_value=task):
                with patch.object(svc, "_notify_webhook", new=AsyncMock()):
                    await svc._join_task(task, "agent-1", "AgentOne")

        # Compensation must have been called with the participation ID
        pool.cancel_participation.assert_awaited_once_with("pid-rollback", task.task_id)

    @pytest.mark.anyio
    async def test_original_exception_propagates_after_rollback(self):
        """Caller always sees the escrow exception regardless of compensation
        outcome — no silent success with inconsistent data."""
        pool = AsyncMock(spec=TaskPool)
        pool.join_task = AsyncMock(return_value="pid-x")
        pool.cancel_participation = AsyncMock()

        escrow = AsyncMock()
        escrow.get_by_task.return_value = _escrow_info(status="locked")
        escrow.accept_v2.side_effect = ValueError("backend unavailable")

        task = _open_task()
        svc = _build_service(pool, escrow)

        with pytest.raises(ValueError, match="backend unavailable"):
            with patch.object(svc, "get_task", return_value=task):
                with patch.object(svc, "_notify_webhook", new=AsyncMock()):
                    await svc._join_task(task, "agent-1", "AgentOne")


# ---------------------------------------------------------------------------
# M10: double failure → original exception still propagates
# ---------------------------------------------------------------------------


class TestM10DoubleFailure:
    """If both ``accept_v2`` and the compensating ``cancel_participation``
    fail, the service must still propagate the *original* escrow exception
    so the caller gets a 500 (not a silent success)."""

    @pytest.mark.anyio
    async def test_original_exception_propagates_on_double_failure(self):
        pool = AsyncMock(spec=TaskPool)
        pool.join_task = AsyncMock(return_value="pid-double")
        pool.cancel_participation.side_effect = RuntimeError("cancel failed too")

        escrow = AsyncMock()
        escrow.get_by_task.return_value = _escrow_info(status="locked")
        escrow.accept_v2.side_effect = RuntimeError("accept failed")

        task = _open_task()
        svc = _build_service(pool, escrow)

        with pytest.raises(RuntimeError, match="accept failed"):
            with patch.object(svc, "get_task", return_value=task):
                with patch.object(svc, "_notify_webhook", new=AsyncMock()):
                    await svc._join_task(task, "agent-1", "AgentOne")

        pool.cancel_participation.assert_awaited_once()


# ---------------------------------------------------------------------------
# M10: non-LOCKED escrow → no activation attempted (no compensation needed)
# ---------------------------------------------------------------------------


class TestM10NonLockedEscrow:
    """When the escrow is already ACTIVE (subsequent participant), no
    activation is attempted and no compensation is needed."""

    @pytest.mark.anyio
    async def test_active_escrow_not_accepted_again(self):
        pool = AsyncMock(spec=TaskPool)
        pool.join_task = AsyncMock(return_value="pid-2nd")
        pool.cancel_participation = AsyncMock()

        escrow = AsyncMock()
        escrow.get_by_task.return_value = _escrow_info(status="active")

        task = _open_task()
        svc = _build_service(pool, escrow)

        with patch.object(svc, "get_task", return_value=task):
            with patch.object(svc, "_notify_webhook", new=AsyncMock()):
                result_task, pid = await svc._join_task(task, "agent-2", "AgentTwo")

        escrow.accept_v2.assert_not_called()
        pool.cancel_participation.assert_not_called()
        assert pid == "pid-2nd"


# ---------------------------------------------------------------------------
# M10: non-platform currency → escrow block skipped entirely
# ---------------------------------------------------------------------------


class TestM10NonPlatformCurrency:
    """Tasks with non-platform currencies (e.g. USD) must never touch the
    escrow client, so there is no join-escrow inconsistency possible."""

    @pytest.mark.anyio
    async def test_non_platform_currency_skips_escrow(self):
        pool = AsyncMock(spec=TaskPool)
        pool.join_task = AsyncMock(return_value="pid-usd")
        pool.cancel_participation = AsyncMock()

        escrow = AsyncMock()

        task = _open_task(reward_currency="USD")
        svc = _build_service(pool, escrow)

        with patch.object(svc, "get_task", return_value=task):
            with patch.object(svc, "_notify_webhook", new=AsyncMock()):
                result_task, pid = await svc._join_task(task, "agent-3", "AgentThree")

        escrow.get_by_task.assert_not_called()
        escrow.accept_v2.assert_not_called()
        pool.cancel_participation.assert_not_called()
