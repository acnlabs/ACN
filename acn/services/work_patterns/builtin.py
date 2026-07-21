"""builtin_work — Phase 1 OrgWorkItem storage behind IWorkPattern."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from ...core.entities.org import OrgWorkItem, WorkStatus
from ...core.interfaces.org_repository import IOrgRepository
from ...core.interfaces.work_pattern import IWorkPattern


class BuiltinWorkPattern(IWorkPattern):
    """Default Work Graph: Org-local work items (Redis / Postgres org repos)."""

    plugin_id = "builtin_work"

    def __init__(self, repository: IOrgRepository) -> None:
        self._repo = repository

    async def create_work(
        self,
        org_id: str,
        *,
        title: str,
        assignee_agent_id: str | None = None,
    ) -> OrgWorkItem:
        now = datetime.now(UTC)
        work = OrgWorkItem(
            work_id=f"work_{uuid4().hex[:16]}",
            org_id=org_id,
            title=title,
            assignee_agent_id=assignee_agent_id,
            status="todo",
            created_at=now,
            updated_at=now,
        )
        await self._repo.save_work(work)
        return work

    async def update_work(
        self,
        org_id: str,
        work_id: str,
        *,
        status: WorkStatus,
        assignee_agent_id: str | None = None,
    ) -> OrgWorkItem:
        # Local import avoids circular import with OrgService.
        from ..org_service import OrgWorkNotFoundError

        work = await self._repo.find_work(org_id, work_id)
        if not work:
            raise OrgWorkNotFoundError(org_id, work_id)
        work.status = status
        if assignee_agent_id is not None:
            work.assignee_agent_id = assignee_agent_id
        work.updated_at = datetime.now(UTC)
        await self._repo.save_work(work)
        return work

    async def list_work(
        self,
        org_id: str,
        *,
        open_only: bool = False,
    ) -> list[OrgWorkItem]:
        return await self._repo.list_work(org_id, open_only=open_only)
