"""Task Repository Interface

Defines contract for task persistence operations.
"""

from abc import ABC, abstractmethod

from ..entities import Task, TaskMode, TaskStatus


class ITaskRepository(ABC):
    """
    Abstract interface for Task persistence

    Infrastructure layer provides concrete implementation (e.g., Redis).
    """

    @abstractmethod
    async def save(self, task: Task) -> None:
        """
        Save or update a task

        Args:
            task: Task entity to save
        """
        pass

    @abstractmethod
    async def find_by_id(self, task_id: str) -> Task | None:
        """
        Find task by ID

        Args:
            task_id: Task identifier

        Returns:
            Task entity or None if not found
        """
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
        """
        Find open tasks with optional filters

        Args:
            mode: Filter by task mode
            skills: Filter by required skills
            task_type: Filter by task type
            limit: Maximum number of tasks
            offset: Pagination offset

        Returns:
            List of open tasks
        """
        pass

    @abstractmethod
    async def find_by_creator(self, creator_id: str, limit: int = 50) -> list[Task]:
        """
        Find tasks created by a specific user/agent

        Args:
            creator_id: Creator identifier
            limit: Maximum number of tasks

        Returns:
            List of tasks
        """
        pass

    @abstractmethod
    async def find_by_assignee(self, assignee_id: str, limit: int = 50) -> list[Task]:
        """
        Find tasks assigned to a specific agent

        Args:
            assignee_id: Assignee identifier
            limit: Maximum number of tasks

        Returns:
            List of tasks
        """
        pass

    @abstractmethod
    async def find_by_status(self, status: TaskStatus, limit: int = 50) -> list[Task]:
        """
        Find tasks by status

        Args:
            status: Task status
            limit: Maximum number of tasks

        Returns:
            List of tasks
        """
        pass

    @abstractmethod
    async def delete(self, task_id: str) -> bool:
        """
        Delete a task

        Args:
            task_id: Task identifier

        Returns:
            True if deleted, False if not found
        """
        pass

    @abstractmethod
    async def exists(self, task_id: str) -> bool:
        """
        Check if task exists

        Args:
            task_id: Task identifier

        Returns:
            True if task exists
        """
        pass

    @abstractmethod
    async def count_open_tasks(self) -> int:
        """
        Count total open tasks

        Returns:
            Number of open tasks
        """
        pass

    @abstractmethod
    async def record_completion(self, task_id: str, agent_id: str) -> None:
        """
        Record task completion by an agent (for repeatable open tasks)

        Args:
            task_id: Task identifier
            agent_id: Agent who completed the task
        """
        pass

    @abstractmethod
    async def has_completed(self, task_id: str, agent_id: str) -> bool:
        """
        Check if agent has already completed this task

        Args:
            task_id: Task identifier
            agent_id: Agent identifier

        Returns:
            True if agent has completed the task
        """
        pass
