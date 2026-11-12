from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from acn.core.entities.task import Task, TaskStatus
from acn.infrastructure.persistence.redis.task_repository import RedisTaskRepository


@pytest.mark.asyncio
async def test_board_pagination_filters_status_and_breaks_timestamp_ties(monkeypatch):
    redis = AsyncMock()
    redis.sunion.return_value = {b"a", b"b", b"c", b"closed", b"other"}
    repository = RedisTaskRepository(redis)
    tasks = {
        task_id: Task(
            task_id=task_id,
            creator_type="human",
            creator_id="owner",
            creator_name="Owner",
            title="Example",
            description="Example",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            status=TaskStatus.CANCELLED if task_id == "closed" else TaskStatus.OPEN,
            metadata={"board_id": "other" if task_id == "other" else "board"},
        )
        for task_id in ("a", "b", "c", "closed", "other")
    }
    monkeypatch.setattr(repository, "find_by_id", AsyncMock(side_effect=tasks.__getitem__))

    first = await repository.find_by_board("board", limit=2, status=TaskStatus.OPEN)
    second = await repository.find_by_board("board", limit=2, offset=2, status=TaskStatus.OPEN)

    assert [task.task_id for task in first] == ["c", "b"]
    assert [task.task_id for task in second] == ["a"]
