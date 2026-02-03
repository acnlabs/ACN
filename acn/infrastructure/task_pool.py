"""Task Pool

High-level abstraction for the ACN Task Pool.
Provides simplified interface for agents to discover and work on tasks.
"""

import structlog

from ..core.entities import Task, TaskMode
from ..core.interfaces import ITaskRepository

logger = structlog.get_logger()


class TaskPool:
    """
    Task Pool - Central hub for task discovery and management

    The Task Pool provides a simplified interface for:
    - Agents to discover available tasks (pull model)
    - Filtering tasks by skills, mode, type
    - Tracking task completions

    This is a "pull" model where agents actively query for tasks.
    Future: Could add push notifications via BroadcastService.
    """

    def __init__(self, repository: ITaskRepository):
        """
        Initialize Task Pool

        Args:
            repository: Task repository for persistence
        """
        self.repository = repository

    async def add(self, task: Task) -> Task:
        """
        Add a task to the pool

        Args:
            task: Task to add

        Returns:
            Added task
        """
        await self.repository.save(task)
        logger.info(
            "task_added_to_pool",
            task_id=task.task_id,
            mode=task.mode.value,
            title=task.title,
        )
        return task

    async def remove(self, task_id: str) -> bool:
        """
        Remove a task from the pool

        Args:
            task_id: Task identifier

        Returns:
            True if removed, False if not found
        """
        success = await self.repository.delete(task_id)
        if success:
            logger.info("task_removed_from_pool", task_id=task_id)
        return success

    async def get(self, task_id: str) -> Task | None:
        """
        Get a task by ID

        Args:
            task_id: Task identifier

        Returns:
            Task or None
        """
        return await self.repository.find_by_id(task_id)

    async def get_open_tasks(
        self,
        mode: TaskMode | None = None,
        skills: list[str] | None = None,
        task_type: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Task]:
        """
        Get open tasks from the pool

        Args:
            mode: Filter by task mode (open/assigned)
            skills: Filter by agent's skills (returns tasks the agent can do)
            task_type: Filter by task type
            limit: Maximum number of tasks to return
            offset: Pagination offset

        Returns:
            List of matching open tasks
        """
        return await self.repository.find_open_tasks(
            mode=mode,
            skills=skills,
            task_type=task_type,
            limit=limit,
            offset=offset,
        )

    async def find_tasks_for_agent(
        self,
        agent_skills: list[str],
        limit: int = 20,
    ) -> list[Task]:
        """
        Find tasks suitable for an agent based on their skills

        Args:
            agent_skills: Agent's skill list
            limit: Maximum number of tasks to return

        Returns:
            List of matching tasks
        """
        # Get all open tasks and filter by skills
        tasks = await self.repository.find_open_tasks(limit=limit * 2)  # Get more to filter

        # Filter to tasks the agent can do
        matching_tasks = [task for task in tasks if task.matches_skills(agent_skills)][:limit]

        return matching_tasks

    async def count_open(self) -> int:
        """
        Count open tasks in the pool

        Returns:
            Number of open tasks
        """
        return await self.repository.count_open_tasks()

    async def has_agent_completed(self, task_id: str, agent_id: str) -> bool:
        """
        Check if an agent has already completed a task

        Useful for repeatable tasks to check agent's completion history.

        Args:
            task_id: Task identifier
            agent_id: Agent identifier

        Returns:
            True if agent has completed this task
        """
        return await self.repository.has_completed(task_id, agent_id)

    async def record_completion(self, task_id: str, agent_id: str) -> None:
        """
        Record that an agent completed a task

        Args:
            task_id: Task identifier
            agent_id: Agent identifier
        """
        await self.repository.record_completion(task_id, agent_id)
        logger.info(
            "task_completion_recorded",
            task_id=task_id,
            agent_id=agent_id,
        )

    async def get_stats(self) -> dict:
        """
        Get task pool statistics

        Returns:
            Dictionary with pool statistics
        """
        open_count = await self.count_open()

        # Get counts by mode
        open_mode_tasks = await self.repository.find_open_tasks(mode=TaskMode.OPEN, limit=1000)
        assigned_mode_tasks = await self.repository.find_open_tasks(
            mode=TaskMode.ASSIGNED, limit=1000
        )

        return {
            "total_open": open_count,
            "open_mode_count": len(open_mode_tasks),
            "assigned_mode_count": len(assigned_mode_tasks),
        }
