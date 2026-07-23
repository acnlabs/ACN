"""OrgService.import_work_from_task — Task → Org work bridge."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from acn.core.entities.org import Org, OrgOwner, OrgPrincipal, OrgWorkItem
from acn.core.entities.task import Task, TaskStatus
from acn.core.exceptions import OrgConflictError
from acn.services.org_service import OrgService, OrgTaskImportError


@pytest.fixture
def mock_org_repo():
    repo = AsyncMock()
    repo.find_org_by_subnet = AsyncMock(return_value=None)
    repo.find_work = AsyncMock(return_value=None)
    repo.save_work = AsyncMock()
    o = Org(
        org_id="org_x",
        display_name="X",
        subnet_id="fence-x",
        created_by=OrgPrincipal(kind="agent", subject="agt_gov"),
        owner=OrgOwner(kind="none"),
        steward_agent_id="agt_gov",
        plugins={"work": "builtin_work"},
    )
    repo.find_org = AsyncMock(return_value=o)
    return repo


@pytest.fixture
def mock_subnet_service():
    return AsyncMock()


@pytest.fixture
def mock_agent_service():
    return AsyncMock()


@pytest.fixture
def mock_task_repo():
    return AsyncMock()


@pytest.fixture
def svc(mock_org_repo, mock_subnet_service, mock_agent_service, mock_task_repo):
    return OrgService(
        org_repository=mock_org_repo,
        subnet_service=mock_subnet_service,
        agent_service=mock_agent_service,
        webhook_service=AsyncMock(),
        task_repository=mock_task_repo,
    )


def _task(*, task_id: str = "task_1", title: str = "Do the thing", meta=None, slug=None):
    return Task(
        task_id=task_id,
        status=TaskStatus.OPEN,
        creator_type="agent",
        creator_id="agt_other",
        creator_name="other",
        title=title,
        description="x" * 12,
        metadata=dict(meta or {}),
        subnet_slug=slug,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_import_creates_work_and_links_metadata(svc, mock_org_repo, mock_task_repo):
    task = _task(meta={"org_publish": True, "org_id": "org_x"})
    mock_task_repo.find_by_id = AsyncMock(return_value=task)
    mock_task_repo.save = AsyncMock()

    work, already = await svc.import_work_from_task(
        "org_x",
        task_id="task_1",
        caller_type="agent",
        caller_sub="agt_gov",
    )
    assert already is False
    assert work.title == "Do the thing"
    assert task.metadata["org_work_id"] == work.work_id
    assert task.metadata["org_id"] == "org_x"
    assert task.metadata["org_import"] is True
    mock_task_repo.save.assert_awaited()
    mock_org_repo.save_work.assert_awaited()


@pytest.mark.asyncio
async def test_import_idempotent(svc, mock_org_repo, mock_task_repo):
    existing = OrgWorkItem(
        work_id="work_abc",
        org_id="org_x",
        title="Do the thing",
    )
    task = _task(meta={"org_id": "org_x", "org_work_id": "work_abc", "org_import": True})
    mock_task_repo.find_by_id = AsyncMock(return_value=task)
    mock_org_repo.find_work = AsyncMock(return_value=existing)

    work, already = await svc.import_work_from_task(
        "org_x",
        task_id="task_1",
        caller_type="agent",
        caller_sub="agt_gov",
    )
    assert already is True
    assert work.work_id == "work_abc"
    mock_task_repo.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_import_conflict_other_org(svc, mock_task_repo):
    task = _task(meta={"org_id": "org_other", "org_work_id": "work_z"})
    mock_task_repo.find_by_id = AsyncMock(return_value=task)
    with pytest.raises(OrgConflictError) as ei:
        await svc.import_work_from_task(
            "org_x",
            task_id="task_1",
            caller_type="agent",
            caller_sub="agt_gov",
        )
    assert ei.value.reason == "task_already_imported"


@pytest.mark.asyncio
async def test_import_fenced_requires_membership(svc, mock_subnet_service, mock_task_repo):
    task = _task(slug="private-net")
    mock_task_repo.find_by_id = AsyncMock(return_value=task)
    mock_subnet_service.get_subnet = AsyncMock(
        return_value=SimpleNamespace(member_agent_ids={"someone_else"})
    )
    with pytest.raises(OrgTaskImportError) as ei:
        await svc.import_work_from_task(
            "org_x",
            task_id="task_1",
            caller_type="agent",
            caller_sub="agt_gov",
        )
    assert ei.value.reason == "not_subnet_member"


@pytest.mark.asyncio
async def test_import_without_task_repo(mock_org_repo, mock_subnet_service, mock_agent_service):
    svc = OrgService(
        org_repository=mock_org_repo,
        subnet_service=mock_subnet_service,
        agent_service=mock_agent_service,
        task_repository=None,
    )
    with pytest.raises(OrgTaskImportError) as ei:
        await svc.import_work_from_task(
            "org_x",
            task_id="task_1",
            caller_type="agent",
            caller_sub="agt_gov",
        )
    assert ei.value.reason == "task_repository_unavailable"
