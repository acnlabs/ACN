"""Task Domain Entity

Pure business logic for Task and Participation, independent of infrastructure.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4


class TaskMode(StrEnum):
    """Task mode"""

    OPEN = "open"  # Open task, any agent can complete
    ASSIGNED = "assigned"  # Assigned to specific agent


class ApprovalType(StrEnum):
    """Task approval type"""

    MANUAL = "manual"  # Human review required (default)
    AUTO = "auto"  # Auto-approve on submission
    VALIDATOR = "validator"  # Use platform validator (future)
    WEBHOOK = "webhook"  # Call external webhook (future)


class TaskStatus(StrEnum):
    """Task status"""

    OPEN = "open"  # Task is open for acceptance
    ASSIGNED = "assigned"  # Task assigned to an agent
    IN_PROGRESS = "in_progress"  # Agent is working on it
    SUBMITTED = "submitted"  # Result submitted, pending review
    COMPLETED = "completed"  # Approved and done
    REJECTED = "rejected"  # Submission rejected
    CANCELLED = "cancelled"  # Cancelled by creator


class ParticipationStatus(StrEnum):
    """Participation lifecycle status"""

    ACTIVE = "active"  # Participant is working on the task
    SUBMITTED = "submitted"  # Participant submitted, pending review
    COMPLETED = "completed"  # Approved and reward released
    REJECTED = "rejected"  # Submission rejected by creator
    CANCELLED = "cancelled"  # Participant withdrew or timed out


@dataclass
class Participation:
    """
    Participation — tracks one participant's lifecycle within a multi-participant task.

    Each participant independently goes through:
        active → submitted → completed / rejected → (cancelled)
    The parent Task stays OPEN while participations are active.
    """

    participation_id: str
    task_id: str

    # Participant info
    participant_id: str
    participant_name: str
    participant_type: str = "agent"  # "human" or "agent"

    # Lifecycle
    status: ParticipationStatus = ParticipationStatus.ACTIVE
    joined_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Submission
    submission: str | None = None
    submission_artifacts: list[dict] = field(default_factory=list)
    submitted_at: datetime | None = None

    # Review / Rejection (fields moved down from Escrow for per-participation tracking)
    rejection_reason: str | None = None
    rejected_at: datetime | None = None
    reject_response_deadline: datetime | None = None
    review_request_id: str | None = None
    review_notes: str | None = None
    reviewed_by: str | None = None

    # Completion
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None

    def submit(self, submission: str, artifacts: list[dict] | None = None) -> None:
        """Submit work for this participation"""
        if self.status != ParticipationStatus.ACTIVE:
            raise ValueError(f"Cannot submit in status: {self.status}")
        self.submission = submission
        self.submission_artifacts = artifacts or []
        self.submitted_at = datetime.now(UTC)
        self.status = ParticipationStatus.SUBMITTED

    def complete(self, reviewer_id: str | None = None, notes: str | None = None) -> None:
        """Mark participation as completed (approved)"""
        if self.status != ParticipationStatus.SUBMITTED:
            raise ValueError(f"Cannot complete in status: {self.status}")
        self.reviewed_by = reviewer_id
        self.review_notes = notes
        self.completed_at = datetime.now(UTC)
        self.status = ParticipationStatus.COMPLETED

    def reject(self, reviewer_id: str | None = None, reason: str | None = None) -> None:
        """Reject this participation's submission"""
        if self.status != ParticipationStatus.SUBMITTED:
            raise ValueError(f"Cannot reject in status: {self.status}")
        self.reviewed_by = reviewer_id
        self.rejection_reason = reason
        self.rejected_at = datetime.now(UTC)
        self.status = ParticipationStatus.REJECTED

    def cancel(self) -> None:
        """Cancel this participation (withdraw)"""
        if self.status in (ParticipationStatus.COMPLETED, ParticipationStatus.CANCELLED):
            raise ValueError(f"Cannot cancel in status: {self.status}")
        self.cancelled_at = datetime.now(UTC)
        self.status = ParticipationStatus.CANCELLED

    def resubmit(self, submission: str, artifacts: list[dict] | None = None) -> None:
        """Resubmit after rejection"""
        if self.status != ParticipationStatus.REJECTED:
            raise ValueError(f"Cannot resubmit in status: {self.status}")
        self.submission = submission
        self.submission_artifacts = artifacts or []
        self.submitted_at = datetime.now(UTC)
        self.rejection_reason = None
        self.rejected_at = None
        self.reject_response_deadline = None
        self.review_request_id = None
        self.review_notes = None
        self.reviewed_by = None
        self.status = ParticipationStatus.SUBMITTED

    # ========== Serialization ==========

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization"""
        return {
            "participation_id": self.participation_id,
            "task_id": self.task_id,
            "participant_id": self.participant_id,
            "participant_name": self.participant_name,
            "participant_type": self.participant_type,
            "status": self.status.value,
            "joined_at": self.joined_at.isoformat(),
            "submission": self.submission,
            "submission_artifacts": self.submission_artifacts,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "rejection_reason": self.rejection_reason,
            "rejected_at": self.rejected_at.isoformat() if self.rejected_at else None,
            "reject_response_deadline": (
                self.reject_response_deadline.isoformat() if self.reject_response_deadline else None
            ),
            "review_request_id": self.review_request_id,
            "review_notes": self.review_notes,
            "reviewed_by": self.reviewed_by,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Participation":
        """Create Participation from dictionary"""
        data = data.copy()

        # Parse enum
        if isinstance(data.get("status"), str):
            data["status"] = ParticipationStatus(data["status"])

        # Parse datetime fields
        datetime_fields = [
            "joined_at", "submitted_at", "rejected_at",
            "reject_response_deadline", "completed_at", "cancelled_at",
        ]
        for field_name in datetime_fields:
            if data.get(field_name) and isinstance(data[field_name], str):
                try:
                    data[field_name] = datetime.fromisoformat(data[field_name])
                except (ValueError, TypeError):
                    data.pop(field_name, None)
            elif not data.get(field_name):
                data.pop(field_name, None)

        # Parse list fields
        if isinstance(data.get("submission_artifacts"), str):
            import json
            try:
                data["submission_artifacts"] = json.loads(data["submission_artifacts"])
            except (json.JSONDecodeError, TypeError):
                data["submission_artifacts"] = []

        return cls(**data)

    @staticmethod
    def new_id() -> str:
        """Generate a new participation ID"""
        return str(uuid4())


@dataclass
class Task:
    """
    Task Domain Entity

    Represents a task in the ACN Task Pool.
    Supports two modes:
    - OPEN: Available to all agents, can be repeatable
    - ASSIGNED: Assigned to a specific agent

    Integrates with AP2 for payment handling.
    """

    task_id: str
    mode: TaskMode

    # Creator info
    creator_type: str  # "human" or "agent"
    creator_id: str
    creator_name: str

    # Task content
    title: str
    description: str
    task_type: str = "general"  # coding, review, research, design, etc.
    required_skills: list[str] = field(default_factory=list)

    # Status
    status: TaskStatus = TaskStatus.OPEN

    # Assignment (for ASSIGNED mode or after acceptance in OPEN mode)
    assignee_id: str | None = None
    assignee_name: str | None = None
    assigned_at: datetime | None = None

    # Submission
    submission: str | None = None
    submission_artifacts: list[dict] = field(default_factory=list)
    submitted_at: datetime | None = None

    # Review
    review_notes: str | None = None
    reviewed_by: str | None = None

    # Reward (AP2 integration)
    reward_amount: str = "0"  # String for precision (e.g., "100.50")
    reward_currency: str = "USD"  # USD, USDC, ETH, points, etc.
    payment_task_id: str | None = None  # Reference to AP2 PaymentTask

    # Budget model (supports completion/token/hour/milestone modes)
    reward_unit: str = "completion"  # completion, token, hour, milestone
    total_budget: str = "0"  # Total escrowed amount = reward_amount × max_units
    released_amount: str = "0"  # Amount released to agents so far

    # Multi-participant support
    is_multi_participant: bool = False  # Multiple agents can work in parallel
    allow_repeat_by_same: bool = False  # Same agent can complete again after finishing

    # DEPRECATED — kept for API backward compatibility only.
    # Internal logic should use is_multi_participant exclusively.
    # __post_init__ ensures is_multi_participant=True → is_repeatable=True for serialization.
    is_repeatable: bool = False
    completed_count: int = 0  # Number of completions
    max_completions: int | None = None  # Max completions (None = unlimited, not recommended)
    active_participants_count: int = 0  # Number of currently active participations

    # Timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    deadline: datetime | None = None
    completed_at: datetime | None = None

    # Approval settings
    approval_type: str = "manual"  # manual, auto, validator, webhook
    validator_id: str | None = None  # For validator type: invite_agent, daily_checkin, etc.

    # Idempotency: tracks whether escrow/payment has been released for this task
    payment_released: bool = False

    # Metadata
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        """Validate invariants and sync backward-compat flags"""
        if not self.task_id:
            raise ValueError("task_id cannot be empty")
        if not self.title:
            raise ValueError("title cannot be empty")
        if not self.creator_id:
            raise ValueError("creator_id cannot be empty")

        # Backward compat: is_repeatable=True from old API consumers → enable is_multi_participant
        if self.is_repeatable and not self.is_multi_participant:
            self.is_multi_participant = True
        # Forward sync: keep is_repeatable=True for serialization when is_multi_participant is set
        # (old API consumers expect this field; internal logic uses is_multi_participant only)
        if self.is_multi_participant:
            self.is_repeatable = True

    # ========== Status Transitions ==========

    def can_be_accepted(self) -> bool:
        """
        Check if task can be accepted.

        For multi-participant tasks, delegates to can_join().
        For single-participant tasks, checks status and completion state.

        NOTE: This is a fast-fail pre-filter for better error messages.
        The Lua scripts in TaskRepository are the atomic source of truth
        for capacity checks under concurrent access.
        """
        if self.status != TaskStatus.OPEN:
            return False
        if self.is_multi_participant:
            return self._has_capacity()
        # Single-participant: open tasks need no prior completion
        if self.mode == TaskMode.OPEN:
            return self.completed_count == 0
        # Assigned tasks: just need OPEN status (checked above)
        return True

    def can_join(self) -> bool:
        """
        Check if a new participant can join (multi-participant mode).

        NOTE: This is a fast-fail pre-filter for better error messages.
        The Lua scripts in TaskRepository perform the same checks atomically
        and are the single source of truth under concurrent access.
        """
        if not self.is_multi_participant:
            return False
        if self.status != TaskStatus.OPEN:
            return False
        return self._has_capacity()

    def _has_capacity(self) -> bool:
        """Check capacity: completed + active < max (if max is set)"""
        if self.max_completions is not None:
            return (self.completed_count + self.active_participants_count) < self.max_completions
        return True

    def accept(self, agent_id: str, agent_name: str) -> None:
        """
        Accept the task

        Args:
            agent_id: ID of accepting agent
            agent_name: Name of accepting agent

        Raises:
            ValueError: If task cannot be accepted
        """
        if not self.can_be_accepted():
            raise ValueError(f"Task cannot be accepted in status: {self.status}")

        self.assignee_id = agent_id
        self.assignee_name = agent_name
        self.assigned_at = datetime.now(UTC)
        self.status = TaskStatus.IN_PROGRESS

    def submit(self, submission: str, artifacts: list[dict] | None = None) -> None:
        """
        Submit task result

        Args:
            submission: Result/deliverable
            artifacts: Optional artifacts

        Raises:
            ValueError: If task is not in progress
        """
        if self.status != TaskStatus.IN_PROGRESS:
            raise ValueError(f"Cannot submit in status: {self.status}")

        self.submission = submission
        self.submission_artifacts = artifacts or []
        self.submitted_at = datetime.now(UTC)
        self.status = TaskStatus.SUBMITTED

    def complete(self, reviewer_id: str | None = None, notes: str | None = None) -> None:
        """
        Mark task as completed

        Args:
            reviewer_id: ID of reviewer
            notes: Review notes

        Raises:
            ValueError: If task is not submitted or budget insufficient
        """
        if self.status != TaskStatus.SUBMITTED:
            raise ValueError(f"Cannot complete in status: {self.status}")

        # Check budget before releasing reward
        if float(self.total_budget) > 0 and not self.can_release_reward():
            raise ValueError("Insufficient budget to release reward")

        self.reviewed_by = reviewer_id
        self.review_notes = notes
        self.completed_at = datetime.now(UTC)
        self.completed_count += 1

        # Release reward from budget
        if float(self.total_budget) > 0:
            self.release_reward()

        self.status = TaskStatus.COMPLETED

        # For multi-participant tasks, reset to open after completion
        # (single-participant repeatable tasks go through this same path via is_multi_participant)
        if self.is_multi_participant and self.mode == TaskMode.OPEN:
            # Check max completions
            if self.max_completions is None or self.completed_count < self.max_completions:
                self._reset_for_next_completion()

    def _reset_for_next_completion(self) -> None:
        """Reset task state for next completion (repeatable tasks)"""
        self.status = TaskStatus.OPEN
        self.assignee_id = None
        self.assignee_name = None
        self.assigned_at = None
        self.submission = None
        self.submission_artifacts = []
        self.submitted_at = None
        self.review_notes = None
        self.reviewed_by = None
        # Keep completed_at as last completion time

    def reject(self, reviewer_id: str | None = None, notes: str | None = None) -> None:
        """
        Reject submission

        Args:
            reviewer_id: ID of reviewer
            notes: Rejection reason

        Raises:
            ValueError: If task is not submitted
        """
        if self.status != TaskStatus.SUBMITTED:
            raise ValueError(f"Cannot reject in status: {self.status}")

        self.reviewed_by = reviewer_id
        self.review_notes = notes
        self.status = TaskStatus.REJECTED

    def cancel(self) -> None:
        """
        Cancel the task

        Raises:
            ValueError: If task is already completed
        """
        if self.status == TaskStatus.COMPLETED:
            raise ValueError("Cannot cancel completed task")

        self.status = TaskStatus.CANCELLED

    def reopen(self) -> None:
        """
        Reopen a rejected/cancelled task

        Raises:
            ValueError: If task is completed
        """
        if self.status == TaskStatus.COMPLETED:
            raise ValueError("Cannot reopen completed task")

        self.status = TaskStatus.OPEN
        # Don't clear assignee for ASSIGNED mode tasks

    # ========== Queries ==========

    def is_open(self) -> bool:
        """Check if task is open for acceptance"""
        return self.status == TaskStatus.OPEN

    def is_completed(self) -> bool:
        """Check if task is completed"""
        return self.status == TaskStatus.COMPLETED

    def is_active(self) -> bool:
        """Check if task is active (not completed/cancelled)"""
        return self.status not in [TaskStatus.COMPLETED, TaskStatus.CANCELLED]

    def has_payment(self) -> bool:
        """Check if task has associated payment"""
        return self.payment_task_id is not None

    def remaining_budget(self) -> float:
        """Get remaining budget"""
        return float(self.total_budget) - float(self.released_amount)

    def can_release_reward(self) -> bool:
        """Check if there's enough budget to release reward"""
        return self.remaining_budget() >= float(self.reward_amount)

    def release_reward(self) -> None:
        """Release reward for one completion, updating released_amount"""
        reward = float(self.reward_amount)
        released = float(self.released_amount)
        self.released_amount = str(released + reward)

    def is_past_deadline(self) -> bool:
        """Check if task is past deadline"""
        if not self.deadline:
            return False
        return datetime.now(UTC) > self.deadline

    def matches_skills(self, agent_skills: list[str]) -> bool:
        """Check if agent has required skills"""
        if not self.required_skills:
            return True
        return all(skill in agent_skills for skill in self.required_skills)

    # ========== Serialization ==========

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization"""
        return {
            "task_id": self.task_id,
            "mode": self.mode.value,
            "creator_type": self.creator_type,
            "creator_id": self.creator_id,
            "creator_name": self.creator_name,
            "title": self.title,
            "description": self.description,
            "task_type": self.task_type,
            "required_skills": self.required_skills,
            "status": self.status.value,
            "assignee_id": self.assignee_id,
            "assignee_name": self.assignee_name,
            "assigned_at": self.assigned_at.isoformat() if self.assigned_at else None,
            "submission": self.submission,
            "submission_artifacts": self.submission_artifacts,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "review_notes": self.review_notes,
            "reviewed_by": self.reviewed_by,
            "reward_amount": self.reward_amount,
            "reward_currency": self.reward_currency,
            "payment_task_id": self.payment_task_id,
            "reward_unit": self.reward_unit,
            "total_budget": self.total_budget,
            "released_amount": self.released_amount,
            "is_multi_participant": self.is_multi_participant,
            "allow_repeat_by_same": self.allow_repeat_by_same,
            "is_repeatable": self.is_repeatable,  # backward compat
            "completed_count": self.completed_count,
            "max_completions": self.max_completions,
            "active_participants_count": self.active_participants_count,
            "created_at": self.created_at.isoformat(),
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "approval_type": self.approval_type,
            "validator_id": self.validator_id,
            "payment_released": self.payment_released,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        """Create Task from dictionary"""
        data = data.copy()

        # Parse enums
        if isinstance(data.get("mode"), str):
            data["mode"] = TaskMode(data["mode"])
        if isinstance(data.get("status"), str):
            data["status"] = TaskStatus(data["status"])

        # Parse datetime strings
        datetime_fields = [
            "assigned_at",
            "submitted_at",
            "created_at",
            "deadline",
            "completed_at",
        ]
        for field_name in datetime_fields:
            if data.get(field_name) and isinstance(data[field_name], str):
                try:
                    data[field_name] = datetime.fromisoformat(data[field_name])
                except (ValueError, TypeError):
                    data.pop(field_name, None)

        return cls(**data)
