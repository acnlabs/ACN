"""Redis Implementation of Task Repository

Concrete implementation using Redis for task persistence.
"""

import json
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as redis  # type: ignore[import-untyped]

from ....core.entities import Participation, ParticipationStatus, Task, TaskMode, TaskStatus
from ....core.interfaces import ITaskRepository

# ============================================================================
# Lua Scripts for Atomic Operations
# ============================================================================

# Atomic join: check capacity + duplicate + create participation
LUA_JOIN_TASK = """
local task_key = KEYS[1]
local active_count_key = KEYS[2]
local participations_key = KEYS[3]
local user_task_key = KEYS[4]
local participation_key = KEYS[5]

local max_completions = tonumber(ARGV[1])  -- -1 means unlimited
local allow_repeat = ARGV[2] == "true"
local participation_id = ARGV[3]
local participant_id = ARGV[4]
local joined_at_score = tonumber(ARGV[5])
local participation_data = ARGV[6]  -- JSON string

-- Check task status
local task_status = redis.call('HGET', task_key, 'status')
if task_status ~= 'open' then
    return redis.error_reply('TASK_NOT_OPEN')
end

-- Check capacity
local completed = tonumber(redis.call('HGET', task_key, 'completed_count') or '0')
local active = tonumber(redis.call('GET', active_count_key) or '0')
if max_completions >= 0 and (completed + active) >= max_completions then
    return redis.error_reply('TASK_FULL')
end

-- Check duplicate: does this user already have an active participation?
if not allow_repeat then
    local user_participations = redis.call('SMEMBERS', user_task_key)
    for _, pid in ipairs(user_participations) do
        local pstatus = redis.call('HGET', 'acn:participation:' .. pid, 'status')
        if pstatus == 'active' or pstatus == 'submitted' then
            return redis.error_reply('ALREADY_JOINED')
        end
    end
end

-- Create participation
local data = cjson.decode(participation_data)
for k, v in pairs(data) do
    redis.call('HSET', participation_key, k, tostring(v))
end

-- Update indices
redis.call('INCR', active_count_key)
redis.call('ZADD', participations_key, joined_at_score, participation_id)
redis.call('SADD', user_task_key, participation_id)

-- Sync active_participants_count on task hash
local new_active = tonumber(redis.call('GET', active_count_key) or '0')
redis.call('HSET', task_key, 'active_participants_count', tostring(new_active))

return participation_id
"""

# Atomic cancel: set cancelled + decrement active count
LUA_CANCEL_PARTICIPATION = """
local participation_key = KEYS[1]
local active_count_key = KEYS[2]
local task_key = KEYS[3]

local current_status = redis.call('HGET', participation_key, 'status')
if not current_status then
    return redis.error_reply('NOT_FOUND')
end
if current_status == 'completed' or current_status == 'cancelled' then
    return redis.error_reply('CANNOT_CANCEL')
end

local was_active = (current_status == 'active' or current_status == 'submitted')

redis.call('HSET', participation_key, 'status', 'cancelled')
redis.call('HSET', participation_key, 'cancelled_at', ARGV[1])

if was_active then
    redis.call('DECR', active_count_key)
    -- Ensure non-negative
    local cnt = tonumber(redis.call('GET', active_count_key) or '0')
    if cnt < 0 then redis.call('SET', active_count_key, '0') end
end

-- Sync to task hash
local new_active = tonumber(redis.call('GET', active_count_key) or '0')
redis.call('HSET', task_key, 'active_participants_count', tostring(new_active))

return 'OK'
"""

