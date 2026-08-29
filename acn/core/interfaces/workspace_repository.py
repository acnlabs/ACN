"""Execution Workspace repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..entities.workspace import Workspace, WorkspaceAttestation


class IWorkspaceRepository(ABC):
    @abstractmethod
    async def save_workspace(self, workspace: Workspace) -> None:
        """Insert or update a workspace.

        Implementations must reject a second **active** ``admit=task``
        workspace for the same ``task_id``, and a second **active**
        ``admit=org`` workspace for the same ``org_id`` (D15). Raise
        ``WorkspaceAlreadyActiveError``. Closed rows do not occupy the
        slot. ``admit=allowlist`` is unrestricted.
        """

    @abstractmethod
    async def find_workspace(self, workspace_id: str) -> Workspace | None:
        """Find by id."""

    @abstractmethod
    async def find_active_by_task_id(self, task_id: str) -> Workspace | None:
        """The active ``admit=task`` workspace for this task, if any."""

    @abstractmethod
    async def find_active_by_org_id(self, org_id: str) -> Workspace | None:
        """The active ``admit=org`` workspace for this org, if any."""

    @abstractmethod
    async def save_attestation(self, attestation: WorkspaceAttestation) -> None:
        """Insert an owner attestation."""

    @abstractmethod
    async def find_attestation(
        self, attestation_id: str
    ) -> WorkspaceAttestation | None:
        """Find one attestation."""
