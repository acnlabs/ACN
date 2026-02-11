"""Unit Tests for Task & Participation Entities

Tests pure business logic for multi-participant task support,
including Participation lifecycle, Task.can_join(), and backward compatibility.
"""

from datetime import datetime

import pytest

from acn.core.entities.task import (
    Participation,
    ParticipationStatus,
    Task,
    TaskMode,
    TaskStatus,
)


# ============================================================================
# Helpers
# ============================================================================


def _make_task(**overrides) -> Task:
    """Factory for a minimal valid Task"""
    defaults = dict(
        task_id="task-001",
        mode=TaskMode.OPEN,
        creator_type="human",
        creator_id="creator-001",
        creator_name="Alice",
        title="Test Task",
        description="A test task",
        reward_amount="100",
    )
    defaults.update(overrides)
    return Task(**defaults)


def _make_participation(**overrides) -> Participation:
    """Factory for a minimal valid Participation"""
    defaults = dict(
        participation_id="part-001",
        task_id="task-001",
        participant_id="agent-001",
        participant_name="Bot-1",
        participant_type="agent",
    )
    defaults.update(overrides)
    return Participation(**defaults)


# ============================================================================
# Participation Entity Tests
# ============================================================================


class TestParticipation:
    """Test Participation domain entity"""

    def test_creation_defaults(self):
        """Test creating a participation with defaults"""
        p = _make_participation()

        assert p.participation_id == "part-001"
        assert p.task_id == "task-001"
        assert p.participant_id == "agent-001"
        assert p.status == ParticipationStatus.ACTIVE
        assert p.submission is None
        assert p.completed_at is None

    # ── Submit ──

    def test_submit_from_active(self):
        """ACTIVE → SUBMITTED on submit"""
        p = _make_participation()
        p.submit("Here is my work", [{"url": "https://example.com/file"}])

        assert p.status == ParticipationStatus.SUBMITTED
        assert p.submission == "Here is my work"
        assert len(p.submission_artifacts) == 1
        assert p.submitted_at is not None

    def test_submit_from_wrong_status_raises(self):
        """Cannot submit when not ACTIVE"""
        p = _make_participation(status=ParticipationStatus.SUBMITTED)
        with pytest.raises(ValueError, match="Cannot submit"):
            p.submit("work")

    # ── Complete ──

    def test_complete_from_submitted(self):
        """SUBMITTED → COMPLETED on complete"""
        p = _make_participation(status=ParticipationStatus.SUBMITTED)
        p.complete(reviewer_id="reviewer-1", notes="LGTM")

        assert p.status == ParticipationStatus.COMPLETED
        assert p.reviewed_by == "reviewer-1"
        assert p.review_notes == "LGTM"
        assert p.completed_at is not None

    def test_complete_from_wrong_status_raises(self):
        """Cannot complete when not SUBMITTED"""
        p = _make_participation(status=ParticipationStatus.ACTIVE)
        with pytest.raises(ValueError, match="Cannot complete"):
            p.complete()

    # ── Reject ──

    def test_reject_from_submitted(self):
        """SUBMITTED → REJECTED on reject"""
        p = _make_participation(status=ParticipationStatus.SUBMITTED)
        p.reject(reviewer_id="reviewer-1", reason="Incomplete")

        assert p.status == ParticipationStatus.REJECTED
        assert p.rejection_reason == "Incomplete"
        assert p.rejected_at is not None

    def test_reject_from_wrong_status_raises(self):
        """Cannot reject when not SUBMITTED"""
        p = _make_participation(status=ParticipationStatus.ACTIVE)
        with pytest.raises(ValueError, match="Cannot reject"):
            p.reject()

    # ── Cancel ──

    def test_cancel_from_active(self):
        """ACTIVE → CANCELLED on cancel"""
        p = _make_participation()
        p.cancel()

        assert p.status == ParticipationStatus.CANCELLED
        assert p.cancelled_at is not None

    def test_cancel_from_submitted(self):
        """SUBMITTED → CANCELLED (e.g. creator cancels task)"""
        p = _make_participation(status=ParticipationStatus.SUBMITTED)
        p.cancel()
        assert p.status == ParticipationStatus.CANCELLED

    def test_cancel_from_rejected(self):
        """REJECTED → CANCELLED (withdraw after rejection)"""
        p = _make_participation(status=ParticipationStatus.REJECTED)
        p.cancel()
        assert p.status == ParticipationStatus.CANCELLED

    def test_cancel_completed_raises(self):
        """Cannot cancel COMPLETED participation"""
        p = _make_participation(status=ParticipationStatus.COMPLETED)
        with pytest.raises(ValueError, match="Cannot cancel"):
            p.cancel()

    def test_cancel_already_cancelled_raises(self):
        """Cannot cancel CANCELLED participation"""
        p = _make_participation(status=ParticipationStatus.CANCELLED)
        with pytest.raises(ValueError, match="Cannot cancel"):
            p.cancel()

    # ── Resubmit ──

    def test_resubmit_from_rejected(self):
        """REJECTED → SUBMITTED on resubmit"""
        p = _make_participation(
            status=ParticipationStatus.REJECTED,
            rejection_reason="Incomplete",
            rejected_at=datetime(2025, 1, 1),
            reviewed_by="reviewer-1",
        )
        p.resubmit("Updated work")

        assert p.status == ParticipationStatus.SUBMITTED
        assert p.submission == "Updated work"
        assert p.rejection_reason is None
        assert p.rejected_at is None
        assert p.reviewed_by is None

    def test_resubmit_from_wrong_status_raises(self):
        """Cannot resubmit when not REJECTED"""
        p = _make_participation(status=ParticipationStatus.ACTIVE)
        with pytest.raises(ValueError, match="Cannot resubmit"):
            p.resubmit("work")

    # ── Full Lifecycle ──

    def test_full_lifecycle_happy_path(self):
        """active → submitted → completed"""
        p = _make_participation()

        p.submit("My work")
        assert p.status == ParticipationStatus.SUBMITTED

        p.complete(reviewer_id="r1")
        assert p.status == ParticipationStatus.COMPLETED

    def test_full_lifecycle_reject_resubmit(self):
        """active → submitted → rejected → resubmitted → completed"""
        p = _make_participation()

        p.submit("First attempt")
        p.reject(reason="Missing docs")
        assert p.status == ParticipationStatus.REJECTED

        p.resubmit("Second attempt with docs")
        assert p.status == ParticipationStatus.SUBMITTED

        p.complete()
        assert p.status == ParticipationStatus.COMPLETED

    def test_full_lifecycle_cancel(self):
        """active → cancelled"""
        p = _make_participation()
        p.cancel()
        assert p.status == ParticipationStatus.CANCELLED

    # ── Serialization ──

    def test_to_dict(self):
        """Test participation serialization"""
        p = _make_participation()
        d = p.to_dict()

        assert d["participation_id"] == "part-001"
        assert d["task_id"] == "task-001"
        assert d["participant_id"] == "agent-001"
        assert d["status"] == "active"
        assert d["submission"] is None
        assert d["completed_at"] is None

    def test_from_dict_round_trip(self):
        """Test dict → Participation → dict round trip"""
        original = _make_participation()
        original.submit("work")
        original.complete(reviewer_id="r1", notes="ok")

        d = original.to_dict()
        restored = Participation.from_dict(d)

        assert restored.participation_id == original.participation_id
        assert restored.status == ParticipationStatus.COMPLETED
        assert restored.submission == "work"
        assert restored.reviewed_by == "r1"
        assert restored.completed_at is not None

    def test_new_id_is_unique(self):
        """Each call to new_id() returns a unique UUID"""
        ids = {Participation.new_id() for _ in range(100)}
        assert len(ids) == 100


