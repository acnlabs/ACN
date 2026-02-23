"""Unit Tests for TaskService — Multi-Participant Flows

Tests business logic for join, submit, review, and cancel participation
using mocked dependencies (no Redis, no network).
"""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from acn.core.entities.task import (
    Participation,
    ParticipationStatus,
    Task,
    TaskMode,
    TaskStatus,
)
from acn.core.interfaces.task_repository import ITaskRepository
from acn.infrastructure.task_pool import TaskPool
from acn.services.task_service import TaskService

# ============================================================================
# Helpers
# ============================================================================


def _make_task(**overrides) -> Task:
    defaults = {
        "task_id": "task-001",
        "mode": TaskMode.OPEN,
        "creator_type": "human",
        "creator_id": "creator-001",
        "creator_name": "Alice",
        "title": "Test Multi Task",
        "description": "A multi-participant task",
        "reward_amount": "50",
        "reward_currency": "points",
        "is_multi_participant": True,
        "max_completions": 5,
    }
    defaults.update(overrides)
    return Task(**defaults)


def _make_participation(**overrides) -> Participation:
    defaults = {
        "participation_id": "part-001",
        "task_id": "task-001",
        "participant_id": "agent-001",
        "participant_name": "Bot-1",
        "participant_type": "agent",
        "status": ParticipationStatus.ACTIVE,
        "joined_at": datetime(2025, 6, 1),
    }
    defaults.update(overrides)
    return Participation(**defaults)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_repo():
    """Mock ITaskRepository"""
    repo = AsyncMock(spec=ITaskRepository)
    return repo


@pytest.fixture
def mock_task_pool(mock_repo):
    """Mock TaskPool wrapping a mock repository"""
    pool = AsyncMock(spec=TaskPool)
    pool.repository = mock_repo
    return pool


@pytest.fixture
def service(mock_repo, mock_task_pool):
    """TaskService with mocked dependencies"""
    svc = TaskService(
        repository=mock_repo,
        task_pool=mock_task_pool,
        # Disable optional integrations (escrow, wallet, etc.)
        payment_manager=None,
        webhook_service=None,
        activity_service=None,
        escrow_client=None,
        agent_repository=None,
        wallet_client=None,
    )
    return svc


# ============================================================================
# accept_task — Multi-Participant Join
# ============================================================================


class TestAcceptTaskMultiParticipant:
    """Test accept_task branching into multi-participant join flow"""

    async def test_join_creates_participation(self, service, mock_repo, mock_task_pool):
        """accept_task on multi-participant task creates participation"""
        task = _make_task()
        mock_repo.find_by_id.return_value = task
        mock_task_pool.join_task.return_value = "part-new-001"
        # After join, return updated task
        updated_task = _make_task(active_participants_count=1)
        mock_repo.find_by_id.side_effect = [task, updated_task]

        result_task, pid = await service.accept_task(
            task_id="task-001",
            agent_id="agent-001",
            agent_name="Bot-1",
            agent_type="agent",
        )

        assert pid == "part-new-001"
        mock_task_pool.join_task.assert_awaited_once()
        call_kwargs = mock_task_pool.join_task.call_args
        assert call_kwargs.kwargs["task_id"] == "task-001"
        assert call_kwargs.kwargs["max_completions"] == 5

    async def test_accept_single_participant_returns_none_pid(self, service, mock_repo, mock_task_pool):
        """accept_task on single-participant task returns pid=None"""
        task = _make_task(is_multi_participant=False, is_repeatable=False)
        # override __post_init__ side effect
        task.is_multi_participant = False
        task.is_repeatable = False
        mock_repo.find_by_id.return_value = task
        mock_repo.save.return_value = None
        mock_task_pool.has_agent_completed.return_value = False

        result_task, pid = await service.accept_task(
            task_id="task-001",
            agent_id="agent-001",
            agent_name="Bot-1",
        )

        assert pid is None
        mock_task_pool.join_task.assert_not_awaited()


# ============================================================================
# submit_task — Multi-Participant Submit
# ============================================================================


