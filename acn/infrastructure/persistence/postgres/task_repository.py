"""PostgreSQL Implementation of ITaskRepository

Persistent task storage backed by Railway PostgreSQL.
active_participants_count is NOT stored here — Redis Counter is authoritative.
"""

from datetime import UTC, datetime

import redis.asyncio as aioredis
import structlog
from sqlalchemy import String, cast, delete, func, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ....core.entities import Participation, Task, TaskMode, TaskStatus
from ....core.entities.task import ParticipationStatus
from ....core.interfaces import ITaskRepository
from .models import ParticipationModel, TaskModel

logger = structlog.get_logger()

# Redis key helpers (mirrors RedisTaskRepository conventions)
_ACTIVE_COUNT_KEY = "acn:task:{task_id}:active_count"
_COMPLETIONS_KEY = "acn:task:completions:{task_id}"


def _tz(dt: datetime | None) -> datetime | None:
    """Ensure a datetime is timezone-aware (UTC). asyncpg rejects naive datetimes."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class PostgresTaskRepository(ITaskRepository):
    """
    PostgreSQL-backed TaskRepository.

    - Task / Participation data → PostgreSQL (durable)
    - active_participants_count  → Redis Counter (real-time)
    - Completion set             → Redis Set (fast has_completed lookups)
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: aioredis.Redis,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis_client

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _active_count_key(self, task_id: str) -> str:
        return _ACTIVE_COUNT_KEY.format(task_id=task_id)

    def _completions_key(self, task_id: str) -> str:
        return _COMPLETIONS_KEY.format(task_id=task_id)

    # ---- Task mapping -------------------------------------------------------

    def _model_to_task(self, row: TaskModel, active_count: int = 0) -> Task:
        meta = row.task_metadata or {}
        return Task(
            task_id=row.task_id,
            mode=TaskMode(row.mode),
            status=TaskStatus(row.status),
            creator_id=row.creator_id,
            creator_type=row.creator_type,
            creator_name=meta.get("creator_name", ""),
            title=row.title,
            description=row.description or "",
            task_type=meta.get("task_type", "general"),
            required_skills=list(row.required_skills or []),
            assignee_id=row.assignee_id,
            assignee_name=meta.get("assignee_name"),
            assigned_at=meta.get("assigned_at")
            and datetime.fromisoformat(meta["assigned_at"]),
            submission=meta.get("submission"),
            submission_artifacts=meta.get("submission_artifacts", []),
            submitted_at=meta.get("submitted_at")
            and datetime.fromisoformat(meta["submitted_at"]),
            review_notes=meta.get("review_notes"),
            reviewed_by=meta.get("reviewed_by"),
            reward_amount=row.reward_amount,
            reward_currency=row.reward_currency,
            payment_task_id=meta.get("payment_task_id"),
            reward_unit=meta.get("reward_unit", "completion"),
            total_budget=meta.get("total_budget", "0"),
            released_amount=meta.get("released_amount", "0"),
            is_multi_participant=row.is_multi_participant,
            allow_repeat_by_same=meta.get("allow_repeat_by_same", False),
            is_repeatable=row.is_multi_participant,
            completed_count=row.completed_count,
            max_completions=row.max_completions,
            active_participants_count=active_count,
            created_at=row.created_at,
            deadline=row.deadline,
            completed_at=meta.get("completed_at")
            and datetime.fromisoformat(meta["completed_at"]),
            approval_type=meta.get("approval_type", "manual"),
            validator_id=meta.get("validator_id"),
            metadata=meta.get("extra_metadata", {}),
        )

    def _task_to_model(self, task: Task) -> TaskModel:
        """Convert Task entity → ORM model (upsert-safe)."""
        # Overflow fields not in dedicated columns go into metadata JSONB
        extra_meta: dict = {
            "creator_name": task.creator_name,
            "task_type": task.task_type,
            "submission": task.submission,
            "submission_artifacts": task.submission_artifacts,
            "submitted_at": task.submitted_at.isoformat() if task.submitted_at else None,
            "review_notes": task.review_notes,
            "reviewed_by": task.reviewed_by,
            "payment_task_id": task.payment_task_id,
            "reward_unit": task.reward_unit,
            "total_budget": task.total_budget,
            "released_amount": task.released_amount,
            "allow_repeat_by_same": task.allow_repeat_by_same,
            "assignee_name": task.assignee_name,
            "assigned_at": task.assigned_at.isoformat() if task.assigned_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "approval_type": task.approval_type,
            "validator_id": task.validator_id,
            "extra_metadata": task.metadata,
        }
        return TaskModel(
            task_id=task.task_id,
            mode=task.mode.value,
            status=task.status.value,
            creator_id=task.creator_id,
            creator_type=task.creator_type,
            title=task.title,
            description=task.description,
            reward_amount=task.reward_amount,
            reward_currency=task.reward_currency,
            assignee_id=task.assignee_id,
            is_multi_participant=task.is_multi_participant,
            max_completions=task.max_completions,
            completed_count=task.completed_count,
            required_skills=task.required_skills or None,
            created_at=_tz(task.created_at) or datetime.now(UTC),
            deadline=_tz(task.deadline),
            task_metadata=extra_meta,
        )

    # ---- Participation mapping -----------------------------------------------

    def _model_to_participation(self, row: ParticipationModel) -> Participation:
        return Participation(
            participation_id=row.participation_id,
            task_id=row.task_id,
            participant_id=row.participant_id,
            participant_name=row.participant_name,
            participant_type=row.participant_type,
            status=ParticipationStatus(row.status),
            joined_at=row.joined_at,
            submission=row.submission,
            submission_artifacts=row.submission_artifacts or [],
            submitted_at=row.submitted_at,
            rejection_reason=row.rejection_reason,
            rejected_at=row.rejected_at,
            reject_response_deadline=row.reject_response_deadline,
            review_request_id=row.review_request_id,
            review_notes=row.review_notes,
            reviewed_by=row.reviewed_by,
            cancelled_at=row.cancelled_at,
            completed_at=row.completed_at,
        )

    def _participation_to_model(self, p: Participation) -> ParticipationModel:
        return ParticipationModel(
            participation_id=p.participation_id,
            task_id=p.task_id,
            participant_id=p.participant_id,
            participant_name=p.participant_name,
            participant_type=p.participant_type,
            status=p.status.value,
            joined_at=_tz(p.joined_at) or datetime.now(UTC),
            submission=p.submission,
            submission_artifacts=p.submission_artifacts or None,
            submitted_at=_tz(p.submitted_at),
            rejection_reason=p.rejection_reason,
            rejected_at=_tz(p.rejected_at),
            reject_response_deadline=_tz(p.reject_response_deadline),
            review_request_id=p.review_request_id,
            review_notes=p.review_notes,
            reviewed_by=p.reviewed_by,
            cancelled_at=_tz(p.cancelled_at),
            completed_at=_tz(p.completed_at),
        )

    # =========================================================================
    # Task CRUD
    # =========================================================================

    async def save(self, task: Task) -> None:
        model = self._task_to_model(task)
        async with self._session_factory() as session:
            existing = await session.get(TaskModel, task.task_id)
            if existing:
                # Update all mutable columns
                await session.execute(
                    update(TaskModel)
                    .where(TaskModel.task_id == task.task_id)
                    .values(
                        mode=model.mode,
                        status=model.status,
                        creator_id=model.creator_id,
                        creator_type=model.creator_type,
                        title=model.title,
                        description=model.description,
                        reward_amount=model.reward_amount,
                        reward_currency=model.reward_currency,
                        assignee_id=model.assignee_id,
                        is_multi_participant=model.is_multi_participant,
                        max_completions=model.max_completions,
                        completed_count=model.completed_count,
                        required_skills=model.required_skills,
                        deadline=model.deadline,
                        task_metadata=model.task_metadata,
                    )
                )
            else:
                session.add(model)
            await session.commit()

    async def find_by_id(self, task_id: str) -> Task | None:
        async with self._session_factory() as session:
            row = await session.get(TaskModel, task_id)
            if not row:
                return None
            active = int(await self._redis.get(self._active_count_key(task_id)) or 0)
            return self._model_to_task(row, active)

    async def find_open_tasks(
        self,
        mode: TaskMode | None = None,
        skills: list[str] | None = None,
        task_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        async with self._session_factory() as session:
            stmt = select(TaskModel).where(TaskModel.status == TaskStatus.OPEN.value)
            if mode:
                stmt = stmt.where(TaskModel.mode == mode.value)
            if skills:
                # PostgreSQL ARRAY containment: required_skills @> ARRAY[skill1, skill2]
                stmt = stmt.where(
                    TaskModel.required_skills.contains(cast(skills, ARRAY(String)))
                )
            if task_type:
                # task_type is stored in JSONB metadata column
                stmt = stmt.where(
                    TaskModel.task_metadata["task_type"].astext == task_type
                )
            stmt = stmt.order_by(TaskModel.created_at.desc()).limit(limit).offset(offset)
            result = await session.execute(stmt)
            rows = result.scalars().all()

        return await self._rows_to_tasks(rows)

    async def _rows_to_tasks(self, rows: list[TaskModel]) -> list[Task]:
        """Convert ORM rows to Task entities, batch-fetching active counts via pipeline."""
        if not rows:
            return []
        async with self._redis.pipeline(transaction=False) as pipe:
            for row in rows:
                pipe.get(self._active_count_key(row.task_id))
            counts = await pipe.execute()
        return [
            self._model_to_task(row, int(cnt or 0))
            for row, cnt in zip(rows, counts, strict=True)
        ]

    async def find_by_creator(self, creator_id: str, limit: int = 50) -> list[Task]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TaskModel)
                .where(TaskModel.creator_id == creator_id)
                .order_by(TaskModel.created_at.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
        return await self._rows_to_tasks(rows)

    async def find_by_assignee(self, assignee_id: str, limit: int = 50) -> list[Task]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TaskModel)
                .where(TaskModel.assignee_id == assignee_id)
                .order_by(TaskModel.created_at.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
        return await self._rows_to_tasks(rows)

    async def find_by_status(self, status: TaskStatus, limit: int = 50) -> list[Task]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TaskModel)
                .where(TaskModel.status == status.value)
                .order_by(TaskModel.created_at.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
        return await self._rows_to_tasks(rows)

    async def delete(self, task_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(TaskModel).where(TaskModel.task_id == task_id)
            )
            await session.commit()
            return result.rowcount > 0

    async def exists(self, task_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TaskModel.task_id).where(TaskModel.task_id == task_id)
            )
            return result.scalar() is not None

    async def count_open_tasks(self) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count()).where(TaskModel.status == TaskStatus.OPEN.value)
            )
            return result.scalar() or 0

    async def record_completion(self, task_id: str, agent_id: str) -> None:
        """Track which agents have completed this task (uses Redis Set for fast lookups)."""
        await self._redis.sadd(self._completions_key(task_id), agent_id)

    async def has_completed(self, task_id: str, agent_id: str) -> bool:
        return bool(await self._redis.sismember(self._completions_key(task_id), agent_id))

    # =========================================================================
    # Participation CRUD
    # =========================================================================

    async def save_participation(self, participation: Participation) -> None:
        model = self._participation_to_model(participation)
        async with self._session_factory() as session:
            existing = await session.get(ParticipationModel, participation.participation_id)
            if existing:
                await session.execute(
                    update(ParticipationModel)
                    .where(ParticipationModel.participation_id == participation.participation_id)
                    .values(
                        status=model.status,
                        submission=model.submission,
                        submission_artifacts=model.submission_artifacts,
                        submitted_at=model.submitted_at,
                        rejection_reason=model.rejection_reason,
                        rejected_at=model.rejected_at,
                        reject_response_deadline=model.reject_response_deadline,
                        review_request_id=model.review_request_id,
                        review_notes=model.review_notes,
                        reviewed_by=model.reviewed_by,
                        cancelled_at=model.cancelled_at,
                        completed_at=model.completed_at,
                    )
                )
            else:
                session.add(model)
            await session.commit()

    async def add_application(self, task_id: str, participation: Participation) -> None:
        """Save an APPLIED participation without touching the Redis active count."""
        await self.save_participation(participation)

    async def find_participation_by_id(self, participation_id: str) -> Participation | None:
        async with self._session_factory() as session:
            row = await session.get(ParticipationModel, participation_id)
            return self._model_to_participation(row) if row else None

    async def find_participations_by_task(
        self,
        task_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Participation]:
        async with self._session_factory() as session:
            stmt = (
                select(ParticipationModel)
                .where(ParticipationModel.task_id == task_id)
                .order_by(ParticipationModel.joined_at.asc())
                .limit(limit)
                .offset(offset)
            )
            if status:
                stmt = stmt.where(ParticipationModel.status == status)
            result = await session.execute(stmt)
            return [self._model_to_participation(r) for r in result.scalars().all()]

    async def find_participation_by_user_and_task(
        self,
        task_id: str,
        participant_id: str,
        active_only: bool = True,
    ) -> Participation | None:
        async with self._session_factory() as session:
            stmt = select(ParticipationModel).where(
                ParticipationModel.task_id == task_id,
                ParticipationModel.participant_id == participant_id,
            )
            if active_only:
                stmt = stmt.where(
                    ParticipationModel.status.in_(
                        [ParticipationStatus.ACTIVE.value, ParticipationStatus.SUBMITTED.value]
                    )
                )
            stmt = stmt.order_by(ParticipationModel.joined_at.desc()).limit(1)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return self._model_to_participation(row) if row else None

    async def find_participations_by_user(
        self,
        participant_id: str,
        limit: int = 50,
    ) -> list[Participation]:
        async with self._session_factory() as session:
            stmt = (
                select(ParticipationModel)
                .where(ParticipationModel.participant_id == participant_id)
                .order_by(ParticipationModel.joined_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [self._model_to_participation(r) for r in result.scalars().all()]

    # =========================================================================
    # Atomic Operations (PostgreSQL SELECT FOR UPDATE + Redis counter)
    # =========================================================================

    async def atomic_join_task(
        self,
        task_id: str,
        participation: Participation,
        max_completions: int | None,
        allow_repeat: bool,
    ) -> str:
        """Join task atomically using PG row-level lock + PG count (no Redis race window).

        Capacity is enforced by counting active participations directly from PG
        inside the row lock, guaranteeing serialised access under concurrent joins.
        The Redis counter is synced after commit for display/fast-read purposes.
        """
        async with self._session_factory() as session:
            async with session.begin():
                # Lock the task row — serialises concurrent joins for the same task
                result = await session.execute(
                    select(TaskModel)
                    .where(TaskModel.task_id == task_id)
                    .with_for_update()
                )
                task_row = result.scalar_one_or_none()
                if not task_row:
                    raise ValueError("TASK_NOT_FOUND")
                if task_row.status != TaskStatus.OPEN.value:
                    raise ValueError("TASK_NOT_OPEN")

                # Count active participations from PG (consistent with lock, no Redis race)
                if max_completions is not None:
                    active_result = await session.execute(
                        select(func.count())
                        .select_from(ParticipationModel)
                        .where(
                            ParticipationModel.task_id == task_id,
                            ParticipationModel.status.in_(
                                [
                                    ParticipationStatus.ACTIVE.value,
                                    ParticipationStatus.SUBMITTED.value,
                                ]
                            ),
                        )
                    )
                    active = active_result.scalar() or 0
                    if (task_row.completed_count + active) >= max_completions:
                        raise ValueError("TASK_FULL")

                # Duplicate check
                if not allow_repeat:
                    dup = await session.execute(
                        select(ParticipationModel).where(
                            ParticipationModel.task_id == task_id,
                            ParticipationModel.participant_id == participation.participant_id,
                            ParticipationModel.status.in_(
                                [ParticipationStatus.ACTIVE.value, ParticipationStatus.SUBMITTED.value]
                            ),
                        )
                    )
                    if dup.scalar_one_or_none():
                        raise ValueError("ALREADY_JOINED")

                # Persist participation
                session.add(self._participation_to_model(participation))

        # Sync Redis counter after commit (best-effort, for display performance)
        await self._redis.incr(self._active_count_key(task_id))
        return participation.participation_id

    async def atomic_cancel_participation(
        self,
        participation_id: str,
        task_id: str,
    ) -> None:
        now_iso = datetime.now(UTC).isoformat()
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    select(ParticipationModel)
                    .where(ParticipationModel.participation_id == participation_id)
                    .with_for_update()
                )
                row = result.scalar_one_or_none()
                if not row:
                    raise ValueError("NOT_FOUND")
                if row.status in (ParticipationStatus.COMPLETED.value, ParticipationStatus.CANCELLED.value):
                    raise ValueError("CANNOT_CANCEL")

                was_active = row.status in (
                    ParticipationStatus.ACTIVE.value, ParticipationStatus.SUBMITTED.value
                )
                row.status = ParticipationStatus.CANCELLED.value
                row.cancelled_at = datetime.fromisoformat(now_iso)

        if was_active:
            # Decrement Redis counter, floor at 0
            key = self._active_count_key(task_id)
            new_val = await self._redis.decr(key)
            if new_val < 0:
                await self._redis.set(key, 0)

    async def atomic_complete_participation(
        self,
        participation_id: str,
        task_id: str,
    ) -> int:
        now_iso = datetime.now(UTC).isoformat()
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    select(ParticipationModel)
                    .where(ParticipationModel.participation_id == participation_id)
                    .with_for_update()
                )
                row = result.scalar_one_or_none()
                if not row:
                    raise ValueError("NOT_FOUND")
                if row.status != ParticipationStatus.SUBMITTED.value:
                    raise ValueError("NOT_SUBMITTED")

                row.status = ParticipationStatus.COMPLETED.value
                row.completed_at = datetime.fromisoformat(now_iso)

                # Atomically increment completed_count at the DB level to prevent
                # race conditions when multiple completions occur concurrently.
                count_result = await session.execute(
                    update(TaskModel)
                    .where(TaskModel.task_id == task_id)
                    .values(completed_count=TaskModel.completed_count + 1)
                    .returning(TaskModel.completed_count)
                )
                new_count = count_result.scalar() or 1

        # Decrement Redis active counter
        key = self._active_count_key(task_id)
        new_val = await self._redis.decr(key)
        if new_val < 0:
            await self._redis.set(key, 0)

        return new_count

    async def count_active_participations(self, task_id: str) -> int:
        return int(await self._redis.get(self._active_count_key(task_id)) or 0)

    async def batch_cancel_participations(self, task_id: str) -> int:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            result = await session.execute(
                select(ParticipationModel).where(
                    ParticipationModel.task_id == task_id,
                    ParticipationModel.status.in_(
                        [ParticipationStatus.ACTIVE.value, ParticipationStatus.SUBMITTED.value]
                    ),
                )
            )
            rows = result.scalars().all()
            for row in rows:
                row.status = ParticipationStatus.CANCELLED.value
                row.cancelled_at = now
            await session.commit()

        count = len(rows)
        if count:
            await self._redis.set(self._active_count_key(task_id), 0)
        return count

    async def decrement_active_count(self, task_id: str) -> int:
        key = self._active_count_key(task_id)
        new_val = await self._redis.decr(key)
        if new_val < 0:
            await self._redis.set(key, 0)
            return 0
        return new_val
