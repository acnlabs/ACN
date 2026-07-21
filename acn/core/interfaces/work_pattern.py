"""IWorkPattern — Org Harness Work Graph port (Phase 2 / ADR-0014 D7)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..entities.org import OrgWorkItem, WorkStatus


class IWorkPattern(ABC):
    """How an Org models and mutates work items.

    Control Loop (``IOrgLoop`` / thin tick) **reads** via this port; it never
    executes L1 tools. Kernel identity / membership stay outside.
    """

    #: Canonical plugin id (e.g. ``builtin_work``).
    plugin_id: str

    @abstractmethod
    async def create_work(
        self,
        org_id: str,
        *,
        title: str,
        assignee_agent_id: str | None = None,
    ) -> OrgWorkItem:
        """Create a work item in ``todo`` status."""

    @abstractmethod
    async def update_work(
        self,
        org_id: str,
        work_id: str,
        *,
        status: WorkStatus,
        assignee_agent_id: str | None = None,
    ) -> OrgWorkItem:
        """Update status and optional assignee."""

    @abstractmethod
    async def list_work(
        self,
        org_id: str,
        *,
        open_only: bool = False,
    ) -> list[OrgWorkItem]:
        """List work items; ``open_only`` → todo / in_progress."""