class TestSubmitTaskMultiParticipant:
    """Test submit_task for multi-participant tasks"""

    async def test_submit_with_explicit_participation_id(self, service, mock_repo, mock_task_pool):
        """submit_task with participation_id resolves and submits"""
        task = _make_task(approval_type="manual")
        p = _make_participation()
        mock_repo.find_by_id.return_value = task
        mock_task_pool.get_participation.return_value = p

        await service.submit_task(
            task_id="task-001",
            agent_id="agent-001",
            submission="Here is my work",
            participation_id="part-001",
        )

        # Participation should have been submitted
        assert p.status == ParticipationStatus.SUBMITTED
        assert p.submission == "Here is my work"
        mock_repo.save_participation.assert_awaited_once_with(p)

    async def test_submit_auto_find_participation(self, service, mock_repo, mock_task_pool):
        """submit_task without participation_id auto-finds active participation"""
        task = _make_task(approval_type="manual")
        p = _make_participation()
        mock_repo.find_by_id.return_value = task
        mock_task_pool.get_user_participation.return_value = p

        await service.submit_task(
            task_id="task-001",
            agent_id="agent-001",
            submission="Auto-found work",
        )

        assert p.status == ParticipationStatus.SUBMITTED
        mock_task_pool.get_user_participation.assert_awaited_once()

    async def test_submit_wrong_owner_raises(self, service, mock_repo, mock_task_pool):
        """submit_task with wrong agent for participation raises PermissionError"""
        task = _make_task()
        p = _make_participation(participant_id="agent-999")
        mock_repo.find_by_id.return_value = task
        mock_task_pool.get_participation.return_value = p

        with pytest.raises(PermissionError, match="belongs to another"):
            await service.submit_task(
                task_id="task-001",
                agent_id="agent-001",
                submission="work",
                participation_id="part-001",
            )

    async def test_submit_auto_approval(self, service, mock_repo, mock_task_pool):
        """submit_task with auto-approval triggers _auto_complete_participation"""
        task = _make_task(approval_type="auto")
        p = _make_participation()
        mock_repo.find_by_id.return_value = task
        mock_task_pool.get_participation.return_value = p
        mock_task_pool.complete_participation.return_value = 1
        mock_task_pool.record_completion.return_value = None

        await service.submit_task(
            task_id="task-001",
            agent_id="agent-001",
            submission="Auto work",
            participation_id="part-001",
        )

        mock_task_pool.complete_participation.assert_awaited_once()


# ============================================================================
# review_participation
# ============================================================================


class TestReviewParticipation:
    """Test review_participation (approve / reject)"""

    async def test_approve_participation(self, service, mock_repo, mock_task_pool):
        """Approving a participation completes it and records completion"""
        task = _make_task()
        p = _make_participation(status=ParticipationStatus.SUBMITTED)
        mock_repo.find_by_id.return_value = task
        mock_task_pool.get_participation.return_value = p
        mock_task_pool.complete_participation.return_value = 1
        mock_task_pool.record_completion.return_value = None

        await service.review_participation(
            task_id="task-001",
            approver_id="creator-001",
            approved=True,
            participation_id="part-001",
            notes="Good work",
        )

        mock_task_pool.complete_participation.assert_awaited_once()
        mock_task_pool.record_completion.assert_awaited_once()

    async def test_reject_participation(self, service, mock_repo, mock_task_pool):
        """Rejecting a participation sets status to rejected"""
        task = _make_task()
        p = _make_participation(status=ParticipationStatus.SUBMITTED)
        mock_repo.find_by_id.return_value = task
        mock_task_pool.get_participation.return_value = p

        await service.review_participation(
            task_id="task-001",
            approver_id="creator-001",
            approved=False,
            participation_id="part-001",
            notes="Incomplete",
        )

        assert p.status == ParticipationStatus.REJECTED
        assert p.rejection_reason == "Incomplete"
        mock_repo.save_participation.assert_awaited_once()

    async def test_review_not_creator_raises(self, service, mock_repo, mock_task_pool):
        """Non-creator cannot review"""
        task = _make_task()
        mock_repo.find_by_id.return_value = task

        with pytest.raises(PermissionError, match="Only the task creator"):
            await service.review_participation(
                task_id="task-001",
                approver_id="hacker-001",
                approved=True,
                participation_id="part-001",
            )

    async def test_approve_exhausts_task(self, service, mock_repo, mock_task_pool):
        """When max_completions reached, task is COMPLETED and remaining are cancelled"""
        task = _make_task(max_completions=3)
        p = _make_participation(status=ParticipationStatus.SUBMITTED)
        mock_repo.find_by_id.return_value = task
        mock_task_pool.get_participation.return_value = p
        mock_task_pool.complete_participation.return_value = 3  # reached max
        mock_task_pool.record_completion.return_value = None
        mock_task_pool.batch_cancel_participations.return_value = 2

        await service.review_participation(
            task_id="task-001",
            approver_id="creator-001",
            approved=True,
            participation_id="part-001",
        )

        mock_task_pool.batch_cancel_participations.assert_awaited_once_with("task-001")
        # Task should be saved as COMPLETED
        mock_repo.save.assert_awaited()
        saved_task = mock_repo.save.call_args[0][0]
        assert saved_task.status == TaskStatus.COMPLETED

    async def test_review_delegates_to_single_participant(self, service, mock_repo, mock_task_pool):
        """For single-participant task, review_participation delegates to complete_task"""
        task = _make_task(is_multi_participant=False, is_repeatable=False)
        task.is_multi_participant = False
        task.is_repeatable = False
        task.status = TaskStatus.SUBMITTED
        mock_repo.find_by_id.return_value = task
        mock_repo.save.return_value = None
        mock_task_pool.record_completion.return_value = None

        # This should internally call complete_task instead of working with participations
        await service.review_participation(
            task_id="task-001",
            approver_id="creator-001",
            approved=True,
        )

        # Should NOT use participation methods
        mock_task_pool.get_participation.assert_not_awaited()
        mock_task_pool.complete_participation.assert_not_awaited()


