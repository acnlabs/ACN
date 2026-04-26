"""Redis Implementation of Task Repository

Concrete implementation using Redis for task persistence.
"""

import json
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as redis  # type: ignore[import-untyped]

from ....core.entities import Participation, ParticipationStatus, Task, TaskStatus
from ....core.interfaces import ITaskRepository

# Cap for the per-user global participation index
# `acn:user:{uid}:all_participations`. The read path (`find_participations_by_user`)
# only exposes the head via `lrange(0, limit-1)` with `limit<=50`, so anything
# beyond a small multiple of that is dead weight. We keep an order-of-magnitude
# headroom so concurrent writers + the occasional deep-lookup still succeed.
_ALL_PARTICIPATIONS_CAP = 500

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
# Atomic CAS on the task ``status`` field.
#
# Used by ``compare_and_save`` (security audit H3) to make single-participant
# state-machine transitions race-safe. The script either flips ``status`` from
# ``expected`` → ``new`` (returning 1) or refuses (returning 0). The caller
# then proceeds with a normal multi-field ``save()`` only when CAS won; that
# leaves a tiny window where ``status`` is committed but other columns aren't,
# but ``status`` is the source of truth for the state-machine, so any partial
# write is at worst a fixable inconsistency, never a double-pay.
LUA_CAS_TASK_STATUS = """
local task_key = KEYS[1]
local current = redis.call('HGET', task_key, 'status')
if not current then return 0 end
if current ~= ARGV[1] then return 0 end
redis.call('HSET', task_key, 'status', ARGV[2])
return 1
"""

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
    - acn:tasks:by_tag:{tag} → Set (task_ids)
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
        self._cas_status_script: Any | None = None

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

    def _get_cas_status_script(self) -> Any:
        if self._cas_status_script is None:
            self._cas_status_script = self.redis.register_script(LUA_CAS_TASK_STATUS)
        return self._cas_status_script

    async def save(self, task: Task) -> None:
        """Save or update a task in Redis"""
        task_key = f"acn:task:{task.task_id}"

        # Check for existing task to clean up old indices
        existing = await self.find_by_id(task.task_id)

        # Serialize task to dict
        task_dict = task.to_dict()

        # Convert lists/dicts to JSON strings for Redis
        task_dict["required_tags"] = json.dumps(task_dict.get("required_tags", []))
        task_dict["submission_artifacts"] = json.dumps(task_dict.get("submission_artifacts", []))
        task_dict["invited_agent_ids"] = json.dumps(task_dict.get("invited_agent_ids", []))
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

            # 2. Mode index (legacy — computed from require_join_approval)
            db_mode = "assigned" if task.require_join_approval else "open"
            pipe.sadd(f"acn:tasks:by_mode:{db_mode}", task.task_id)

            # 3. Status index
            pipe.sadd(f"acn:tasks:by_status:{task.status.value}", task.task_id)
            if existing and existing.status != task.status:
                pipe.srem(f"acn:tasks:by_status:{existing.status.value}", task.task_id)

            # 4. Tag indices
            for skill in task.required_tags:
                pipe.sadd(f"acn:tasks:by_tag:{skill}", task.task_id)
            if existing:
                for old_skill in existing.required_tags:
                    if old_skill not in task.required_tags:
                        pipe.srem(f"acn:tasks:by_tag:{old_skill}", task.task_id)

            # 5. Creator index
            pipe.sadd(f"acn:tasks:by_creator:{task.creator_id}", task.task_id)

            # 6. Assignee index
            if task.assignee_id:
                pipe.sadd(f"acn:tasks:by_assignee:{task.assignee_id}", task.task_id)
            if existing and existing.assignee_id and existing.assignee_id != task.assignee_id:
                pipe.srem(f"acn:tasks:by_assignee:{existing.assignee_id}", task.task_id)

            await pipe.execute()

    async def compare_and_save(self, task: Task, expected_status: TaskStatus) -> bool:
        """CAS save — security audit H3.

        Two-phase implementation: a Lua script atomically flips the
        ``status`` hash field iff the current value matches ``expected_status``.
        Only when CAS wins do we proceed to the regular ``save()`` for the
        rest of the fields and indexes.

        Why two-phase rather than rewriting all of ``save`` inside a Lua
        script? ``save()`` touches half a dozen indexes (open / by_status /
        by_tag / by_creator / by_assignee / mode) and translating that into
        Lua would duplicate ~80 lines of logic that has to track the existing
        task's *previous* indexes for cleanup. A successful CAS pins the
        state-machine outcome; the index updates that follow are convergent.
        Two losing concurrent callers can never *both* trigger payments.
        """
        task_key = f"acn:task:{task.task_id}"
        cas = self._get_cas_status_script()
        won = await cas(
            keys=[task_key],
            args=[expected_status.value, task.status.value],
        )
        if not won:
            return False
        await self.save(task)
        return True

    async def find_by_id(self, task_id: str) -> Task | None:
        """Find task by ID"""
        task_key = f"acn:task:{task_id}"
        task_dict = await self.redis.hgetall(task_key)

        if not task_dict:
            return None

        return self._dict_to_task(task_dict)

    async def find_open_tasks(
        self,
        mode: str | None = None,
        tags: list[str] | None = None,
        task_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        requesting_agent_id: str | None = None,
    ) -> list[Task]:
        """Find open tasks with optional filters"""
        # Pre-compute subnet visibility for the requesting agent.
        #
        # The previous implementation tried to iterate `acn:subnets:all`,
        # but that index was never written (no `SADD` anywhere in the
        # codebase), and it also used the wrong subnet hash key
        # (`acn:subnet:{sid}` vs the real `acn:subnets:info:{sid}`). Net
        # effect: `visible_subnet_ids` was always empty, so every task
        # with a non-null `subnet_id` was invisible to every agent —
        # private subnets were effectively broken at scale.
        #
        # The correct source of truth is already on the agent itself:
        # `agent.subnet_ids` is the exact set of subnets it belongs to
        # (public included). One HGET, no fan-out over all subnets, no
        # new index to keep in sync on create/delete.
        visible_subnet_ids: set[str] = set()
        if requesting_agent_id:
            raw = await self.redis.hget(
                f"acn:agents:{requesting_agent_id}", "subnet_ids"
            )
            if raw:
                if isinstance(raw, bytes):
                    raw = raw.decode()
                try:
                    visible_subnet_ids = set(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    visible_subnet_ids = set()

        task_ids = await self.redis.zrevrange("acn:tasks:open", offset, offset + limit - 1)

        tasks = []
        for task_id in task_ids:
            task_id = task_id.decode() if isinstance(task_id, bytes) else task_id
            task = await self.find_by_id(task_id)
            if not task:
                continue

            # Apply filters
            if mode:
                task_mode = "assigned" if task.require_join_approval else "open"
                if task_mode != mode:
                    continue
            if tags and not task.matches_tags(tags):
                continue
            if task_type and task.task_type != task_type:
                continue

            # Subnet visibility: public (no subnet_id) or agent is a member
            if task.subnet_id:
                if not requesting_agent_id or task.subnet_id not in visible_subnet_ids:
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

    async def find_by_group(self, group_id: str, limit: int = 100) -> list[Task]:
        """Find tasks by collaboration group_id (scan-based, suitable for small data sets)"""
        all_task_ids = await self.redis.zrange("acn:tasks:open", 0, -1)
        tasks = []
        for task_id in all_task_ids[:limit * 5]:  # over-fetch to account for filtering
            task_id = task_id.decode() if isinstance(task_id, bytes) else task_id
            task = await self.find_by_id(task_id)
            if task and task.group_id == group_id:
                tasks.append(task)
                if len(tasks) >= limit:
                    break
        return tasks

    async def delete(self, task_id: str) -> bool:
        """Delete a task and all its participation side-car keys.

        Without this full sweep, deleting a task that had any participants
        would leak `acn:participation:*`, `acn:user:*:task:{id}:participations`,
        `acn:user:*:all_participations` list entries, and
        `acn:task:{id}:active_count` counters forever (they have no TTL).

        Performance: participation hashes are fetched in a single pipeline
        batch (step 1), and all user-index lrem calls are also pipelined
        (step 4), reducing round-trips from O(pids) + O(users × pids) to
        three pipeline batches regardless of participant count.
        """
        task = await self.find_by_id(task_id)
        if not task:
            return False

        # 1. Collect all participation IDs, then fetch their hashes in one
        #    pipeline batch to get participant_ids without N serial HGETALL calls.
        participations_key = f"acn:task:{task_id}:participations"
        raw_pids = await self.redis.zrange(participations_key, 0, -1)
        pids: list[str] = [p.decode() if isinstance(p, bytes) else p for p in raw_pids]

        participant_ids: set[str] = set()
        if pids:
            async with self.redis.pipeline(transaction=False) as pipe:
                for pid in pids:
                    pipe.hgetall(f"acn:participation:{pid}")
                results = await pipe.execute()
            for raw_data in results:
                if raw_data:
                    try:
                        p = self._dict_to_participation(raw_data)
                        participant_ids.add(p.participant_id)
                    except Exception:
                        pass

        # 2. Primary task key and all task-level index entries — single pipeline.
        _mode = "assigned" if task.require_join_approval else "open"
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.delete(f"acn:task:{task_id}")
            pipe.zrem("acn:tasks:open", task_id)
            pipe.srem(f"acn:tasks:by_mode:{_mode}", task_id)
            pipe.srem(f"acn:tasks:by_status:{task.status.value}", task_id)
            pipe.srem(f"acn:tasks:by_creator:{task.creator_id}", task_id)
            if task.assignee_id:
                pipe.srem(f"acn:tasks:by_assignee:{task.assignee_id}", task_id)
            for skill in task.required_tags:
                pipe.srem(f"acn:tasks:by_tag:{skill}", task_id)
            # 3. Participation side-car keys in the same batch.
            if pids:
                pipe.delete(*[f"acn:participation:{pid}" for pid in pids])
            pipe.delete(participations_key)
            pipe.delete(f"acn:task:{task_id}:active_count")
            pipe.delete(f"acn:task:completions:{task_id}")
            await pipe.execute()

        # 4. User-scoped indices: delete the per-(user,task) participation set
        #    and lrem each pid from the user's global list.  All lrem calls are
        #    batched in a single pipeline so O(users × pids) round-trips become
        #    one network call regardless of participant count.
        if participant_ids:
            async with self.redis.pipeline(transaction=False) as pipe:
                for uid in participant_ids:
                    pipe.delete(f"acn:user:{uid}:task:{task_id}:participations")
                    user_index_key = f"acn:user:{uid}:all_participations"
                    for pid in pids:
                        pipe.lrem(user_index_key, 0, pid)
                await pipe.execute()

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
        await self.redis.ltrim(user_index_key, 0, _ALL_PARTICIPATIONS_CAP - 1)

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
            await self.redis.ltrim(user_index_key, 0, _ALL_PARTICIPATIONS_CAP - 1)
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

        data["required_tags"] = _safe_loads(data.get("required_tags", ""), [])
        data["submission_artifacts"] = _safe_loads(data.get("submission_artifacts", ""), [])
        data["invited_agent_ids"] = _safe_loads(data.get("invited_agent_ids", ""), [])
        data["metadata"] = _safe_loads(data.get("metadata", ""), {})

        # Parse status enum
        data["status"] = TaskStatus(data["status"])

        # Parse booleans (new fields; compat with legacy string values)
        def _bool(val: str, default: bool = False) -> bool:
            return val.lower() == "true" if val else default

        data["require_join_approval"] = _bool(
            data.get("require_join_approval", ""),
            # compat: old Redis may have mode="assigned" instead
            data.get("mode", "open") == "assigned",
        )
        data["auto_approve"] = _bool(data.get("auto_approve", ""))
        data["allow_repeat_by_same"] = _bool(data.get("allow_repeat_by_same", ""))
        data["use_escrow"] = _bool(data.get("use_escrow", ""))
        data["completion_mode"] = data.get("completion_mode") or "independent"
        data["group_id"] = data.get("group_id") or None

        # Parse integers
        data["completed_count"] = int(data.get("completed_count", 0))
        data["active_participants_count"] = int(data.get("active_participants_count", 0))
        # max_participants (new name); compat: fall back to max_completions
        raw_max = data.get("max_participants") or data.get("max_completions")
        data["max_participants"] = int(raw_max) if raw_max else (
            None if _bool(data.get("is_multi_participant", "")) else 1
        )

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

        # Strip unknown fields to survive schema drift (e.g. old Redis entries
        # may contain fields that were later renamed or removed, such as
        # 'required_skills' which was superseded by 'required_tags').
        import dataclasses as _dc
        valid_fields = {f.name for f in _dc.fields(Task)}
        data = {k: v for k, v in data.items() if k in valid_fields}

        return Task(**data)
