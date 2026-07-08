"""Task Repository Interface

Defines contract for task persistence operations.
"""

from abc import ABC, abstractmethod
from typing import Any

from ..entities import Participation, Task, TaskStatus


class ITaskRepository(ABC):
    """
    Abstract interface for Task persistence

    Infrastructure layer provides concrete implementation (e.g., Redis).

    Some write methods accept an optional ``session`` keyword (typed as
    ``Any`` to avoid leaking SQLAlchemy into the core layer). It is the
    transactional-outbox seam used by ``task_service.complete_task`` to
    keep a CAS save and a settlement-outbox INSERT in the same ACID
    transaction (saga v0.1). Behaviour by impl:

    * ``PostgresTaskRepository``: when ``session`` is passed, runs the
      query on that session and does NOT commit / open a new session.
      When ``None``, opens its own session + commits, matching the
      original behaviour.
    * ``RedisTaskRepository``: ignores ``session`` entirely (Redis
      transactions and SQL sessions are different abstractions; the
      saga path in v0.1 only runs against PostgreSQL).
    """

    # ========== Task CRUD ==========

    @abstractmethod
    async def save(self, task: Task, *, session: Any | None = None) -> None:
        """Save or update a task.

        ``session`` is optional — see class docstring.
        """
        pass

    @abstractmethod
    async def compare_and_save(
        self,
        task: Task,
        expected_status: TaskStatus,
        *,
        session: Any | None = None,
    ) -> bool:
        """Atomically save the task only if the persisted status equals
        ``expected_status``.

        Used to make single-participant state-machine transitions safe under
        concurrent requests (security audit H3). Without this, two callers
        that both read ``SUBMITTED`` can each pass the in-memory status check
        in :meth:`Task.complete` and both trigger payment release / reward
        distribution before either persists — i.e. *double-pay*.

        Implementations MUST perform the status check and the field write in
        a single atomic operation (e.g. ``UPDATE ... WHERE status=?`` for SQL,
        a Lua script or WATCH/MULTI for Redis).

        ``session`` is optional — see class docstring. Critical to the saga
        v0.1 atomicity guarantee: ``complete_task`` passes its outer session
        in so the CAS and the outbox INSERT share one transaction.

        Returns:
            True if the CAS won and the task was persisted, False if the
            status precondition no longer holds (caller should treat the
            transition as already-applied and behave idempotently).
        """
        pass

    @abstractmethod
    async def find_by_id(self, task_id: str) -> Task | None:
        """Find task by ID"""
        pass

    @abstractmethod
    async def find_open_tasks(
        self,
        mode: str | None = None,
        tags: list[str] | None = None,
        task_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        requesting_agent_id: str | None = None,
    ) -> list[Task]:
        """Find open tasks with optional filters.

        Args:
            requesting_agent_id: If provided, include private subnet tasks
                visible to this agent. If None, only public tasks returned.
        """
        pass

    @abstractmethod
    async def find_by_creator(self, creator_id: str, limit: int = 50) -> list[Task]:
        """Find tasks created by a specific user/agent"""
        pass

    @abstractmethod
    async def find_by_assignee(self, assignee_id: str, limit: int = 50) -> list[Task]:
        """Find tasks assigned to a specific agent"""
        pass

    @abstractmethod
    async def find_by_status(self, status: TaskStatus, limit: int = 50) -> list[Task]:
        """Find tasks by status"""
        pass

    @abstractmethod
    async def find_by_group(self, group_id: str, limit: int = 100) -> list[Task]:
        """Find all tasks belonging to a collaboration group"""
        pass

    @abstractmethod
    async def find_by_board(self, board_id: str, limit: int = 100) -> list[Task]:
        """Find tasks by TaskBoard id (metadata hint; ACN candidate set only — SoT is backend board_tasks)"""
        pass

    @abstractmethod
    async def delete(self, task_id: str) -> bool:
        """Delete a task"""
        pass

    @abstractmethod
    async def exists(self, task_id: str) -> bool:
        """Check if task exists"""
        pass

    @abstractmethod
    async def count_open_tasks(self) -> int:
        """Count total open tasks"""
        pass

    @abstractmethod
    async def record_completion(self, task_id: str, agent_id: str) -> None:
        """Record task completion by an agent"""
        pass

    @abstractmethod
    async def has_completed(self, task_id: str, agent_id: str) -> bool:
        """Check if agent has already completed this task"""
        pass

    # ========== Participation CRUD ==========

    @abstractmethod
    async def save_participation(
        self,
        participation: Participation,
        *,
        session: Any | None = None,
    ) -> None:
        """Save or update a participation.

        ``session`` is optional — see class docstring.
        """
        pass

    @abstractmethod
    async def add_application(self, task_id: str, participation: Participation) -> None:
        """
        Add an application (participation with status APPLIED) for an assigned task.
        Saves the participation and adds it to task/user indices without incrementing active_count.
        """
        pass

    @abstractmethod
    async def find_participation_by_id(self, participation_id: str) -> Participation | None:
        """Find participation by ID"""
        pass

    @abstractmethod
    async def find_participations_by_task(
        self,
        task_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Participation]:
        """Find participations for a task, optionally filtered by status"""
        pass

    @abstractmethod
    async def find_participation_by_user_and_task(
        self,
        task_id: str,
        participant_id: str,
        active_only: bool = True,
    ) -> Participation | None:
        """Find a user's participation in a task (most recent active/submitted)"""
        pass

    @abstractmethod
    async def find_participations_by_user(
        self,
        participant_id: str,
        limit: int = 50,
    ) -> list[Participation]:
        """Find all participations for a user"""
        pass

    @abstractmethod
    async def atomic_join_task(
        self,
        task_id: str,
        participation: Participation,
        max_completions: int | None,
        allow_repeat: bool,
    ) -> str:
        """
        Atomically join a multi-participant task.

        Checks capacity, duplicate participation, and creates the participation
        in a single atomic operation.

        Args:
            task_id: Task identifier
            participation: Participation to create
            max_completions: Max completions limit (None = unlimited)
            allow_repeat: Whether same user can have multiple active participations

        Returns:
            participation_id

        Raises:
            ValueError: If task is full or user already has active participation
        """
        pass

    @abstractmethod
    async def atomic_cancel_participation(
        self,
        participation_id: str,
        task_id: str,
    ) -> None:
        """
        Atomically cancel a participation and decrement active count.

        Raises:
            ValueError: If participation cannot be cancelled
        """
        pass

    @abstractmethod
    async def atomic_complete_participation(
        self,
        participation_id: str,
        task_id: str,
        reviewer_id: str | None = None,
        notes: str | None = None,
    ) -> int:
        """
        Atomically mark participation as completed, increment completed_count,
        and decrement active_participants_count.

        Returns:
            New completed_count

        Raises:
            ValueError: If participation cannot be completed
        """
        pass

    @abstractmethod
    async def count_active_participations(self, task_id: str) -> int:
        """Count active participations for a task"""
        pass

    @abstractmethod
    async def batch_cancel_participations(self, task_id: str) -> int:
        """
        Cancel all active/submitted participations for a task (used when task is cancelled).

        Returns:
            Number of participations cancelled
        """
        pass

    @abstractmethod
    async def decrement_active_count(self, task_id: str) -> int:
        """
        Decrement the active participant count for a task.

        Used when an escrow/payment flow needs to release a slot without
        going through atomic_cancel_participation (e.g., post-completion cleanup).

        Returns:
            New active count (>= 0)
        """
        pass