# ============================================================================
# cancel_participation
# ============================================================================


class TestCancelParticipation:
    """Test cancel_participation"""

    async def test_cancel_by_participant(self, service, mock_repo, mock_task_pool):
        """Participant can cancel their own participation"""
        p = _make_participation()
        mock_task_pool.get_participation.return_value = p
        mock_task_pool.cancel_participation.return_value = None
        mock_repo.find_by_id.return_value = _make_task()

        await service.cancel_participation(
            task_id="task-001",
            participation_id="part-001",
            canceller_id="agent-001",
        )

        mock_task_pool.cancel_participation.assert_awaited_once_with("part-001", "task-001")

    async def test_cancel_by_other_user_raises(self, service, mock_repo, mock_task_pool):
        """Non-participant cannot cancel someone else's participation"""
        p = _make_participation(participant_id="agent-001")
        mock_task_pool.get_participation.return_value = p

        with pytest.raises(PermissionError, match="Only the participant"):
            await service.cancel_participation(
                task_id="task-001",
                participation_id="part-001",
                canceller_id="agent-999",
            )

    async def test_cancel_nonexistent_raises(self, service, mock_repo, mock_task_pool):
        """Cannot cancel a nonexistent participation"""
        mock_task_pool.get_participation.return_value = None

        with pytest.raises(ValueError, match="not found"):
            await service.cancel_participation(
                task_id="task-001",
                participation_id="part-nonexistent",
                canceller_id="agent-001",
            )

    async def test_cancel_wrong_task_raises(self, service, mock_repo, mock_task_pool):
        """Cannot cancel participation from a different task"""
        p = _make_participation(task_id="other-task")
        mock_task_pool.get_participation.return_value = p

        with pytest.raises(ValueError, match="does not belong"):
            await service.cancel_participation(
                task_id="task-001",
                participation_id="part-001",
                canceller_id="agent-001",
            )


# ============================================================================
# _resolve_participation
# ============================================================================


class TestResolveParticipation:
    """Test _resolve_participation helper"""

    async def test_resolve_by_explicit_id(self, service, mock_task_pool):
        """Resolves by explicit participation_id"""
        p = _make_participation()
        mock_task_pool.get_participation.return_value = p

        result = await service._resolve_participation("task-001", "agent-001", "part-001")
        assert result.participation_id == "part-001"

    async def test_resolve_auto_find(self, service, mock_task_pool):
        """Auto-finds user's active participation when no ID given"""
        p = _make_participation()
        mock_task_pool.get_user_participation.return_value = p

        result = await service._resolve_participation("task-001", "agent-001", None)
        assert result.participation_id == "part-001"
        mock_task_pool.get_user_participation.assert_awaited_once()

    async def test_resolve_no_active_raises(self, service, mock_task_pool):
        """Raises when no active participation found"""
        mock_task_pool.get_user_participation.return_value = None

        with pytest.raises(ValueError, match="No active participation"):
            await service._resolve_participation("task-001", "agent-001", None)

    async def test_resolve_wrong_owner_raises(self, service, mock_task_pool):
        """Raises when participation belongs to another user"""
        p = _make_participation(participant_id="agent-999")
        mock_task_pool.get_participation.return_value = p

        with pytest.raises(PermissionError, match="belongs to another"):
            await service._resolve_participation("task-001", "agent-001", "part-001")
