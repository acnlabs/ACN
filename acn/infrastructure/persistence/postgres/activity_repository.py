"""PostgreSQL Implementation of IActivityRepository

Full persistent activity storage — no row limit, no TTL.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ....core.interfaces.activity_repository import IActivityRepository
from .models import ActivityModel


class PostgresActivityRepository(IActivityRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # =========================================================================
    # Mapping
    # =========================================================================

    def _model_to_dict(self, row: ActivityModel) -> dict[str, Any]:
        d: dict[str, Any] = {
            "event_id": row.event_id,
            "type": row.type,
            "actor_type": row.actor_type,
            "actor_id": row.actor_id,
            "actor_name": row.actor_name,
            "description": row.description,
            "timestamp": row.timestamp.isoformat(),
        }
        if row.points is not None:
            d["points"] = row.points
        if row.task_id:
            d["task_id"] = row.task_id
        if row.event_metadata:
            d["metadata"] = row.event_metadata
        return d

    # =========================================================================
    # IActivityRepository
    # =========================================================================

    async def save(
        self,
        event_id: str,
        event_type: str,
        actor_type: str,
        actor_id: str,
        actor_name: str,
        description: str,
        timestamp: str,
        points: int | None = None,
        task_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        ts = datetime.fromisoformat(timestamp)
        if not ts.tzinfo:
            ts = ts.replace(tzinfo=UTC)

        model = ActivityModel(
            event_id=event_id,
            type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            actor_name=actor_name,
            description=description,
            points=points,
            task_id=task_id,
            event_metadata=metadata,
            timestamp=ts,
        )
        async with self._session_factory() as session:
            existing = await session.get(ActivityModel, event_id)
            if not existing:
                session.add(model)
                await session.commit()

    async def find_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ActivityModel)
                .order_by(ActivityModel.timestamp.desc())
                .limit(limit)
            )
            return [self._model_to_dict(r) for r in result.scalars().all()]

    async def find_by_user(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ActivityModel)
                .where(ActivityModel.actor_id == user_id)
                .order_by(ActivityModel.timestamp.desc())
                .limit(limit)
            )
            return [self._model_to_dict(r) for r in result.scalars().all()]

    async def find_by_task(self, task_id: str, limit: int = 50) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ActivityModel)
                .where(ActivityModel.task_id == task_id)
                .order_by(ActivityModel.timestamp.desc())
                .limit(limit)
            )
            return [self._model_to_dict(r) for r in result.scalars().all()]

    async def find_by_agent(self, agent_id: str, limit: int = 20) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ActivityModel)
                .where(
                    ActivityModel.actor_type == "agent",
                    ActivityModel.actor_id == agent_id,
                )
                .order_by(ActivityModel.timestamp.desc())
                .limit(limit)
            )
            return [self._model_to_dict(r) for r in result.scalars().all()]

    async def find_by_agents(
        self, agent_ids: list[str], limit: int = 20
    ) -> list[dict[str, Any]]:
        if not agent_ids:
            return []
        async with self._session_factory() as session:
            result = await session.execute(
                select(ActivityModel)
                .where(
                    ActivityModel.actor_type == "agent",
                    ActivityModel.actor_id.in_(agent_ids),
                )
                .order_by(ActivityModel.timestamp.desc())
                .limit(limit)
            )
            return [self._model_to_dict(r) for r in result.scalars().all()]
