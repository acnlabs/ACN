"""Task Domain Entity

Pure business logic for Task and Participation, independent of infrastructure.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4


class TaskStatus(StrEnum):
    """Task status"""

    OPEN = "open"          # Task is open for acceptance
    IN_PROGRESS = "in_progress"  # Agent is working on it
    SUBMITTED = "submitted"      # Result submitted, pending review
    COMPLETED = "completed"      # Approved and done
    REJECTED = "rejected"        # Submission rejected
    CANCELLED = "cancelled"      # Cancelled by creator


class ParticipationStatus(StrEnum):
    """Participation lifecycle status"""

    APPLIED = "applied"      # Applied for task with join approval, awaiting creator approval
    ACTIVE = "active"        # Participant is working on the task
    SUBMITTED = "submitted"  # Participant submitted, pending review
    COMPLETED = "completed"  # Approved and reward released
    REJECTED = "rejected"    # Submission rejected by creator
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
    joined_at: datetime = field(default_factory=datetime.now)

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
            "joined_at",
            "submitted_at",
            "rejected_at",
            "reject_response_deadline",
            "completed_at",
            "cancelled_at",
        ]
        for field_name in datetime_fields:
            if data.get(field_name) and isinstance(data[field_name], str):
                data[field_name] = datetime.fromisoformat(data[field_name])
            elif not data.get(field_name):
                data.pop(field_name, None)

        # Parse list fields
        if isinstance(data.get("submission_artifacts"), str):
            import json

            data["submission_artifacts"] = json.loads(data["submission_artifacts"])

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

    Participation model (two orthogonal dimensions):
    - max_participants: capacity (1=single, N=fixed, None=unlimited)
    - completion_mode: how participants work and settle
        - "independent": each completes separately, paid per completion
        - "competitive": each submits separately, creator picks winner(s)
        - "collaborative": team works together, settles on group completion

    When max_participants=1, completion_mode is always "independent".
    When max_participants is None (unlimited), "collaborative" is invalid.
    """

    task_id: str

    # Creator info
    creator_type: str  # "human" or "agent"
    creator_id: str
    creator_name: str

    # Task content
    title: str
    description: str
    task_type: str = "general"
    required_tags: list[str] = field(default_factory=list)

    # Status
    status: TaskStatus = TaskStatus.OPEN

    # Assignment (set when an agent accepts a single-participant task)
    assignee_id: str | None = None
    assignee_name: str | None = None
    assignee_type: str | None = None  # "agent" | "human"
    assigned_at: datetime | None = None

    # Submission (single-participant path)
    submission: str | None = None
    submission_artifacts: list[dict] = field(default_factory=list)
    submitted_at: datetime | None = None

    # Review
    review_notes: str | None = None
    reviewed_by: str | None = None

    # Reward
    reward: str = "0"   # Per-completion reward amount (string for precision)
    reward_currency: str = "credits"
    payment_task_id: str | None = None

    # Budget
    total_budget: str = "0"      # Total locked budget
    released_amount: str = "0"   # Amount released to agents so far
    max_total_budget: str | None = None  # Budget cap for bounty tasks (max_participants=None)

    # Participation control
    max_participants: int | None = 1    # 1=single, N=fixed, None=unlimited
    completion_mode: str = "independent"  # "independent" | "competitive" | "collaborative"
    require_join_approval: bool = False  # True: solvers must apply and be approved to join
    auto_approve: bool = False           # True: submissions auto-complete without review
    allow_repeat_by_same: bool = False   # True: same solver can complete again after finishing

    # Invitation: creator can invite specific solvers who bypass require_join_approval
    invited_agent_ids: list[str] = field(default_factory=list)

    # Escrow
    use_escrow: bool = False  # True: budget locked in escrow at creation time

    # Collaboration
    group_id: str | None = None  # Link related subtasks into a collaborative group

    # Visibility — NULL means public; set to an ACN Subnet ID to restrict to members only
    subnet_id: str | None = None

    # Progress
    completed_count: int = 0
    active_participants_count: int = 0

    # Timestamps
    created_at: datetime = field(default_factory=datetime.now)
    deadline: datetime | None = None
    completed_at: datetime | None = None

    # Metadata (extensible for future features)
    metadata: dict = field(default_factory=dict)

    VALID_COMPLETION_MODES = ("independent", "competitive", "collaborative")

    def __post_init__(self):
        """Validate invariants"""
        if not self.task_id:
            raise ValueError("task_id cannot be empty")
        if not self.title:
            raise ValueError("title cannot be empty")
        if not self.creator_id:
            raise ValueError("creator_id cannot be empty")
        if self.completion_mode not in self.VALID_COMPLETION_MODES:
            raise ValueError(f"Invalid completion_mode: {self.completion_mode}")
        if self.max_participants == 1 and self.completion_mode != "independent":
            self.completion_mode = "independent"
        if self.max_participants is None and self.completion_mode == "collaborative":
            raise ValueError("collaborative mode requires finite max_participants")

    # ========== Helpers ==========

    def _is_multi(self) -> bool:
        """True if multiple agents can participate (max_participants != 1)"""
        return self.max_participants is None or self.max_participants > 1

    # ========== Status Transitions ==========

    def can_be_accepted(self) -> bool:
        """
        Check if task can be accepted by an agent.

        NOTE: This is a fast-fail pre-filter for better error messages.
        The Lua scripts in TaskRepository are the atomic source of truth
        for capacity checks under concurrent access.
        """
        if self.status != TaskStatus.OPEN:
            return False
        if self._is_multi():
            return self._has_capacity()
        # Single-participant: only accept if not yet completed
        return self.completed_count == 0

    def can_join(self) -> bool:
        """
        Check if a new participant can join (multi-participant mode).

        NOTE: This is a fast-fail pre-filter for better error messages.
        The Lua scripts in TaskRepository perform the same checks atomically
        and are the single source of truth under concurrent access.
        """
        if not self._is_multi():
            return False
        if self.status != TaskStatus.OPEN:
            return False
        return self._has_capacity()

    def _has_capacity(self) -> bool:
        """Check capacity: completed + active < max_participants (if max is set)"""
        if self.max_participants is not None:
            return (self.completed_count + self.active_participants_count) < self.max_participants
        return True

    def accept(self, agent_id: str, agent_name: str) -> None:
        """
        Accept the task (single-participant path).

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
        self.assignee_type = "agent"
        self.assigned_at = datetime.now(UTC)
        self.status = TaskStatus.IN_PROGRESS

    def submit(self, submission: str, artifacts: list[dict] | None = None) -> None:
        """
        Submit task result (single-participant path).

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
        Mark task as completed (single-participant path).

        Args:
            reviewer_id: ID of reviewer
            notes: Review notes

        Raises:
            ValueError: If task is not submitted or budget insufficient
        """
        if self.status != TaskStatus.SUBMITTED:
            raise ValueError(f"Cannot complete in status: {self.status}")

        if float(self.total_budget) > 0 and not self.can_release_reward():
            raise ValueError("Insufficient budget to release reward")

        self.reviewed_by = reviewer_id
        self.review_notes = notes
        self.completed_at = datetime.now(UTC)
        self.completed_count += 1

        if float(self.total_budget) > 0:
            self.release_reward()

        self.status = TaskStatus.COMPLETED

        # Multi-participant tasks reset to OPEN after each completion
        if self._is_multi():
            if self.max_participants is None or self.completed_count < self.max_participants:
                self._reset_for_next_completion()

    def _reset_for_next_completion(self) -> None:
        """Reset task state for next completion (multi-participant tasks)"""
        self.status = TaskStatus.OPEN
        self.assignee_id = None
        self.assignee_name = None
        self.assignee_type = None
        self.assigned_at = None
        self.submission = None
        self.submission_artifacts = []
        self.submitted_at = None
        self.review_notes = None
        self.reviewed_by = None

    def reject(self, reviewer_id: str | None = None, notes: str | None = None) -> None:
        """
        Reject submission (single-participant path).

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

    def resubmit(self, submission: str, artifacts: list[dict] | None = None) -> None:
        """
        Resubmit after rejection (single-participant path).

        Raises:
            ValueError: If task is not in rejected status
        """
        if self.status != TaskStatus.REJECTED:
            raise ValueError(f"Cannot resubmit in status: {self.status}")
        self.submission = submission
        self.submission_artifacts = artifacts or []
        self.submitted_at = datetime.now(UTC)
        self.review_notes = None
        self.reviewed_by = None
        self.status = TaskStatus.SUBMITTED

    def cancel(self) -> None:
        """Cancel the task"""
        if self.status == TaskStatus.COMPLETED:
            raise ValueError("Cannot cancel completed task")
        self.status = TaskStatus.CANCELLED

    def reopen(self) -> None:
        """Reopen a rejected/cancelled task"""
        if self.status == TaskStatus.COMPLETED:
            raise ValueError("Cannot reopen completed task")
        self.status = TaskStatus.OPEN

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
        return self.remaining_budget() >= float(self.reward)

    def release_reward(self) -> None:
        """Release reward for one completion, updating released_amount"""
        reward = float(self.reward)
        released = float(self.released_amount)
        self.released_amount = str(released + reward)

    def is_past_deadline(self) -> bool:
        """Check if task is past deadline"""
        if not self.deadline:
            return False
        return datetime.now(UTC) > self.deadline

    def matches_tags(self, agent_tags: list[str]) -> bool:
        """Check if agent has all required tags"""
        if not self.required_tags:
            return True
        return all(tag in agent_tags for tag in self.required_tags)

    # ========== Serialization ==========

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization"""
        return {
            "task_id": self.task_id,
            "creator_type": self.creator_type,
            "creator_id": self.creator_id,
            "creator_name": self.creator_name,
            "title": self.title,
            "description": self.description,
            "task_type": self.task_type,
            "required_tags": self.required_tags,
            "status": self.status.value,
            "assignee_id": self.assignee_id,
            "assignee_name": self.assignee_name,
            "assignee_type": self.assignee_type,
            "assigned_at": self.assigned_at.isoformat() if self.assigned_at else None,
            "submission": self.submission,
            "submission_artifacts": self.submission_artifacts,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "review_notes": self.review_notes,
            "reviewed_by": self.reviewed_by,
            "reward": self.reward,
            "reward_currency": self.reward_currency,
            "payment_task_id": self.payment_task_id,
            "total_budget": self.total_budget,
            "released_amount": self.released_amount,
            "max_total_budget": self.max_total_budget,
            "max_participants": self.max_participants,
            "completion_mode": self.completion_mode,
            "require_join_approval": self.require_join_approval,
            "auto_approve": self.auto_approve,
            "allow_repeat_by_same": self.allow_repeat_by_same,
            "use_escrow": self.use_escrow,
            "invited_agent_ids": self.invited_agent_ids,
            "group_id": self.group_id,
            "completed_count": self.completed_count,
            "active_participants_count": self.active_participants_count,
            "created_at": self.created_at.isoformat(),
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "metadata": self.metadata,
            "subnet_id": self.subnet_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        """Create Task from dictionary"""
        data = data.copy()

        # Parse status enum
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
                data[field_name] = datetime.fromisoformat(data[field_name])

        # Strip removed fields that may come from old serialized data
        for old_field in (
            "mode", "approval_type", "validator_id", "reward_unit",
            "is_multi_participant", "is_repeatable", "max_completions",
            "reward_amount",
        ):
            data.pop(old_field, None)

        # Default completion_mode for tasks created before this field existed
        if "completion_mode" not in data:
            data["completion_mode"] = "independent"

        return cls(**data)
