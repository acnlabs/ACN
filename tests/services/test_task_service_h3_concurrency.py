"""H3 security tests: state-machine transitions are concurrency-safe.

Pre-launch audit finding H3: ``complete_task`` / ``reject_task`` /
``cancel_task`` previously did

    1. ``task = await get_task()`` — read state
    2. in-memory ``task.complete()`` — checks ``status == SUBMITTED``
    3. ``await save(task)`` — unconditional write
    4. side effects (payment release, reward distribution, escrow refund)

Two concurrent callers both pass the in-memory check at step 2 and both
reach step 4 — *double payment*. The fix replaces step 3 with
``compare_and_save(task, expected_status=...)`` which performs an atomic
``UPDATE ... WHERE status=?`` (PG) or a Lua-CAS (Redis) and reports
whether the caller won the race. Side effects only run on a winning CAS;
the loser short-circuits with the latest persisted state for idempotent
semantics.

These tests pin down that contract by mocking the repository and asserting:
* CAS won → side effects execute exactly once
* CAS lost → side effects do not execute and the latest task is returned
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from acn.core.entities.task import Task, TaskStatus
from acn.core.interfaces.task_repository import ITaskRepository
from acn.infrastructure.task_pool import TaskPool
from acn.services.task_service import TaskService


def _submitted_task(**overrides) -> Task:
    """Single-participant task already in SUBMITTED state."""
    defaults = {
        "task_id": "task-h3",
        "creator_type": "human",
        "creator_id": "creator-1",
        "creator_name": "Creator",
        "title": "H3",
        "description": "concurrency test",
        "reward": "10",
        "reward_currency": "ap_points",
        "max_participants": 1,
        "status": TaskStatus.SUBMITTED,
        "assignee_id": "agent-1",
        "assignee_name": "Solver",
        "submission": "did the thing",
        "submitted_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Task(**defaults)


@pytest.fixture
def repo() -> AsyncMock:
    return AsyncMock(spec=ITaskRepository)


@pytest.fixture
def pool(repo) -> AsyncMock:
    p = AsyncMock(spec=TaskPool)
    p.repository = repo
    p.record_completion = AsyncMock()
    return p


@pytest.fixture
def service(repo, pool) -> TaskService:
    # Keep payment/reward/escrow paths off so we can assert they are *not*
    # called rather than mock around them.
    return TaskService(
        repository=repo,
        task_pool=pool,
        payment_manager=None,
        webhook_service=None,
        activity_service=None,
        escrow_client=None,
        agent_repository=None,
    )


# ─────────────────────────────────────────────
# complete_task
# ─────────────────────────────────────────────


class TestCompleteTaskCAS:
    async def test_winning_cas_runs_side_effects_once(
        self, service: TaskService, repo: AsyncMock, pool: AsyncMock
    ) -> None:
        task = _submitted_task()
        repo.find_by_id.return_value = task
        repo.compare_and_save.return_value = True

        await service.complete_task(
            task_id="task-h3", approver_id="creator-1", notes="ok"
        )

        repo.compare_and_save.assert_awaited_once()
        cas_kwargs = repo.compare_and_save.await_args.kwargs
        assert cas_kwargs["expected_status"] == TaskStatus.SUBMITTED, (
            "complete_task must CAS on SUBMITTED so concurrent reject "
            "or cancel cannot be silently overwritten"
        )
        pool.record_completion.assert_awaited_once_with("task-h3", "agent-1")

    async def test_losing_cas_does_not_record_completion_or_reward(
        self, service: TaskService, repo: AsyncMock, pool: AsyncMock
    ) -> None:
        """Concurrent caller arrives second: CAS returns False, no side effects."""
        task = _submitted_task()
        # find_by_id is called twice: once at entry, once for the idempotent
        # "return latest" branch after losing the race.
        winner_view = _submitted_task(status=TaskStatus.COMPLETED)
        repo.find_by_id.side_effect = [task, winner_view]
        repo.compare_and_save.return_value = False

        result = await service.complete_task(
            task_id="task-h3", approver_id="creator-1", notes="ok"
        )

        assert result.status == TaskStatus.COMPLETED, (
            "Loser of the CAS race must return the winner's state, not raise"
        )
        # critical: side effects must NOT fire — that's the whole point
        pool.record_completion.assert_not_awaited()
        # save() (the unconditional one) must also not be called — only CAS
        repo.save.assert_not_awaited()

    async def test_non_creator_is_rejected_before_cas(
        self, service: TaskService, repo: AsyncMock
    ) -> None:
        task = _submitted_task()
        repo.find_by_id.return_value = task

        with pytest.raises(PermissionError):
            await service.complete_task(
                task_id="task-h3", approver_id="not-creator", notes="x"
            )

        repo.compare_and_save.assert_not_awaited()

    async def test_losing_cas_does_not_release_payment(
        self, repo: AsyncMock, pool: AsyncMock
    ) -> None:
        """Strong proof that the loser of a complete-vs-complete race never
        invokes the payment release. ``payment_manager=None`` in the default
        fixture would not prove this — a mocked manager does."""
        payment_manager = AsyncMock()
        svc = TaskService(
            repository=repo,
            task_pool=pool,
            payment_manager=payment_manager,
            webhook_service=None,
            activity_service=None,
            escrow_client=None,
            agent_repository=None,
        )

        task = _submitted_task(payment_task_id="pay-1")
        winner_view = _submitted_task(status=TaskStatus.COMPLETED)
        repo.find_by_id.side_effect = [task, winner_view]
        repo.compare_and_save.return_value = False

        result = await svc.complete_task(
            task_id="task-h3", approver_id="creator-1", notes="ok"
        )

        assert result.status == TaskStatus.COMPLETED
        payment_manager.update_status.assert_not_awaited()


# ─────────────────────────────────────────────
# reject_task
# ─────────────────────────────────────────────


class TestRejectTaskCAS:
    async def test_winning_cas_persists_rejection(
        self, service: TaskService, repo: AsyncMock
    ) -> None:
        task = _submitted_task()
        repo.find_by_id.return_value = task
        repo.compare_and_save.return_value = True

        await service.reject_task(
            task_id="task-h3", reviewer_id="creator-1", notes="nope"
        )

        repo.compare_and_save.assert_awaited_once()
        assert (
            repo.compare_and_save.await_args.kwargs["expected_status"]
            == TaskStatus.SUBMITTED
        )

    async def test_losing_cas_returns_latest_without_double_writing(
        self, service: TaskService, repo: AsyncMock
    ) -> None:
        task = _submitted_task()
        winner_view = _submitted_task(status=TaskStatus.COMPLETED)
        repo.find_by_id.side_effect = [task, winner_view]
        repo.compare_and_save.return_value = False

        result = await service.reject_task(
            task_id="task-h3", reviewer_id="creator-1"
        )

        assert result.status == TaskStatus.COMPLETED
        repo.save.assert_not_awaited()


# ─────────────────────────────────────────────
# cancel_task
# ─────────────────────────────────────────────


class TestCancelTaskCAS:
    async def test_cancel_uses_pre_transition_status_as_cas_expectation(
        self, service: TaskService, repo: AsyncMock
    ) -> None:
        """cancel can fire from many states (OPEN/IN_PROGRESS/SUBMITTED/REJECTED).
        We capture the CURRENT status as the CAS expectation so a concurrent
        approve-then-cancel can't refund a task that was already paid out."""
        task = _submitted_task(status=TaskStatus.IN_PROGRESS)
        repo.find_by_id.return_value = task
        repo.compare_and_save.return_value = True

        await service.cancel_task(task_id="task-h3", canceller_id="creator-1")

        cas_kwargs = repo.compare_and_save.await_args.kwargs
        assert cas_kwargs["expected_status"] == TaskStatus.IN_PROGRESS

    async def test_cancel_already_cancelled_is_idempotent_noop(
        self, service: TaskService, repo: AsyncMock
    ) -> None:
        task = _submitted_task(status=TaskStatus.CANCELLED)
        repo.find_by_id.return_value = task

        result = await service.cancel_task(
            task_id="task-h3", canceller_id="creator-1"
        )

        assert result.status == TaskStatus.CANCELLED
        # No CAS, no save — the early-return path leaves storage alone.
        repo.compare_and_save.assert_not_awaited()
        repo.save.assert_not_awaited()

    async def test_losing_cas_does_not_refund_escrow(
        self, repo: AsyncMock, pool: AsyncMock
    ) -> None:
        """If a concurrent complete_task already moved the task to COMPLETED,
        cancel must lose the CAS and NOT issue an escrow refund — otherwise
        the agent would receive both the reward AND the creator would get
        their escrow back."""
        # Inject a real escrow mock so we can prove .refund() is *not* awaited.
        # (Using escrow_client=None would only prove nothing was attempted on
        # a None object, not that the CAS branch correctly skipped refund.)
        escrow = AsyncMock()
        svc = TaskService(
            repository=repo,
            task_pool=pool,
            payment_manager=None,
            webhook_service=None,
            activity_service=None,
            escrow_client=escrow,
            agent_repository=None,
        )

        task = _submitted_task(
            status=TaskStatus.IN_PROGRESS,
            reward="10",
            reward_currency="ap_points",
            total_budget="10",
        )
        winner_view = _submitted_task(status=TaskStatus.COMPLETED)
        repo.find_by_id.side_effect = [task, winner_view]
        repo.compare_and_save.return_value = False

        result = await svc.cancel_task(task_id="task-h3", canceller_id="creator-1")

        assert result.status == TaskStatus.COMPLETED
        escrow.refund.assert_not_awaited()

    async def test_losing_cas_in_multi_participant_does_not_cancel_participations(
        self, repo: AsyncMock, pool: AsyncMock
    ) -> None:
        """Regression: ``batch_cancel_participations`` previously ran BEFORE
        the CAS, so a cancel that lost the race would still flip every
        participation to CANCELLED, leaving ``task=COMPLETED`` next to
        ``participations=all CANCELLED`` — a torn state that downstream
        listings can't make sense of. After the H3 follow-up fix,
        participations are only batch-cancelled when the CAS wins."""
        multi_task = Task(
            task_id="task-h3-multi",
            creator_type="human",
            creator_id="creator-1",
            creator_name="Creator",
            title="multi",
            description="multi",
            reward="0",
            reward_currency="ap_points",
            max_participants=5,  # _is_multi() == True
            status=TaskStatus.IN_PROGRESS,
        )
        winner_view = Task(**{**multi_task.to_dict(), "status": TaskStatus.COMPLETED})
        repo.find_by_id.side_effect = [multi_task, winner_view]
        repo.compare_and_save.return_value = False

        svc = TaskService(
            repository=repo,
            task_pool=pool,
            payment_manager=None,
            webhook_service=None,
            activity_service=None,
            escrow_client=None,
            agent_repository=None,
        )

        await svc.cancel_task(task_id="task-h3-multi", canceller_id="creator-1")

        pool.batch_cancel_participations.assert_not_awaited()


