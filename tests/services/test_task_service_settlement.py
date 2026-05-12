"""Settlement saga × ``complete_task`` integration boundary tests.

The promise of Todo 7 is *no more double-write window*: when the
saga is enabled (production default), ``complete_task`` enqueues an
outbox row and stops — the ``SettlementWorker`` then drives escrow
release + reward distribute + reputation write asynchronously. When
the saga is disabled (``OUTBOX_ENQUEUE_REQUIRED=false`` or Redis-only
deployments where the PG deps aren't wired), the legacy synchronous
inline payment + reward path remains as an in-place rollback lever.

These cases pin that mutual-exclusion contract by mocking the outbox
+ UoW + payment manager and asserting which path actually fires.

Before Todo 7 both paths ran together, which silently relied on
backend + worker idempotency to avoid double-pay. After Todo 7 the
two paths are mutually exclusive — these tests guard that no
regression accidentally re-introduces the double-write.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from acn.core.entities.task import Task, TaskStatus
from acn.core.interfaces.settlement_outbox_repository import (
    ISettlementOutboxRepository,
)
from acn.core.interfaces.task_repository import ITaskRepository
from acn.core.interfaces.unit_of_work import IUnitOfWork
from acn.infrastructure.task_pool import TaskPool
from acn.services.task_service import TaskService


def _submitted_task(**overrides: Any) -> Task:
    """A reward-bearing single-participant task awaiting completion.

    Reward + assignee + ap_points currency are deliberately set so the
    legacy ``_distribute_reward`` branch *would* fire if it ran — that
    lets the saga-on test prove suppression rather than vacuously
    pass.
    """
    defaults: dict[str, Any] = {
        "task_id": "task-saga",
        "creator_type": "human",
        "creator_id": "creator-1",
        "creator_name": "Creator",
        "title": "Saga boundary test",
        "description": "completion fires saga or legacy, never both",
        "reward": "10",
        "reward_currency": "ap_points",
        "max_participants": 1,
        "status": TaskStatus.SUBMITTED,
        "assignee_id": "agent-1",
        "assignee_name": "Solver",
        "submission": "did the thing",
        "submitted_at": datetime.now(UTC),
        "payment_task_id": "pay-saga-1",
    }
    defaults.update(overrides)
    return Task(**defaults)


class _NoopUnitOfWork(IUnitOfWork):
    """Async context manager that yields ``None`` and commits silently.

    Mirrors ``PostgresUnitOfWork`` shape without touching a real DB.
    The session token is never inspected by the fakes / mocks we
    pass into ``TaskService``, so ``None`` is enough.
    """

    @asynccontextmanager
    async def transaction(self) -> Any:
        yield None


@pytest.fixture
def repo() -> AsyncMock:
    r = AsyncMock(spec=ITaskRepository)
    r.compare_and_save.return_value = True
    return r


@pytest.fixture
def pool(repo: AsyncMock) -> AsyncMock:
    p = AsyncMock(spec=TaskPool)
    p.repository = repo
    p.record_completion = AsyncMock()
    return p


@pytest.fixture
def payment_manager() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def outbox() -> AsyncMock:
    o = AsyncMock(spec=ISettlementOutboxRepository)
    o.enqueue.return_value = True
    return o


@pytest.fixture
def uow() -> _NoopUnitOfWork:
    return _NoopUnitOfWork()


# ----------------------------------------------------------------------
# Saga-on path: producer enqueues, inline payment + reward MUST stay quiet
# ----------------------------------------------------------------------


class TestCompleteTaskSagaOn:
    async def test_enqueues_outbox_and_skips_inline_payment_and_reward(
        self,
        repo: AsyncMock,
        pool: AsyncMock,
        payment_manager: AsyncMock,
        outbox: AsyncMock,
        uow: _NoopUnitOfWork,
    ) -> None:
        """Saga path: outbox.enqueue called once; legacy inline payment
        + reward distribute MUST NOT fire — that's the whole point of
        Todo 7.
        """
        task = _submitted_task()
        repo.find_by_id.return_value = task

        # An escrow mock that would explode on any call — proof that
        # the inline ``_distribute_reward`` (which uses ``self.escrow``)
        # never reaches it in the saga-on path.
        escrow = AsyncMock()
        escrow.get_by_task.side_effect = AssertionError(
            "escrow.get_by_task must not be called when saga is enabled — "
            "that would mean the inline _distribute_reward branch ran"
        )

        service = TaskService(
            repository=repo,
            task_pool=pool,
            payment_manager=payment_manager,
            webhook_service=None,
            activity_service=None,
            escrow_client=escrow,
            agent_repository=None,
            settlement_outbox=outbox,
            unit_of_work=uow,
            outbox_enqueue_required=True,
        )

        await service.complete_task(
            task_id="task-saga", approver_id="creator-1", notes="approve"
        )

        outbox.enqueue.assert_awaited_once()
        # The event must carry the producer-computed step_status so the
        # worker doesn't re-derive gating.
        enqueued_event = outbox.enqueue.await_args.args[0]
        assert enqueued_event.trigger == "review_pass"
        assert enqueued_event.task_id == "task-saga"

        payment_manager.update_status.assert_not_awaited()
        escrow.get_by_task.assert_not_awaited()

    async def test_enqueue_failure_rolls_back_no_inline_fallback(
        self,
        repo: AsyncMock,
        pool: AsyncMock,
        payment_manager: AsyncMock,
        outbox: AsyncMock,
        uow: _NoopUnitOfWork,
    ) -> None:
        """If outbox.enqueue raises inside the saga transaction, the
        caller must see the exception. We deliberately do NOT silently
        fall back to the legacy inline path — that would re-introduce
        the double-write window (caller retries -> second enqueue
        succeeds, but legacy already paid).
        """
        task = _submitted_task()
        repo.find_by_id.return_value = task
        outbox.enqueue.side_effect = RuntimeError("simulated PG outage")

        service = TaskService(
            repository=repo,
            task_pool=pool,
            payment_manager=payment_manager,
            webhook_service=None,
            activity_service=None,
            escrow_client=None,
            agent_repository=None,
            settlement_outbox=outbox,
            unit_of_work=uow,
            outbox_enqueue_required=True,
        )

        with pytest.raises(RuntimeError, match="simulated PG outage"):
            await service.complete_task(
                task_id="task-saga", approver_id="creator-1", notes="approve"
            )

        # No inline side effects should have run before the saga
        # transaction blew up — atomicity guarantee.
        payment_manager.update_status.assert_not_awaited()
        pool.record_completion.assert_not_awaited()


# ----------------------------------------------------------------------
# Saga-off path: emergency disarm — inline payment + reward MUST run
# ----------------------------------------------------------------------


class TestCompleteTaskSagaOff:
    async def test_no_outbox_dep_runs_inline_payment(
        self,
        repo: AsyncMock,
        pool: AsyncMock,
        payment_manager: AsyncMock,
    ) -> None:
        """Redis-only / test wiring: no outbox + no UoW injected.
        Legacy inline payment release MUST run — this is the rollback
        lever, the path production used before saga rolled out.
        """
        task = _submitted_task()
        repo.find_by_id.return_value = task

        service = TaskService(
            repository=repo,
            task_pool=pool,
            payment_manager=payment_manager,
            webhook_service=None,
            activity_service=None,
            escrow_client=None,
            agent_repository=None,
            # No settlement_outbox, no unit_of_work → saga disabled
        )

        await service.complete_task(
            task_id="task-saga", approver_id="creator-1", notes="approve"
        )

        payment_manager.update_status.assert_awaited_once_with(
            "pay-saga-1", "completed"
        )

    async def test_explicit_disarm_runs_inline_payment(
        self,
        repo: AsyncMock,
        pool: AsyncMock,
        payment_manager: AsyncMock,
        outbox: AsyncMock,
        uow: _NoopUnitOfWork,
    ) -> None:
        """``OUTBOX_ENQUEUE_REQUIRED=false`` — outbox + UoW are wired
        (PG mode) but the operator pulled the emergency lever.
        Producer must NOT enqueue, inline payment MUST run.
        """
        task = _submitted_task()
        repo.find_by_id.return_value = task

        service = TaskService(
            repository=repo,
            task_pool=pool,
            payment_manager=payment_manager,
            webhook_service=None,
            activity_service=None,
            escrow_client=None,
            agent_repository=None,
            settlement_outbox=outbox,
            unit_of_work=uow,
            outbox_enqueue_required=False,  # explicit disarm
        )

        await service.complete_task(
            task_id="task-saga", approver_id="creator-1", notes="approve"
        )

        outbox.enqueue.assert_not_awaited()
        payment_manager.update_status.assert_awaited_once_with(
            "pay-saga-1", "completed"
        )