# ============================================================================
# Task Entity — Multi-Participant Tests
# ============================================================================


class TestTaskMultiParticipant:
    """Test Task multi-participant support"""

    # ── Backward Compatibility ──

    def test_is_repeatable_implies_is_multi_participant(self):
        """is_repeatable=True should auto-set is_multi_participant=True"""
        t = _make_task(is_repeatable=True)
        assert t.is_multi_participant is True
        assert t.is_repeatable is True

    def test_is_multi_participant_implies_is_repeatable(self):
        """is_multi_participant=True should auto-set is_repeatable=True"""
        t = _make_task(is_multi_participant=True)
        assert t.is_repeatable is True

    def test_neither_flag_set(self):
        """Default: both flags are False"""
        t = _make_task()
        assert t.is_multi_participant is False
        assert t.is_repeatable is False

    # ── can_join() ──

    def test_can_join_multi_participant_open(self):
        """Multi-participant OPEN task allows joining"""
        t = _make_task(is_multi_participant=True, max_completions=10)
        assert t.can_join() is True

    def test_can_join_not_multi_participant(self):
        """Single-participant task cannot be joined"""
        t = _make_task(is_multi_participant=False)
        assert t.can_join() is False

    def test_can_join_non_open_status(self):
        """Cannot join a task that's not OPEN"""
        t = _make_task(is_multi_participant=True, status=TaskStatus.CANCELLED)
        assert t.can_join() is False

    def test_can_join_at_capacity(self):
        """Cannot join when completed + active >= max_completions"""
        t = _make_task(
            is_multi_participant=True,
            max_completions=5,
            completed_count=3,
            active_participants_count=2,
        )
        assert t.can_join() is False

    def test_can_join_under_capacity(self):
        """Can join when completed + active < max_completions"""
        t = _make_task(
            is_multi_participant=True,
            max_completions=5,
            completed_count=2,
            active_participants_count=1,
        )
        assert t.can_join() is True

    def test_can_join_unlimited(self):
        """Can always join when max_completions is None (unlimited)"""
        t = _make_task(
            is_multi_participant=True,
            max_completions=None,
            completed_count=999,
            active_participants_count=100,
        )
        assert t.can_join() is True

    # ── can_be_accepted() delegates to can_join() for multi ──

    def test_can_be_accepted_delegates_to_can_join(self):
        """For multi-participant tasks, can_be_accepted() uses can_join()"""
        t = _make_task(is_multi_participant=True, max_completions=10)
        assert t.can_be_accepted() is True  # same as can_join()

        t.status = TaskStatus.CANCELLED
        assert t.can_be_accepted() is False

    # ── to_dict() includes new fields ──

    def test_to_dict_multi_participant_fields(self):
        """to_dict() includes multi-participant fields"""
        t = _make_task(
            is_multi_participant=True,
            allow_repeat_by_same=True,
            active_participants_count=3,
            max_completions=10,
            completed_count=2,
        )
        d = t.to_dict()

        assert d["is_multi_participant"] is True
        assert d["allow_repeat_by_same"] is True
        assert d["active_participants_count"] == 3
        assert d["max_completions"] == 10
        assert d["completed_count"] == 2
        # backward compat
        assert d["is_repeatable"] is True


