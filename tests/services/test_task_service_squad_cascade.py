"""Task termination → ``task_scoped`` subnet cascade unit tests
(ADR-0003 Phase 3).

Pins the cascade hook behaviour on the three task-terminal paths
(``complete_task`` / ``reject_task`` / ``cancel_task``):

* Matching ``task_scoped`` subnet is dissolved via
  ``SubnetService.delete_subnet(..., owner="system")``.
* A subnet whose ``linked_task_id`` doesn't match is left untouched.
* A ``persistent`` subnet that happens to carry the same
  ``linked_task_id`` is filtered out by the cascade — only
  ``lifecycle == "task_scoped"`` is dissolved.
* Cascade failure logs a warning but does **NOT** roll back the
  task transition: the task remains in its terminal state and
  the method returns normally.
* Cascade runs **after** the settlement-Saga side effects
  (``_notify_webhook`` / ``activity.record_*`` / escrow refund).
  We pin the ordering by recording every awaited call onto a
  shared ``MagicMock`` and asserting the call sequence.

Mocks all external dependencies (no Redis / PG / network). Forces
the legacy non-saga path on ``complete_task`` by leaving
``settlement_outbox`` / ``unit_of_work`` as ``None`` — exercising
``_dissolve_task_scoped_subnets`` is what this file is about, not
the saga branch (which is covered in
``test_task_service_settlement.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.entities import Subnet
from acn.core.entities.task import Task, TaskStatus
from acn.core.exceptions import SubnetNotFoundException
from acn.core.interfaces import IAgentRepository, ISubnetRepository, ITaskRepository
from acn.infrastructure.task_pool import TaskPool
from acn.services.subnet_service import SubnetService
from acn.services.task_service import TaskService

# ---------------------------------------------------------------------------
# Entity builders — keep the test data minimal so each test reads top-down
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str = "task-42",
    *,
    creator_id: str = "creator-001",
    assignee_id: str | None = "agent-001",
    status: TaskStatus = TaskStatus.SUBMITTED,
) -> Task:
    """Build a Task that takes the simple path through every terminal
    method: no payment, no escrow currency, single-participant."""
    return Task(
        task_id=task_id,
        creator_type="human",
        creator_id=creator_id,
        creator_name="Alice",
        title=f"Task {task_id}",
        description="Test task",
        # ``"usd"`` is NOT in PLATFORM_CURRENCIES — skips reward
        # distribution / escrow refund and keeps the test mock surface tiny.
        reward="50",
        reward_currency="usd",
        max_participants=1,
        assignee_id=assignee_id,
        status=status,
    )


def _make_subnet(
    subnet_id: str,
    *,
    owner: str = "alice",
    lifecycle: str = "task_scoped",
    linked_task_id: str | None = "task-42",
    parent_subnet_id: str | None = "parent-1",
) -> Subnet:
    return Subnet(
        subnet_id=subnet_id,
        name=subnet_id,
        owner=owner,
        parent_subnet_id=parent_subnet_id,
        lifecycle=lifecycle,  # type: ignore[arg-type]
        linked_task_id=linked_task_id,
        member_agent_ids={owner},
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Fixtures — shared TaskService wired with cascade dependencies
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_task_repo() -> AsyncMock:
    return AsyncMock(spec=ITaskRepository)


@pytest.fixture
def mock_task_pool(mock_task_repo) -> AsyncMock:
    pool = AsyncMock(spec=TaskPool)
    pool.repository = mock_task_repo
    return pool


@pytest.fixture
def mock_subnet_repository() -> AsyncMock:
    return AsyncMock(spec=ISubnetRepository)


@pytest.fixture
def mock_subnet_service() -> AsyncMock:
    return AsyncMock(spec=SubnetService)


@pytest.fixture
def mock_webhook_service() -> AsyncMock:
    """Stub webhook target — task_service uses ``_notify_webhook`` which
    calls ``webhook.send_event``; we record those for ordering assertions."""
    svc = MagicMock()
    svc.send_event = AsyncMock(return_value=None)
    return svc


@pytest.fixture
def mock_activity_service() -> AsyncMock:
    """ActivityService — also recorded for ordering assertions."""
    svc = AsyncMock()
    svc.record_task_approved = AsyncMock(return_value=None)
    svc.record_task_rejected = AsyncMock(return_value=None)
    svc.record_task_cancelled = AsyncMock(return_value=None)
    return svc


@pytest.fixture
def call_order_recorder() -> MagicMock:
    """Shared sink that all the relevant mocks tee their call markers
    to, so the test can assert relative ordering between webhook,
    activity, and the cascade hook in a single sequence."""
    return MagicMock()


@pytest.fixture
def service(
    mock_task_repo,
    mock_task_pool,
    mock_subnet_repository,
    mock_subnet_service,
    mock_webhook_service,
    mock_activity_service,
    call_order_recorder,
) -> TaskService:
    """TaskService wired with the cascade dependencies.

    ``settlement_outbox`` / ``unit_of_work`` are intentionally left
    ``None`` so ``complete_task`` falls onto the legacy synchronous
    path — what this file's tests exercise. The cascade hook is
    independent of which path produces the CAS save, so the legacy
    branch is sufficient pinning.
    """
    svc = TaskService(
        repository=mock_task_repo,
        task_pool=mock_task_pool,
        payment_manager=None,
        webhook_service=mock_webhook_service,
        activity_service=mock_activity_service,
        escrow_client=None,
        agent_repository=AsyncMock(spec=IAgentRepository),
        subnet_repository=mock_subnet_repository,
        subnet_service=mock_subnet_service,
    )

    # Tee every call we care about (webhook, activity, cascade) onto
    # ``call_order_recorder`` so a single ``mock_calls`` sequence
    # captures the relative ordering between them. Each side-effect
    # also returns whatever the underlying ``AsyncMock`` would normally
    # return (None for these methods).
    def _record(label: str):
        async def _hit(*args, **kwargs):
            call_order_recorder(label)

        return _hit

    mock_webhook_service.send_event.side_effect = _record("webhook")
    mock_activity_service.record_task_approved.side_effect = _record("activity")
    mock_activity_service.record_task_rejected.side_effect = _record("activity")
    mock_activity_service.record_task_cancelled.side_effect = _record("activity")
    mock_subnet_service.delete_subnet.side_effect = _record("cascade")

    return svc


# ---------------------------------------------------------------------------
# complete_task — three sub-cases + ordering + failure
# ---------------------------------------------------------------------------


class TestCompleteTaskCascade:
    """ADR-0003 §3 — task termination on completion dissolves task_scoped subnets."""

    @pytest.mark.asyncio
    async def test_matching_task_scoped_subnet_is_dissolved(
        self,
        service,
        mock_task_repo,
        mock_subnet_repository,
        mock_subnet_service,
    ):
        task = _make_task(task_id="task-42")
        mock_task_repo.find_by_id.return_value = task
        mock_task_repo.compare_and_save.return_value = True
        squad = _make_subnet(
            "squad-1",
            lifecycle="task_scoped",
            linked_task_id="task-42",
        )
        mock_subnet_repository.find_by_linked_task.return_value = [squad]

        result = await service.complete_task(
            task_id="task-42",
            approver_id="creator-001",
        )

        assert result.status == TaskStatus.COMPLETED
        mock_subnet_repository.find_by_linked_task.assert_awaited_once_with("task-42")
        mock_subnet_service.delete_subnet.assert_awaited_once_with(
            "squad-1", owner="system"
        )

    @pytest.mark.asyncio
    async def test_subnet_with_different_linked_task_is_left_intact(
        self,
        service,
        mock_task_repo,
        mock_subnet_repository,
        mock_subnet_service,
    ):
        """``find_by_linked_task`` is keyed on the exact task_id; a
        subnet bound to a different task should not surface here at
        all. We pin the contract by returning an empty result."""
        task = _make_task(task_id="task-42")
        mock_task_repo.find_by_id.return_value = task
        mock_task_repo.compare_and_save.return_value = True
        mock_subnet_repository.find_by_linked_task.return_value = []

        await service.complete_task(
            task_id="task-42",
            approver_id="creator-001",
        )

        mock_subnet_service.delete_subnet.assert_not_called()

    @pytest.mark.asyncio
    async def test_persistent_subnet_with_same_link_is_filtered_out(
        self,
        service,
        mock_task_repo,
        mock_subnet_repository,
        mock_subnet_service,
    ):
        """Belt-and-braces — if a row carries ``linked_task_id ==
        task_id`` but its ``lifecycle`` has somehow been flipped to
        ``persistent`` (out-of-band edit, or a future schema where
        the link survives promotion), the cascade still filters it
        out and only ``task_scoped`` rows are dissolved.
        """
        task = _make_task(task_id="task-42")
        mock_task_repo.find_by_id.return_value = task
        mock_task_repo.compare_and_save.return_value = True
        persistent_match = _make_subnet(
            "persistent-1",
            lifecycle="persistent",
            linked_task_id=None,  # entity invariant — persistent forbids non-None
        )
        # Hand-craft a Subnet that violates the entity invariant on
        # purpose to exercise the lifecycle filter — we bypass
        # ``__post_init__`` via ``object.__setattr__`` on the
        # returned mock subnet rather than constructing one (which
        # would raise).
        rogue = _make_subnet(
            "rogue-persistent",
            lifecycle="task_scoped",
            linked_task_id="task-42",
        )
        # Now flip ``lifecycle`` after construction; setattr on a
        # dataclass instance is legal and does NOT trigger
        # ``__post_init__`` re-validation (see ADR review notes).
        rogue.lifecycle = "persistent"  # type: ignore[assignment]

        squad = _make_subnet(
            "squad-1",
            lifecycle="task_scoped",
            linked_task_id="task-42",
        )
        mock_subnet_repository.find_by_linked_task.return_value = [
            rogue,
            persistent_match,
            squad,
        ]

        await service.complete_task(
            task_id="task-42",
            approver_id="creator-001",
        )

        # Only ``squad-1`` is dissolved; the two persistent rows are
        # skipped by the lifecycle filter.
        mock_subnet_service.delete_subnet.assert_awaited_once_with(
            "squad-1", owner="system"
        )

    @pytest.mark.asyncio
    async def test_cascade_failure_does_not_roll_back_completion(
        self,
        service,
        mock_task_repo,
        mock_subnet_repository,
        mock_subnet_service,
    ):
        task = _make_task(task_id="task-42")
        mock_task_repo.find_by_id.return_value = task
        mock_task_repo.compare_and_save.return_value = True
        mock_subnet_repository.find_by_linked_task.return_value = [
            _make_subnet("squad-1")
        ]
        mock_subnet_service.delete_subnet.side_effect = RuntimeError(
            "redis connection lost"
        )

        # Cascade explodes — but the method returns successfully and
        # the task remains in COMPLETED. Cascade is best-effort.
        result = await service.complete_task(
            task_id="task-42",
            approver_id="creator-001",
        )

        assert result.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_subnet_already_deleted_is_silent_success(
        self,
        service,
        mock_task_repo,
        mock_subnet_repository,
        mock_subnet_service,
    ):
        """Concurrent dissolve race — another path already deleted
        the subnet. ``delete_subnet`` raises
        ``SubnetNotFoundException``; cascade swallows it as a
        successful no-op (ADR §"Idempotency on concurrent dissolution")."""
        task = _make_task(task_id="task-42")
        mock_task_repo.find_by_id.return_value = task
        mock_task_repo.compare_and_save.return_value = True
        mock_subnet_repository.find_by_linked_task.return_value = [
            _make_subnet("squad-gone")
        ]
        mock_subnet_service.delete_subnet.side_effect = SubnetNotFoundException(
            "squad-gone"
        )

        result = await service.complete_task(
            task_id="task-42",
            approver_id="creator-001",
        )

        assert result.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_cascade_runs_after_webhook_and_activity(
        self,
        service,
        mock_task_repo,
        mock_subnet_repository,
        call_order_recorder,
    ):
        """ADR §3 — cascade must fire AFTER the settlement Saga (here:
        platform webhook + activity record). Asserted via the
        relative ordering of recorded marker calls."""
        task = _make_task(task_id="task-42")
        mock_task_repo.find_by_id.return_value = task
        mock_task_repo.compare_and_save.return_value = True
        mock_subnet_repository.find_by_linked_task.return_value = [
            _make_subnet("squad-1")
        ]

        await service.complete_task(
            task_id="task-42",
            approver_id="creator-001",
        )

        labels = [c.args[0] for c in call_order_recorder.call_args_list]
        # Required ordering: webhook → activity → cascade.
        assert labels.index("cascade") > labels.index("webhook")
        assert labels.index("cascade") > labels.index("activity")


# ---------------------------------------------------------------------------
# reject_task — single comprehensive pin
# ---------------------------------------------------------------------------


class TestRejectTaskCascade:
    @pytest.mark.asyncio
    async def test_reject_dissolves_task_scoped_subnet_after_webhook(
        self,
        service,
        mock_task_repo,
        mock_subnet_repository,
        mock_subnet_service,
        call_order_recorder,
    ):
        task = _make_task(task_id="task-42")
        mock_task_repo.find_by_id.return_value = task
        mock_task_repo.compare_and_save.return_value = True
        mock_subnet_repository.find_by_linked_task.return_value = [
            _make_subnet("squad-1"),
        ]

        result = await service.reject_task(
            task_id="task-42",
            reviewer_id="creator-001",
            notes="not good enough",
        )

        assert result.status == TaskStatus.REJECTED
        mock_subnet_service.delete_subnet.assert_awaited_once_with(
            "squad-1", owner="system"
        )
        labels = [c.args[0] for c in call_order_recorder.call_args_list]
        assert labels.index("cascade") > labels.index("webhook")

    @pytest.mark.asyncio
    async def test_reject_cascade_failure_does_not_roll_back(
        self,
        service,
        mock_task_repo,
        mock_subnet_repository,
        mock_subnet_service,
    ):
        task = _make_task(task_id="task-42")
        mock_task_repo.find_by_id.return_value = task
        mock_task_repo.compare_and_save.return_value = True
        mock_subnet_repository.find_by_linked_task.return_value = [
            _make_subnet("squad-1")
        ]
        mock_subnet_service.delete_subnet.side_effect = RuntimeError(
            "transient repo error"
        )

        result = await service.reject_task(
            task_id="task-42",
            reviewer_id="creator-001",
        )
        assert result.status == TaskStatus.REJECTED


# ---------------------------------------------------------------------------
# cancel_task — single comprehensive pin
# ---------------------------------------------------------------------------


class TestCancelTaskCascade:
    @pytest.mark.asyncio
    async def test_cancel_dissolves_task_scoped_subnet_after_webhook(
        self,
        service,
        mock_task_repo,
        mock_subnet_repository,
        mock_subnet_service,
        mock_webhook_service,
        call_order_recorder,
    ):
        # A task in OPEN status is cancelable.
        task = _make_task(task_id="task-42", status=TaskStatus.OPEN, assignee_id=None)
        mock_task_repo.find_by_id.return_value = task
        mock_task_repo.compare_and_save.return_value = True
        mock_subnet_repository.find_by_linked_task.return_value = [
            _make_subnet("squad-1"),
        ]

        result = await service.cancel_task(
            task_id="task-42",
            canceller_id="creator-001",
        )

        assert result.status == TaskStatus.CANCELLED
        mock_subnet_service.delete_subnet.assert_awaited_once_with(
            "squad-1", owner="system"
        )
        labels = [c.args[0] for c in call_order_recorder.call_args_list]
        assert labels.index("cascade") > labels.index("webhook")

    @pytest.mark.asyncio
    async def test_cancel_cascade_failure_does_not_roll_back(
        self,
        service,
        mock_task_repo,
        mock_subnet_repository,
        mock_subnet_service,
    ):
        task = _make_task(task_id="task-42", status=TaskStatus.OPEN, assignee_id=None)
        mock_task_repo.find_by_id.return_value = task
        mock_task_repo.compare_and_save.return_value = True
        mock_subnet_repository.find_by_linked_task.return_value = [
            _make_subnet("squad-1")
        ]
        mock_subnet_service.delete_subnet.side_effect = RuntimeError("boom")

        result = await service.cancel_task(
            task_id="task-42",
            canceller_id="creator-001",
        )
        assert result.status == TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# Defence-in-depth — missing dependencies degrade silently
# ---------------------------------------------------------------------------


class TestCascadeDegradesGracefully:
    @pytest.mark.asyncio
    async def test_no_subnet_repository_skips_cascade(
        self,
        mock_task_repo,
        mock_task_pool,
        mock_webhook_service,
        mock_activity_service,
    ):
        """Legacy fixtures that don't wire ``subnet_repository`` /
        ``subnet_service`` must still complete tasks — the cascade
        is a silent no-op."""
        svc = TaskService(
            repository=mock_task_repo,
            task_pool=mock_task_pool,
            payment_manager=None,
            webhook_service=mock_webhook_service,
            activity_service=mock_activity_service,
            escrow_client=None,
            subnet_repository=None,
            subnet_service=None,
        )
        task = _make_task(task_id="task-42")
        mock_task_repo.find_by_id.return_value = task
        mock_task_repo.compare_and_save.return_value = True

        result = await svc.complete_task(
            task_id="task-42",
            approver_id="creator-001",
        )
        assert result.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_lookup_failure_swallowed(
        self,
        service,
        mock_task_repo,
        mock_subnet_repository,
        mock_subnet_service,
    ):
        """``find_by_linked_task`` exploding (e.g. Redis down at the
        tail of complete) must not break the task transition."""
        task = _make_task(task_id="task-42")
        mock_task_repo.find_by_id.return_value = task
        mock_task_repo.compare_and_save.return_value = True
        mock_subnet_repository.find_by_linked_task.side_effect = RuntimeError(
            "redis down"
        )

        result = await service.complete_task(
            task_id="task-42",
            approver_id="creator-001",
        )

        assert result.status == TaskStatus.COMPLETED
        # No subnet was ever dissolved because lookup never returned.
        mock_subnet_service.delete_subnet.assert_not_called()


# ---------------------------------------------------------------------------
# Multiple matches — cascade walks the full list even when one fails
# ---------------------------------------------------------------------------


class TestCascadeIteratesAllMatches:
    @pytest.mark.asyncio
    async def test_one_failure_does_not_block_others(
        self,
        service,
        mock_task_repo,
        mock_subnet_repository,
        mock_subnet_service,
    ):
        task = _make_task(task_id="task-42")
        mock_task_repo.find_by_id.return_value = task
        mock_task_repo.compare_and_save.return_value = True
        mock_subnet_repository.find_by_linked_task.return_value = [
            _make_subnet("squad-1"),
            _make_subnet("squad-2"),
            _make_subnet("squad-3"),
        ]
        # Second delete fails; first and third still attempted.
        mock_subnet_service.delete_subnet.side_effect = [
            None,
            RuntimeError("flaky"),
            None,
        ]

        result = await service.complete_task(
            task_id="task-42",
            approver_id="creator-001",
        )
        assert result.status == TaskStatus.COMPLETED
        assert mock_subnet_service.delete_subnet.await_count == 3
        attempted_ids = [
            c.args[0] for c in mock_subnet_service.delete_subnet.await_args_list
        ]
        assert attempted_ids == ["squad-1", "squad-2", "squad-3"]