# Atomic complete: set completed + increment completed_count + decrement active
LUA_COMPLETE_PARTICIPATION = """
local participation_key = KEYS[1]
local active_count_key = KEYS[2]
local task_key = KEYS[3]

local current_status = redis.call('HGET', participation_key, 'status')
if current_status ~= 'submitted' then
    return redis.error_reply('NOT_SUBMITTED')
end

-- Update participation
redis.call('HSET', participation_key, 'status', 'completed')
redis.call('HSET', participation_key, 'completed_at', ARGV[1])
if ARGV[2] ~= '' then
    redis.call('HSET', participation_key, 'reviewed_by', ARGV[2])
end
if ARGV[3] ~= '' then
    redis.call('HSET', participation_key, 'review_notes', ARGV[3])
end

-- Decrement active, increment completed
redis.call('DECR', active_count_key)
local cnt = tonumber(redis.call('GET', active_count_key) or '0')
if cnt < 0 then redis.call('SET', active_count_key, '0') end

local new_completed = redis.call('HINCRBY', task_key, 'completed_count', 1)

-- Sync active to task hash
local new_active = tonumber(redis.call('GET', active_count_key) or '0')
redis.call('HSET', task_key, 'active_participants_count', tostring(new_active))

return new_completed
"""


class RedisTaskRepository(ITaskRepository):
    """
    Redis-based Task Repository

    Implements ITaskRepository using Redis as storage backend.

    Key Structure — Tasks:
    - acn:task:{task_id} → Hash (task data)
    - acn:tasks:open → SortedSet (task_ids by created_at timestamp)
    - acn:tasks:by_mode:{mode} → Set (task_ids)
    - acn:tasks:by_status:{status} → Set (task_ids)
    - acn:tasks:by_skill:{skill} → Set (task_ids)
    - acn:tasks:by_creator:{creator_id} → Set (task_ids)
    - acn:tasks:by_assignee:{assignee_id} → Set (task_ids)
    - acn:task:completions:{task_id} → Set (agent_ids who completed)
    - acn:task:{task_id}:active_count → Counter (active participations)

    Key Structure — Participations:
    - acn:participation:{participation_id} → Hash (participation data)
    - acn:task:{task_id}:participations → SortedSet (participation_ids by joined_at)
    - acn:user:{user_id}:task:{task_id}:participations → Set (participation_ids for this user+task)
    """

    def __init__(self, redis_client: redis.Redis):
        """
        Initialize Redis Task Repository

        Args:
            redis_client: Redis async client instance
        """
        self.redis = redis_client

        # Register Lua scripts (will be loaded on first use)
        self._join_script: Any | None = None
        self._cancel_script: Any | None = None
        self._complete_script: Any | None = None

    def _get_join_script(self) -> Any:
        if self._join_script is None:
            self._join_script = self.redis.register_script(LUA_JOIN_TASK)
        return self._join_script

    def _get_cancel_script(self) -> Any:
        if self._cancel_script is None:
            self._cancel_script = self.redis.register_script(LUA_CANCEL_PARTICIPATION)
        return self._cancel_script

    def _get_complete_script(self) -> Any:
        if self._complete_script is None:
            self._complete_script = self.redis.register_script(LUA_COMPLETE_PARTICIPATION)
        return self._complete_script

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

        # ===== Update Indices (batched via pipeline to reduce round-trips) =====
        async with self.redis.pipeline(transaction=False) as pipe:
            # 1. Open tasks index (sorted by created_at)
            if task.status == TaskStatus.OPEN:
                timestamp = task.created_at.timestamp()
                pipe.zadd("acn:tasks:open", {task.task_id: timestamp})
            else:
                pipe.zrem("acn:tasks:open", task.task_id)

            # 2. Mode index
            pipe.sadd(f"acn:tasks:by_mode:{task.mode.value}", task.task_id)
            if existing and existing.mode != task.mode:
                pipe.srem(f"acn:tasks:by_mode:{existing.mode.value}", task.task_id)

            # 3. Status index
            pipe.sadd(f"acn:tasks:by_status:{task.status.value}", task.task_id)
            if existing and existing.status != task.status:
                pipe.srem(f"acn:tasks:by_status:{existing.status.value}", task.task_id)

            # 4. Skill indices
            for skill in task.required_skills:
                pipe.sadd(f"acn:tasks:by_skill:{skill}", task.task_id)
            if existing:
                for old_skill in existing.required_skills:
                    if old_skill not in task.required_skills:
                        pipe.srem(f"acn:tasks:by_skill:{old_skill}", task.task_id)

            # 5. Creator index
            pipe.sadd(f"acn:tasks:by_creator:{task.creator_id}", task.task_id)

            # 6. Assignee index
            if task.assignee_id:
                pipe.sadd(f"acn:tasks:by_assignee:{task.assignee_id}", task.task_id)
            if existing and existing.assignee_id and existing.assignee_id != task.assignee_id:
                pipe.srem(f"acn:tasks:by_assignee:{existing.assignee_id}", task.task_id)

            await pipe.execute()

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

    # ========== Participation CRUD ==========

    async def save_participation(self, participation: Participation) -> None:
        """Save or update a participation in Redis"""
        key = f"acn:participation:{participation.participation_id}"
        p_dict = participation.to_dict()

        # Convert lists to JSON strings
        p_dict["submission_artifacts"] = json.dumps(p_dict.get("submission_artifacts", []))

        # Filter None values and convert booleans
        clean = {}
        for k, v in p_dict.items():
            if v is None:
                continue
            elif isinstance(v, bool):
                clean[k] = "true" if v else "false"
            else:
                clean[k] = v

        await self.redis.hset(key, mapping=clean)  # type: ignore[arg-type]

    async def add_application(self, task_id: str, participation: Participation) -> None:
        """Add an application (participation with status APPLIED) for an assigned task."""
        await self.save_participation(participation)
        participations_key = f"acn:task:{task_id}:participations"
        await self.redis.zadd(
            participations_key,
            {participation.participation_id: participation.joined_at.timestamp()},
        )
        user_task_key = f"acn:user:{participation.participant_id}:task:{task_id}:participations"
        await self.redis.sadd(user_task_key, participation.participation_id)
        user_index_key = f"acn:user:{participation.participant_id}:all_participations"
        await self.redis.lpush(user_index_key, participation.participation_id)

    async def find_participation_by_id(self, participation_id: str) -> Participation | None:
        """Find participation by ID"""
        key = f"acn:participation:{participation_id}"
        data = await self.redis.hgetall(key)
        if not data:
            return None
        return self._dict_to_participation(data)

    async def find_participations_by_task(
        self,
        task_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Participation]:
        """Find participations for a task"""
        key = f"acn:task:{task_id}:participations"
        pids = await self.redis.zrevrange(key, offset, offset + limit - 1)

        results = []
        for pid in pids:
            pid_str = pid.decode() if isinstance(pid, bytes) else pid
            p = await self.find_participation_by_id(pid_str)
            if p and (status is None or p.status.value == status):
                results.append(p)

        return results

    async def find_participation_by_user_and_task(
        self,
        task_id: str,
        participant_id: str,
        active_only: bool = True,
    ) -> Participation | None:
        """Find a user's most recent participation in a task"""
        user_task_key = f"acn:user:{participant_id}:task:{task_id}:participations"
        pids = await self.redis.smembers(user_task_key)

        latest: Participation | None = None
        for pid in pids:
            pid_str = pid.decode() if isinstance(pid, bytes) else pid
            p = await self.find_participation_by_id(pid_str)
            if not p:
                continue
            if active_only and p.status not in (
                ParticipationStatus.APPLIED,
                ParticipationStatus.ACTIVE,
                ParticipationStatus.SUBMITTED,
            ):
                continue
            if latest is None or p.joined_at > latest.joined_at:
                latest = p

        return latest

    async def find_participations_by_user(
        self,
        participant_id: str,
        limit: int = 50,
    ) -> list[Participation]:
        """Find all participations for a user (across all tasks).

        Uses a per-user participation index maintained by atomic_join_task.
        Falls back to an empty list if the index key does not exist.
        """
        index_key = f"acn:user:{participant_id}:all_participations"
        participation_ids = await self.redis.lrange(index_key, 0, limit - 1)

        results: list[Participation] = []
        for pid_raw in participation_ids:
            pid = pid_raw.decode() if isinstance(pid_raw, bytes) else pid_raw
            p = await self.find_participation_by_id(pid)
            if p is not None:
                results.append(p)
        return results

    async def atomic_join_task(
        self,
        task_id: str,
        participation: Participation,
        max_completions: int | None,
        allow_repeat: bool,
    ) -> str:
        """Atomically join a multi-participant task using Lua script"""
        script = self._get_join_script()

        task_key = f"acn:task:{task_id}"
        active_count_key = f"acn:task:{task_id}:active_count"
        participations_key = f"acn:task:{task_id}:participations"
        user_task_key = f"acn:user:{participation.participant_id}:task:{task_id}:participations"
        participation_key = f"acn:participation:{participation.participation_id}"

        # Serialize participation data for Lua
        p_dict = participation.to_dict()
        p_dict["submission_artifacts"] = json.dumps(p_dict.get("submission_artifacts", []))
        # Remove None values
        clean = {k: str(v) for k, v in p_dict.items() if v is not None}

        try:
            result = await script(
                keys=[
                    task_key,
                    active_count_key,
                    participations_key,
                    user_task_key,
                    participation_key,
                ],
                args=[
                    max_completions if max_completions is not None else -1,
                    "true" if allow_repeat else "false",
                    participation.participation_id,
                    participation.participant_id,
                    str(participation.joined_at.timestamp()),
                    json.dumps(clean),
                ],
            )
            pid = result.decode() if isinstance(result, bytes) else result
            # Maintain global user participation index for find_participations_by_user
            user_index_key = f"acn:user:{participation.participant_id}:all_participations"
            await self.redis.lpush(user_index_key, pid)
            return pid
        except redis.ResponseError as e:
            err = str(e)
            if "TASK_NOT_OPEN" in err:
                raise ValueError("Task is not open for joining") from e
            elif "TASK_FULL" in err:
                raise ValueError("Task has reached maximum participants") from e
            elif "ALREADY_JOINED" in err:
                raise ValueError("You already have an active participation in this task") from e
            raise

    async def atomic_cancel_participation(
        self,
        participation_id: str,
        task_id: str,
    ) -> None:
        """Atomically cancel participation and decrement active count"""
        script = self._get_cancel_script()

        participation_key = f"acn:participation:{participation_id}"
        active_count_key = f"acn:task:{task_id}:active_count"
        task_key = f"acn:task:{task_id}"

        try:
            await script(
                keys=[participation_key, active_count_key, task_key],
                args=[datetime.now(UTC).isoformat()],
            )
        except redis.ResponseError as e:
            err = str(e)
            if "NOT_FOUND" in err:
                raise ValueError("Participation not found") from e
            elif "CANNOT_CANCEL" in err:
                raise ValueError(
                    "Participation cannot be cancelled (already completed or cancelled)"
                ) from e
            raise

    async def atomic_complete_participation(
        self,
        participation_id: str,
        task_id: str,
        reviewer_id: str | None = None,
        notes: str | None = None,
    ) -> int:
        """Atomically complete participation, increment completed_count, decrement active"""
        script = self._get_complete_script()

        participation_key = f"acn:participation:{participation_id}"
        active_count_key = f"acn:task:{task_id}:active_count"
        task_key = f"acn:task:{task_id}"

        try:
            result = await script(
                keys=[participation_key, active_count_key, task_key],
                args=[
                    datetime.now(UTC).isoformat(),
                    reviewer_id or "",
                    notes or "",
                ],
            )
            return int(result)
        except redis.ResponseError as e:
            if "NOT_SUBMITTED" in str(e):
                raise ValueError("Participation is not in submitted status") from e
            raise

    async def decrement_active_count(self, task_id: str) -> int:
        """Decrement active participant count for a task; floors at 0. Returns new count."""
        active_key = f"acn:task:{task_id}:active_count"
        task_key = f"acn:task:{task_id}"
        new_count = await self.redis.decr(active_key)
        if new_count < 0:
            await self.redis.set(active_key, 0)
            new_count = 0
        await self.redis.hset(task_key, "active_participants_count", str(new_count))
        return new_count

    async def count_active_participations(self, task_id: str) -> int:
        """Count active participations for a task"""
        key = f"acn:task:{task_id}:active_count"
        count = await self.redis.get(key)
        return int(count) if count else 0

    async def batch_cancel_participations(self, task_id: str) -> int:
        """Cancel all active/submitted participations for a task"""
        participations_key = f"acn:task:{task_id}:participations"
        pids = await self.redis.zrange(participations_key, 0, -1)

        cancelled = 0
        for pid in pids:
            pid_str = pid.decode() if isinstance(pid, bytes) else pid
            p = await self.find_participation_by_id(pid_str)
            if p and p.status in (ParticipationStatus.ACTIVE, ParticipationStatus.SUBMITTED):
                try:
                    await self.atomic_cancel_participation(pid_str, task_id)
                    cancelled += 1
                except ValueError:
                    pass  # Already cancelled/completed — skip

        return cancelled

    # ========== Helpers ==========

    def _dict_to_participation(self, data: dict) -> Participation:
        """Convert Redis hash dict to Participation entity"""
        decoded = {}
        for k, v in data.items():
            key = k.decode() if isinstance(k, bytes) else k
            val = v.decode() if isinstance(v, bytes) else v
            decoded[key] = val

        return Participation.from_dict(decoded)

    def _dict_to_task(self, task_dict: dict) -> Task:
        """Convert Redis dict to Task entity"""
        # Decode bytes
        data = {}
        for k, v in task_dict.items():
            key = k.decode() if isinstance(k, bytes) else k
            val = v.decode() if isinstance(v, bytes) else v
            data[key] = val

        # Parse JSON fields — guard against corrupted Redis values
        def _safe_loads(raw: str, default: Any) -> Any:
            try:
                return json.loads(raw) if raw else default
            except (json.JSONDecodeError, TypeError):
                import logging as _logging

                _logging.getLogger(__name__).warning(
                    "task_repository: corrupted JSON field, using default",
                    extra={"raw": raw[:200] if raw else None},
                )
                return default

        data["required_skills"] = _safe_loads(data.get("required_skills", ""), [])
        data["submission_artifacts"] = _safe_loads(data.get("submission_artifacts", ""), [])
        data["metadata"] = _safe_loads(data.get("metadata", ""), {})

        # Parse enums
        data["mode"] = TaskMode(data["mode"])
        data["status"] = TaskStatus(data["status"])

        # Parse booleans
        data["is_repeatable"] = data.get("is_repeatable", "false").lower() == "true"
        data["is_multi_participant"] = data.get("is_multi_participant", "false").lower() == "true"
        data["allow_repeat_by_same"] = data.get("allow_repeat_by_same", "false").lower() == "true"
        data["payment_released"] = data.get("payment_released", "false").lower() == "true"

        # Parse integers
        data["completed_count"] = int(data.get("completed_count", 0))
        data["active_participants_count"] = int(data.get("active_participants_count", 0))
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
                try:
                    data[field_name] = datetime.fromisoformat(data[field_name])
                except (ValueError, TypeError):
                    import logging as _logging

                    _logging.getLogger(__name__).warning(
                        "task_repository: invalid datetime field, discarding",
                        extra={"field": field_name, "value": data[field_name]},
                    )
                    data.pop(field_name, None)
            else:
                data.pop(field_name, None)

        return Task(**data)
