"""Redis-based Task Store for A2A Protocol

Provides persistent task storage using Redis, replacing InMemoryTaskStore.
"""

from __future__ import annotations

import json

import structlog
from a2a.server.context import ServerCallContext
from a2a.server.tasks import TaskStore
from a2a.types import Task, TaskState
from redis.asyncio import Redis

logger = structlog.get_logger()


class RedisTaskStore(TaskStore):
    """Redis-based persistent task storage

    Stores A2A tasks in Redis with:
    - Task persistence across restarts
    - Efficient lookup by task ID
    - Filtering by context and status
    - Automatic expiration (30 days)
    """

    def __init__(self, redis: Redis, key_prefix: str = "a2a:tasks:"):
        """Initialize Redis task store

        Args:
            redis: Redis client
            key_prefix: Key prefix for task storage
        """
        self.redis = redis
        self.key_prefix = key_prefix

    async def get(self, task_id: str, context: ServerCallContext | None = None) -> Task | None:
        """Get task by ID

        Args:
            task_id: Task ID
            context: Optional server call context (unused)

        Returns:
            Task object or None if not found
        """
        task_key = f"{self.key_prefix}{task_id}"
        task_json = await self.redis.get(task_key)

        if not task_json:
            return None

        try:
            task_data = json.loads(task_json)
            return Task(**task_data)
        except Exception as e:
            logger.error("task_load_failed", task_id=task_id, error=str(e))
            return None

    async def save(self, task: Task, context: ServerCallContext | None = None) -> None:
        """Save task to Redis

        Args:
            task: Task to save
            context: Optional server call context (unused)
        """
        task_key = f"{self.key_prefix}{task.id}"

        try:
            # Serialize task
            task_json = task.model_dump_json(by_alias=True, exclude_none=True)

            # Save to Redis
            await self.redis.set(task_key, task_json)

            # Set expiration (30 days)
            await self.redis.expire(task_key, 30 * 24 * 3600)

            # Add to indexes for efficient querying
            await self._update_indexes(task)

            logger.debug(
                "task_saved",
                task_id=task.id,
                context_id=task.context_id,
                status=task.status.state.value,
            )

        except Exception as e:
            logger.error("task_save_failed", task_id=task.id, error=str(e))
            raise

    async def delete(self, task_id: str, context: ServerCallContext | None = None) -> None:
        """Delete task from Redis

        Args:
            task_id: Task ID
            context: Optional server call context (unused)
        """
        task_key = f"{self.key_prefix}{task_id}"

        # Get task before deletion for index cleanup
        task = await self.get(task_id)

        # Delete task data
        await self.redis.delete(task_key)

        # Clean up indexes
        if task:
            await self._remove_from_indexes(task)

        logger.debug("task_deleted", task_id=task_id)

    async def list(
        self,
        context_id: str | None = None,
        status: TaskState | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Task]:
        """List tasks with optional filters

        Args:
            context_id: Filter by context ID
            status: Filter by status
            limit: Maximum number of tasks to return
            offset: Offset for pagination

        Returns:
            List of tasks
        """
        task_ids = await self._get_task_ids(context_id, status)

        # Apply pagination
        paginated_ids = task_ids[offset : offset + limit]

        # Fetch tasks
        tasks = []
        for task_id in paginated_ids:
            task = await self.get(task_id)
            if task:
                tasks.append(task)

        return tasks

    async def _get_task_ids(
        self, context_id: str | None = None, status: TaskState | None = None
    ) -> list[str]:
        """Get task IDs based on filters

        Args:
            context_id: Filter by context ID
            status: Filter by status

        Returns:
            List of task IDs
        """
        if context_id and status:
            # Intersection of context and status
            index_key = f"{self.key_prefix}index:context:{context_id}:status:{status.value}"
            task_ids = await self.redis.smembers(index_key)
        elif context_id:
            # All tasks in context
            index_key = f"{self.key_prefix}index:context:{context_id}"
            task_ids = await self.redis.smembers(index_key)
        elif status:
            # All tasks with status
            index_key = f"{self.key_prefix}index:status:{status.value}"
            task_ids = await self.redis.smembers(index_key)
        else:
            # All tasks - scan all task keys
            task_keys = []
            async for key in self.redis.scan_iter(f"{self.key_prefix}*", count=1000):
                key_str = key.decode() if isinstance(key, bytes) else key
                # Skip index keys
                if ":index:" not in key_str:
                    task_keys.append(key_str)

            # Extract task IDs from keys
            task_ids = [key.replace(self.key_prefix, "") for key in task_keys]
            return task_ids

        # Convert bytes to str
        return [tid.decode() if isinstance(tid, bytes) else tid for tid in task_ids]

    async def _update_indexes(self, task: Task) -> None:
        """Update Redis indexes for efficient querying

        Args:
            task: Task to index
        """
        # Index by context
        context_index = f"{self.key_prefix}index:context:{task.context_id}"
        await self.redis.sadd(context_index, task.id)
        await self.redis.expire(context_index, 30 * 24 * 3600)

        # Index by status
        status_index = f"{self.key_prefix}index:status:{task.status.state.value}"
        await self.redis.sadd(status_index, task.id)
        await self.redis.expire(status_index, 30 * 24 * 3600)

        # Index by context + status
        context_status_index = (
            f"{self.key_prefix}index:context:{task.context_id}:" f"status:{task.status.state.value}"
        )
        await self.redis.sadd(context_status_index, task.id)
        await self.redis.expire(context_status_index, 30 * 24 * 3600)

    async def _remove_from_indexes(self, task: Task) -> None:
        """Remove task from indexes

        Args:
            task: Task to remove from indexes
        """
        # Remove from context index
        context_index = f"{self.key_prefix}index:context:{task.context_id}"
        await self.redis.srem(context_index, task.id)

        # Remove from status index
        status_index = f"{self.key_prefix}index:status:{task.status.state.value}"
        await self.redis.srem(status_index, task.id)

        # Remove from context + status index
        context_status_index = (
            f"{self.key_prefix}index:context:{task.context_id}:" f"status:{task.status.state.value}"
        )
        await self.redis.srem(context_status_index, task.id)

    async def count(self, context_id: str | None = None, status: TaskState | None = None) -> int:
        """Count tasks

        Args:
            context_id: Filter by context ID
            status: Filter by status

        Returns:
            Number of tasks matching filters
        """
        task_ids = await self._get_task_ids(context_id, status)
        return len(task_ids)

    async def clear(self) -> None:
        """Clear all tasks (for testing)

        Warning: This deletes all tasks!
        """
        # Get all task keys
        task_keys = []
        async for key in self.redis.scan_iter(f"{self.key_prefix}*"):
            task_keys.append(key)

        # Delete all
        if task_keys:
            await self.redis.delete(*task_keys)

        logger.warning("all_tasks_cleared", count=len(task_keys))


__all__ = ["RedisTaskStore"]
