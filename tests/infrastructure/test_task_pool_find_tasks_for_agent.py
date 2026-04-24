"""Regression tests for SCALE_AUDIT P2-4: TaskPool.find_tasks_for_agent
used to call ``repo.find_open_tasks(limit=limit*2)`` with no tags and
then re-filter in Python.

After the fix there is exactly one filtering layer: tags are pushed
down to ``find_open_tasks(tags=...)`` and no over-fetching happens.
"""

from unittest.mock import AsyncMock

import pytest

from acn.core.entities import Task
from acn.infrastructure.task_pool import TaskPool


def _task(task_id: str, tags: list[str]) -> Task:
    # Minimal Task; TaskPool only inspects what the repo returns.
    return Task(
        task_id=task_id,
        title=f"t-{task_id}",
        description="",
        required_tags=tags,
        creator_id="creator",
        creator_type="user",
        creator_name="tester",
        task_type="generic",
    )


@pytest.mark.asyncio
async def test_find_tasks_for_agent_pushes_tags_to_repo():
    repo = AsyncMock()
    repo.find_open_tasks = AsyncMock(return_value=[_task("1", ["ai", "ml"])])

    pool = TaskPool(repo)

    result = await pool.find_tasks_for_agent(agent_tags=["ai", "ml"], limit=20)

    # Exactly one repo call, with tags forwarded and limit unchanged
    # (no 2× hedge fan-out).
    repo.find_open_tasks.assert_awaited_once()
    kwargs = repo.find_open_tasks.call_args.kwargs
    assert kwargs["tags"] == ["ai", "ml"], (
        "tags must be pushed down to the repository layer so scale "
        "behavior moves with repo-side indexing"
    )
    assert kwargs["limit"] == 20, (
        "limit must be forwarded verbatim — the previous ×2 hedge was "
        "the anti-pattern we removed"
    )
    # No other filter kwargs should be set — we don't want to hide
    # mode/task_type filtering behind this helper.
    assert "mode" not in kwargs or kwargs["mode"] is None
    assert "task_type" not in kwargs or kwargs["task_type"] is None

    assert len(result) == 1
    assert result[0].task_id == "1"


@pytest.mark.asyncio
async def test_find_tasks_for_agent_does_not_refilter_results():
    """Repo is the single source of truth for tag matching. The pool
    must not second-guess the repo by re-filtering in Python — that's
    what caused the duplicated filtering layer in the first place."""

    repo = AsyncMock()
    # Simulate a repo that (for whatever reason) returns a task whose
    # tags don't match what we asked for. The pool must pass it
    # through, not silently drop it.
    surprising_task = _task("surprise", ["unrelated"])
    repo.find_open_tasks = AsyncMock(return_value=[surprising_task])

    pool = TaskPool(repo)

    result = await pool.find_tasks_for_agent(agent_tags=["ai"], limit=20)

    assert result == [surprising_task], (
        "TaskPool must trust the repository's filter result; re-running "
        "matches_tags() in Python is exactly the double-work P2-4 was "
        "about"
    )


@pytest.mark.asyncio
async def test_find_tasks_for_agent_respects_empty_result():
    repo = AsyncMock()
    repo.find_open_tasks = AsyncMock(return_value=[])

    pool = TaskPool(repo)

    result = await pool.find_tasks_for_agent(agent_tags=["nothing"], limit=20)

    assert result == []
    repo.find_open_tasks.assert_awaited_once()
