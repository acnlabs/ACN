"""Task Repository Interface

Defines contract for task persistence operations.
"""

from abc import ABC, abstractmethod

from ..entities import Participation, Task, TaskMode, TaskStatus


class ITaskRepository(ABC):
    """
    Abstract interface for Task persistence

    Infrastructure layer provides concrete implementation (e.g., Redis).
    """

    # ========== Task CRUD ==========

    @abstractmethod
    async def save(self, task: Task) -> None:
        """Save or update a task"""
        pass

    @abstractmethod
    async def find_by_id(self, task_id: str) -> Task | None:
        """Find task by ID"""
        pass

    @abstractmethod
    async def find_open_tasks(
        self,
        mode: TaskMode | None = None,
        skills: list[str] | None = None,
        task_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        """Find open tasks with optional filters"""
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
    async def save_participation(self, participation: Participation) -> None:
        """Save or update a participation"""
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