# ============================================================================
# Task Entity — Existing Behavior (Sanity)
# ============================================================================


class TestTaskBasic:
    """Sanity checks for existing Task behavior"""

    def test_create_valid_task(self):
        t = _make_task()
        assert t.task_id == "task-001"
        assert t.status == TaskStatus.OPEN
        assert t.mode == TaskMode.OPEN

    def test_validation_empty_id(self):
        with pytest.raises(ValueError, match="task_id cannot be empty"):
            _make_task(task_id="")

    def test_validation_empty_title(self):
        with pytest.raises(ValueError, match="title cannot be empty"):
            _make_task(title="")

    def test_validation_empty_creator(self):
        with pytest.raises(ValueError, match="creator_id cannot be empty"):
            _make_task(creator_id="")

    def test_accept_single_participant(self):
        """Single-participant accept flow"""
        t = _make_task()
        t.accept("agent-1", "Bot-1")
        assert t.status == TaskStatus.IN_PROGRESS
        assert t.assignee_id == "agent-1"

    def test_submit_then_complete(self):
        """Standard single-participant lifecycle"""
        t = _make_task()
        t.accept("agent-1", "Bot-1")
        t.submit("My work")
        assert t.status == TaskStatus.SUBMITTED

        t.complete(reviewer_id="reviewer-1")
        assert t.status == TaskStatus.COMPLETED
        assert t.completed_count == 1

    def test_submit_then_reject(self):
        """Reject submission"""
        t = _make_task()
        t.accept("agent-1", "Bot-1")
        t.submit("Bad work")
        t.reject(reviewer_id="r1", notes="Not acceptable")
        assert t.status == TaskStatus.REJECTED

    def test_cancel_task(self):
        """Cancel an open task"""
        t = _make_task()
        t.cancel()
        assert t.status == TaskStatus.CANCELLED

    def test_cancel_completed_raises(self):
        """Cannot cancel completed task"""
        t = _make_task()
        t.accept("a1", "Bot")
        t.submit("w")
        t.complete()
        with pytest.raises(ValueError, match="Cannot cancel completed"):
            t.cancel()

    def test_from_dict_round_trip(self):
        """Task dict → from_dict → to_dict round trip"""
        original = _make_task(
            is_multi_participant=True,
            allow_repeat_by_same=True,
            max_completions=5,
        )
        d = original.to_dict()
        restored = Task.from_dict(d)

        assert restored.task_id == original.task_id
        assert restored.is_multi_participant is True
        assert restored.allow_repeat_by_same is True
        assert restored.max_completions == 5
