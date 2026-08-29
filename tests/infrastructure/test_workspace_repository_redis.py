"""RedisWorkspaceRepository regressions — D15 uniqueness (one active slot).

Postgres enforces the invariant with unique partial indexes; Redis claims
``acn:exec_workspace:by_task:{id}`` / ``by_org:{id}`` with ``SET NX``.
These tests pin:

- second save on the same task / org raises ``WorkspaceAlreadyActiveError``;
- a **closed** holder releases the pointer so a new workspace may bind;
- a **dangling** pointer (payload deleted out-of-band) is evicted;
- ``admit=allowlist`` does not occupy a uniqueness slot.

fakeredis runs in bytes mode by default; production composes the client
with ``decode_responses=True`` (see ``acn/api.py``), so the fixture
mirrors that.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fakeredis import aioredis as fakeredis_async

from acn.core.entities.workspace import Workspace
from acn.core.exceptions import WorkspaceAlreadyActiveError
from acn.infrastructure.persistence.redis.workspace_repository import (
    RedisWorkspaceRepository,
    _task_ptr,
    _ws_key,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def fake_redis():
    client = fakeredis_async.FakeRedis(decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()


@pytest.fixture
def repo(fake_redis):
    return RedisWorkspaceRepository(fake_redis)


def _ws(
    workspace_id: str,
    *,
    admit: str = "task",
    task_id: str | None = "task-1",
    org_id: str | None = None,
    status: str = "active",
) -> Workspace:
    return Workspace(
        workspace_id=workspace_id,
        owner_agent_id="agt_owner",
        display_name=f"WS {workspace_id}",
        execution_env={"kind": "git", "uri": "https://github.com/acme/s.git"},
        admit=admit,  # type: ignore[arg-type]
        org_id=org_id,
        task_id=task_id,
        status=status,  # type: ignore[arg-type]
        created_at=datetime(2026, 8, 28, tzinfo=UTC),
        updated_at=datetime(2026, 8, 28, tzinfo=UTC),
    )


async def test_second_task_workspace_conflicts(repo, fake_redis):
    await repo.save_workspace(_ws("ws_a"))
    with pytest.raises(WorkspaceAlreadyActiveError) as ei:
        await repo.save_workspace(_ws("ws_b"))
    assert ei.value.bind_kind == "task"
    assert ei.value.bind_id == "task-1"
    assert ei.value.existing_workspace_id == "ws_a"
    assert await repo.find_workspace("ws_b") is None
    assert await fake_redis.get(_task_ptr("task-1")) == "ws_a"


async def test_second_org_workspace_conflicts(repo):
    await repo.save_workspace(
        _ws("ws_a", admit="org", org_id="org_1", task_id=None)
    )
    with pytest.raises(WorkspaceAlreadyActiveError) as ei:
        await repo.save_workspace(
            _ws("ws_b", admit="org", org_id="org_1", task_id=None)
        )
    assert ei.value.bind_kind == "org"
    assert ei.value.existing_workspace_id == "ws_a"
    assert await repo.find_workspace("ws_b") is None


async def test_closed_task_releases_slot(repo):
    ws = _ws("ws_a")
    await repo.save_workspace(ws)
    ws.status = "closed"
    await repo.save_workspace(ws)
    assert await repo.find_active_by_task_id("task-1") is None
    await repo.save_workspace(_ws("ws_b"))
    assert (await repo.find_active_by_task_id("task-1")).workspace_id == "ws_b"


async def test_dangling_pointer_is_evicted(repo, fake_redis):
    await fake_redis.set(_task_ptr("task-1"), "ws_ghost")
    await repo.save_workspace(_ws("ws_a"))
    assert (await repo.find_active_by_task_id("task-1")).workspace_id == "ws_a"
    assert await fake_redis.get(_task_ptr("task-1")) == "ws_a"


async def test_allowlist_not_unique(repo):
    await repo.save_workspace(
        _ws("ws_a", admit="allowlist", task_id=None, org_id=None)
    )
    await repo.save_workspace(
        _ws("ws_b", admit="allowlist", task_id=None, org_id=None)
    )
    assert await repo.find_workspace("ws_a") is not None
    assert await repo.find_workspace("ws_b") is not None
    assert await repo.find_active_by_task_id("task-1") is None
    assert await repo.find_active_by_org_id("org_1") is None


async def test_idempotent_resave_same_id(repo, fake_redis):
    ws = _ws("ws_a")
    await repo.save_workspace(ws)
    ws.display_name = "Renamed"
    await repo.save_workspace(ws)
    assert await fake_redis.get(_task_ptr("task-1")) == "ws_a"
    assert (await repo.find_workspace("ws_a")).display_name == "Renamed"
    assert await fake_redis.exists(_ws_key("ws_a"))
