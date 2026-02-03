"""Redis Implementation of Task Repository

Concrete implementation using Redis for task persistence.
"""

import json
from datetime import datetime

import redis.asyncio as redis  # type: ignore[import-untyped]

from ....core.entities import Task, TaskMode, TaskStatus
from ....core.interfaces import ITaskRepository


class RedisTaskRepository(ITaskRepository):
    """
    Redis-based Task Repository

    Implements ITaskRepository using Redis as storage backend.

    Key Structure:
    - acn:task:{task_id} → Hash (task data)
    - acn:tasks:open → SortedSet (task_ids by created_at timestamp)
    - acn:tasks:by_mode:{mode} → Set (task_ids)
    - acn:tasks:by_status:{status} → Set (task_ids)
    - acn:tasks:by_skill:{skill} → Set (task_ids)
    - acn:tasks:by_creator:{creator_id} → Set (task_ids)
    - acn:tasks:by_assignee:{assignee_id} → Set (task_ids)
    - acn:task:completions:{task_id} → Set (agent_ids who completed)
    """

    def __init__(self, redis_client: redis.Redis):
        """
        Initialize Redis Task Repository

        Args:
            redis_client: Redis async client instance
        """
        self.redis = redis_client

    async def save(self, task: Task) -> None:
        """Save or update a task in Redis"""
        task_key = f"acn:task:{task.task_id}"

        # Check for existing task to clean up old indices
        existing = await self.find_by_id(task.task_id)

        # Serialize task to dict
        task_dict = task.to_dict()

        # Convert lists/dicts to JSON strings for Redis
        task_dict["required_skills"] = json.dumps(task_dict.get("required_skills", []))
        task_dict["submission_artifacts"] = json.dumps(task_dict.get("submission_artifacts", []))
        task_dict["metadata"] = json.dumps(task_dict.get("metadata", {}))

        # Filter out None values and convert booleans
        clean_dict = {}
        for k, v in task_dict.items():
            if v is None:
                continue
            elif isinstance(v, bool):
                clean_dict[k] = "true" if v else "false"
            else:
                clean_dict[k] = v

        # Save to Redis hash
        await self.redis.hset(task_key, mapping=clean_dict)  # type: ignore[arg-type]

        # ===== Update Indices =====

        # 1. Open tasks index (sorted by created_at)
        if task.status == TaskStatus.OPEN:
            timestamp = task.created_at.timestamp()
            await self.redis.zadd("acn:tasks:open", {task.task_id: timestamp})
        else:
            await self.redis.zrem("acn:tasks:open", task.task_id)

        # 2. Mode index
        await self.redis.sadd(f"acn:tasks:by_mode:{task.mode.value}", task.task_id)
        # Clean up old mode if changed
        if existing and existing.mode != task.mode:
            await self.redis.srem(f"acn:tasks:by_mode:{existing.mode.value}", task.task_id)

        # 3. Status index
        await self.redis.sadd(f"acn:tasks:by_status:{task.status.value}", task.task_id)
        # Clean up old status
        if existing and existing.status != task.status:
            await self.redis.srem(f"acn:tasks:by_status:{existing.status.value}", task.task_id)

        # 4. Skill indices
        for skill in task.required_skills:
            await self.redis.sadd(f"acn:tasks:by_skill:{skill}", task.task_id)
        # Clean up old skills
        if existing:
            for old_skill in existing.required_skills:
                if old_skill not in task.required_skills:
                    await self.redis.srem(f"acn:tasks:by_skill:{old_skill}", task.task_id)

        # 5. Creator index
        await self.redis.sadd(f"acn:tasks:by_creator:{task.creator_id}", task.task_id)

        # 6. Assignee index
        if task.assignee_id:
            await self.redis.sadd(f"acn:tasks:by_assignee:{task.assignee_id}", task.task_id)
        # Clean up old assignee
        if existing and existing.assignee_id and existing.assignee_id != task.assignee_id:
            await self.redis.srem(f"acn:tasks:by_assignee:{existing.assignee_id}", task.task_id)

    async def find_by_id(self, task_id: str) -> Task | None:
        """Find task by ID"""
        task_key = f"acn:task:{task_id}"
        task_dict = await self.redis.hgetall(task_key)

        if not task_dict:
            return None

        return self._dict_to_task(task_dict)

    async def find_open_tasks(
        self,
        mode: TaskMode | None = None,
        skills: list[str] | None = None,
        task_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        """Find open tasks with optional filters"""
        # Get open task IDs (sorted by created_at, newest first)
        task_ids = await self.redis.zrevrange("acn:tasks:open", offset, offset + limit - 1)

        tasks = []
        for task_id in task_ids:
            task_id = task_id.decode() if isinstance(task_id, bytes) else task_id
            task = await self.find_by_id(task_id)
            if not task:
                continue

            # Apply filters
            if mode and task.mode != mode:
                continue
            if skills and not task.matches_skills(skills):
                continue
            if task_type and task.task_type != task_type:
                continue

            tasks.append(task)

        return tasks

    async def find_by_creator(self, creator_id: str, limit: int = 50) -> list[Task]:
        """Find tasks created by a specific user/agent"""
        task_ids = await self.redis.smembers(f"acn:tasks:by_creator:{creator_id}")
        tasks = []

        for task_id in list(task_ids)[:limit]:
            task_id = task_id.decode() if isinstance(task_id, bytes) else task_id
            task = await self.find_by_id(task_id)
            if task:
                tasks.append(task)

        return tasks

    async def find_by_assignee(self, assignee_id: str, limit: int = 50) -> list[Task]:
        """Find tasks assigned to a specific agent"""
        task_ids = await self.redis.smembers(f"acn:tasks:by_assignee:{assignee_id}")
        tasks = []

        for task_id in list(task_ids)[:limit]:
            task_id = task_id.decode() if isinstance(task_id, bytes) else task_id
            task = await self.find_by_id(task_id)
            if task:
                tasks.append(task)

        return tasks

    async def find_by_status(self, status: TaskStatus, limit: int = 50) -> list[Task]:
        """Find tasks by status"""
        task_ids = await self.redis.smembers(f"acn:tasks:by_status:{status.value}")
        tasks = []

        for task_id in list(task_ids)[:limit]:
            task_id = task_id.decode() if isinstance(task_id, bytes) else task_id
            task = await self.find_by_id(task_id)
            if task:
                tasks.append(task)

        return tasks

    async def delete(self, task_id: str) -> bool:
        """Delete a task"""
        task = await self.find_by_id(task_id)
        if not task:
            return False

        # Remove from Redis
        task_key = f"acn:task:{task_id}"
        await self.redis.delete(task_key)

        # Remove from indices
        await self.redis.zrem("acn:tasks:open", task_id)
        await self.redis.srem(f"acn:tasks:by_mode:{task.mode.value}", task_id)
        await self.redis.srem(f"acn:tasks:by_status:{task.status.value}", task_id)
        await self.redis.srem(f"acn:tasks:by_creator:{task.creator_id}", task_id)

        if task.assignee_id:
            await self.redis.srem(f"acn:tasks:by_assignee:{task.assignee_id}", task_id)

        for skill in task.required_skills:
            await self.redis.srem(f"acn:tasks:by_skill:{skill}", task_id)

        # Remove completions
        await self.redis.delete(f"acn:task:completions:{task_id}")

        return True

    async def exists(self, task_id: str) -> bool:
        """Check if task exists"""
        return await self.redis.exists(f"acn:task:{task_id}") > 0

    async def count_open_tasks(self) -> int:
        """Count total open tasks"""
        return await self.redis.zcard("acn:tasks:open")

    async def record_completion(self, task_id: str, agent_id: str) -> None:
        """Record task completion by an agent"""
        await self.redis.sadd(f"acn:task:completions:{task_id}", agent_id)

    async def has_completed(self, task_id: str, agent_id: str) -> bool:
        """Check if agent has already completed this task"""
        return await self.redis.sismember(f"acn:task:completions:{task_id}", agent_id)

    def _dict_to_task(self, task_dict: dict) -> Task:
        """Convert Redis dict to Task entity"""
        # Decode bytes
        data = {}
        for k, v in task_dict.items():
            key = k.decode() if isinstance(k, bytes) else k
            val = v.decode() if isinstance(v, bytes) else v
            data[key] = val

        # Parse JSON fields
        data["required_skills"] = json.loads(data.get("required_skills", "[]"))
        data["submission_artifacts"] = json.loads(data.get("submission_artifacts", "[]"))
        data["metadata"] = json.loads(data.get("metadata", "{}"))

        # Parse enums
        data["mode"] = TaskMode(data["mode"])
        data["status"] = TaskStatus(data["status"])

        # Parse booleans
        data["is_repeatable"] = data.get("is_repeatable", "false").lower() == "true"

        # Parse integers
        data["completed_count"] = int(data.get("completed_count", 0))
        if data.get("max_completions"):
            data["max_completions"] = int(data["max_completions"])
        else:
            data["max_completions"] = None

        # Parse datetime fields
        datetime_fields = [
            "assigned_at",
            "submitted_at",
            "created_at",
            "deadline",
            "completed_at",
        ]
        for field_name in datetime_fields:
            if data.get(field_name):
                data[field_name] = datetime.fromisoformat(data[field_name])
            else:
                data.pop(field_name, None)

        return Task(**data)
