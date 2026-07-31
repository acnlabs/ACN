"""AgentService upsert / refresh performance + TaskService hook."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.entities import Agent, Task
from acn.core.entities.task import TaskStatus
from acn.infrastructure.task_pool import TaskPool
from acn.services.agent_service import AgentService
from acn.services.task_service import TaskService


def _submitted_task(
    *,
    task_id: str = "task-perf",
    creator_id: str = "creator-001",
    assignee_id: str = "agt_assignee",
) -> Task:
    return Task(
        task_id=task_id,
        creator_type="human",
        creator_id=creator_id,
        creator_name="Alice",
        title=f"Task {task_id}",
        description="Test task",
        reward="50",
        reward_currency="usd",
        max_participants=1,
        assignee_id=assignee_id,
        status=TaskStatus.SUBMITTED,
    )


@pytest.mark.asyncio
async def test_upsert_performance_replaces_block_keeps_other_keys() -> None:
    repo = AsyncMock()
    agent = Agent(
        agent_id="agt_1",
        name="Test Agent",
        description="x" * 20,
        endpoint="https://example.com",
        owner=None,
        metadata={
            "visibility": "real",
            "performance": {"settled": 9, "success": 9, "stale": True},
        },
    )
    repo.get = AsyncMock(return_value=agent)
    repo.save = AsyncMock()

    svc = AgentService(agent_repository=repo)
    # get_agent uses repository — wire common pattern
    svc.get_agent = AsyncMock(return_value=agent)

    await svc.upsert_performance(
        "agt_1",
        {"settled": 3, "success": 3, "completion_rate": 1.0},
    )
    assert agent.metadata["visibility"] == "real"
    assert agent.metadata["performance"]["completion_rate"] == 1.0
    assert "stale" not in agent.metadata["performance"]
    repo.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_performance_from_history() -> None:
    repo = AsyncMock()
    agent = Agent(
        agent_id="agt_1",
        name="Test Agent",
        description="x" * 20,
        endpoint="https://example.com",
        owner=None,
        metadata={},
    )
    svc = AgentService(agent_repository=repo)
    svc.get_agent = AsyncMock(return_value=agent)
    repo.save = AsyncMock()

    items = [
        {"status": "completed"},
        {"status": "completed"},
        {"status": "rejected"},
    ]
    perf = await svc.refresh_performance_from_history("agt_1", items)
    assert perf["completion_rate"] == round(2 / 3, 4)
    assert agent.metadata["performance"]["settled"] == 3


@pytest.mark.asyncio
async def test_task_service_refresh_hook_best_effort() -> None:
    task_repo = AsyncMock()
    agent_svc = AsyncMock()
    agent_svc.refresh_performance_from_history = AsyncMock(
        return_value={"settled": 0, "success": 0}
    )
    ts = TaskService(repository=task_repo, agent_service=agent_svc)
    ts.get_agent_task_history = AsyncMock(return_value=[])

    await ts._refresh_agent_performance("agt_x")
    agent_svc.refresh_performance_from_history.assert_awaited_once()

    # Failure must not raise
    agent_svc.refresh_performance_from_history = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    await ts._refresh_agent_performance("agt_x")

    # No agent_service → no-op
    ts2 = TaskService(repository=task_repo, agent_service=None)
    await ts2._refresh_agent_performance("agt_x")


@pytest.mark.asyncio
async def test_complete_task_calls_refresh_agent_performance() -> None:
    task = _submitted_task()
    task_repo = AsyncMock()
    task_repo.find_by_id = AsyncMock(return_value=task)
    task_repo.compare_and_save = AsyncMock(return_value=True)
    task_pool = AsyncMock(spec=TaskPool)
    task_pool.record_completion = AsyncMock(return_value=None)

    ts = TaskService(
        repository=task_repo,
        task_pool=task_pool,
        agent_service=AsyncMock(),
    )
    ts._notify_webhook = AsyncMock()
    ts._dissolve_task_scoped_subnets = AsyncMock()
    ts._refresh_agent_performance = AsyncMock()

    await ts.complete_task(task_id=task.task_id, approver_id="creator-001")
    ts._refresh_agent_performance.assert_awaited_once_with("agt_assignee")


@pytest.mark.asyncio
async def test_reject_task_calls_refresh_agent_performance() -> None:
    task = _submitted_task()
    task_repo = AsyncMock()
    task_repo.find_by_id = AsyncMock(return_value=task)
    task_repo.compare_and_save = AsyncMock(return_value=True)

    ts = TaskService(
        repository=task_repo,
        task_pool=AsyncMock(spec=TaskPool),
        agent_service=AsyncMock(),
        activity_service=MagicMock(
            record_task_rejected=AsyncMock(return_value=None),
        ),
    )
    ts._notify_webhook = AsyncMock()
    ts._dissolve_task_scoped_subnets = AsyncMock()
    ts._refresh_agent_performance = AsyncMock()

    await ts.reject_task(
        task_id=task.task_id,
        reviewer_id="creator-001",
        notes="nope",
    )
    ts._refresh_agent_performance.assert_awaited_once_with("agt_assignee")