# ─────────────────────────────────────────────
# Repository contract — interface enforces compare_and_save
# ─────────────────────────────────────────────


class TestRepositoryContract:
    """If a repository drops compare_and_save, every state-machine route
    is regressed silently. Pin the abstract method down."""

    def test_interface_declares_compare_and_save(self) -> None:
        import inspect

        assert "compare_and_save" in dir(ITaskRepository)
        sig = inspect.signature(ITaskRepository.compare_and_save)
        assert "expected_status" in sig.parameters, (
            "compare_and_save must take expected_status — without it "
            "callers can't express the precondition"
        )

    def test_postgres_repo_implements_it(self) -> None:
        from acn.infrastructure.persistence.postgres.task_repository import (
            PostgresTaskRepository,
        )

        assert "compare_and_save" in dir(PostgresTaskRepository)
        # Verify it's a coroutine function, not just an inherited stub
        import inspect

        method = PostgresTaskRepository.compare_and_save
        assert inspect.iscoroutinefunction(method)

    def test_redis_repo_implements_it(self) -> None:
        from acn.infrastructure.persistence.redis.task_repository import (
            RedisTaskRepository,
        )

        assert "compare_and_save" in dir(RedisTaskRepository)
        import inspect

        method = RedisTaskRepository.compare_and_save
        assert inspect.iscoroutinefunction(method)
