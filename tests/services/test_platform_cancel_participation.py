"""The platform may drop a seat that was never submitted. A submitted seat stays."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from acn.core.entities.task import Participation, ParticipationStatus
from acn.core.interfaces.task_repository import ITaskRepository
from acn.services.task_service import TaskService, TaskStatusConflict


def _participation(**overrides) -> Participation:
    data = {
        "participation_id": "p-1",
        "task_id": "task-1",
        "participant_id": "agent-1",
        "participant_name": "Agent",
        "status": ParticipationStatus.ACTIVE,
    }
    data.update(overrides)
    return Participation(**data)


@pytest.fixture
def service() -> TaskService:
    pool = AsyncMock()
    pool.cancel_participation.return_value = None
    pool.get_participation.return_value = _participation()
    built = TaskService(repository=AsyncMock(spec=ITaskRepository), task_pool=pool)
    built.get_task = AsyncMock()
    return built


@pytest.mark.asyncio
async def test_platform_cancels_an_active_seat(service: TaskService):
    await service.cancel_participation("task-1", "p-1", "backend@internal", platform=True)
    service.task_pool.cancel_participation.assert_awaited_once_with(
        "p-1", "task-1", expected_status="active"
    )


@pytest.mark.asyncio
async def test_platform_leaves_a_submitted_seat(service: TaskService):
    service.task_pool.get_participation.return_value = _participation(
        status=ParticipationStatus.SUBMITTED,
        submitted_at=datetime.now(UTC),
    )
    with pytest.raises(TaskStatusConflict):
        await service.cancel_participation("task-1", "p-1", "backend@internal", platform=True)
    service.task_pool.cancel_participation.assert_not_awaited()
