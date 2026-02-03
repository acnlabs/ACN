"""Task Domain Entity

Pure business logic for Task, independent of infrastructure.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class TaskMode(str, Enum):
    """Task mode"""

    OPEN = "open"  # Open task, any agent can complete
    ASSIGNED = "assigned"  # Assigned to specific agent


class ApprovalType(str, Enum):
    """Task approval type"""

    MANUAL = "manual"  # Human review required (default)
    AUTO = "auto"  # Auto-approve on submission
    VALIDATOR = "validator"  # Use platform validator (future)
    WEBHOOK = "webhook"  # Call external webhook (future)


class TaskStatus(str, Enum):
    """Task status"""

    OPEN = "open"  # Task is open for acceptance
    ASSIGNED = "assigned"  # Task assigned to an agent
    IN_PROGRESS = "in_progress"  # Agent is working on it
    SUBMITTED = "submitted"  # Result submitted, pending review
    COMPLETED = "completed"  # Approved and done
    REJECTED = "rejected"  # Submission rejected
    CANCELLED = "cancelled"  # Cancelled by creator


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

    # Open task specific
    is_repeatable: bool = False  # Can be completed multiple times
    completed_count: int = 0  # Number of completions
    max_completions: int | None = None  # Max completions (None = unlimited, not recommended)

    # Timestamps
    created_at: datetime = field(default_factory=datetime.now)
    deadline: datetime | None = None
    completed_at: datetime | None = None

    # Approval settings
    approval_type: str = "manual"  # manual, auto, validator, webhook
    validator_id: str | None = None  # For validator type: invite_agent, daily_checkin, etc.

    # Metadata
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        """Validate invariants"""
        if not self.task_id:
            raise ValueError("task_id cannot be empty")
        if not self.title:
            raise ValueError("title cannot be empty")
        if not self.creator_id:
            raise ValueError("creator_id cannot be empty")

    # ========== Status Transitions ==========

    def can_be_accepted(self) -> bool:
        """Check if task can be accepted"""
        if self.mode == TaskMode.OPEN:
            # Open tasks can always be accepted (if repeatable or not yet completed)
            if self.is_repeatable:
                return self.status == TaskStatus.OPEN
            return self.status == TaskStatus.OPEN and self.completed_count == 0
        else:
            # Assigned tasks can only be accepted when in OPEN status
            return self.status == TaskStatus.OPEN

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
        self.assigned_at = datetime.now()
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
        self.submitted_at = datetime.now()
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
        self.completed_at = datetime.now()
        self.completed_count += 1

        # Release reward from budget
        if float(self.total_budget) > 0:
            self.release_reward()

        self.status = TaskStatus.COMPLETED

        # For repeatable tasks, reset to open after completion
        if self.is_repeatable and self.mode == TaskMode.OPEN:
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
        return datetime.now() > self.deadline

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
            "is_repeatable": self.is_repeatable,
            "completed_count": self.completed_count,
            "max_completions": self.max_completions,
            "created_at": self.created_at.isoformat(),
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "approval_type": self.approval_type,
            "validator_id": self.validator_id,
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
                data[field_name] = datetime.fromisoformat(data[field_name])

        return cls(**data)
