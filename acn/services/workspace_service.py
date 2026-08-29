"""Execution Workspace service (exec-workspace-v0)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from ..core.entities.org import normalize_execution_env
from ..core.entities.workspace import (
    Workspace,
    WorkspaceAttestation,
    normalize_workspace_execution_env,
)
from ..core.exceptions import AgentNotFoundException, WorkspaceAlreadyActiveError
from ..core.interfaces.task_repository import ITaskRepository
from ..core.interfaces.workspace_repository import IWorkspaceRepository
from ..core.entities.task import ParticipationStatus, TaskStatus
from .agent_service import AgentService
from .org_service import (
    CallerType,
    OrgNotFoundError,
    OrgPermissionError,
    OrgService,
)


class WorkspaceNotFoundError(Exception):
    def __init__(self, workspace_id: str) -> None:
        super().__init__(workspace_id)
        self.workspace_id = workspace_id


class WorkspacePermissionError(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class WorkspaceConflictError(Exception):
    """409: an active workspace already occupies this task / org slot (D15)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _conflict_from_active(existing: Workspace, bind_kind: str) -> WorkspaceConflictError:
    reason = (
        "task_workspace_active"
        if bind_kind == "task"
        else "org_workspace_active"
    )
    return WorkspaceConflictError(
        reason,
        f"{bind_kind} already has active workspace {existing.workspace_id}",
    )


def _conflict_from_repo(exc: WorkspaceAlreadyActiveError) -> WorkspaceConflictError:
    reason = (
        "task_workspace_active"
        if exc.bind_kind == "task"
        else "org_workspace_active"
    )
    return WorkspaceConflictError(reason, str(exc))


def _task_publisher_org_id(task: Any) -> str | None:
    """Org the task is hung on — ``creator_type=org`` only.

    ``metadata.org_id`` is client-attributed (org-task-bridge-v0) and is
    **not** a hang. Trusting it would let a spoofed field grant Org
    steward bind/read on a stranger's task.
    """
    if getattr(task, "creator_type", None) == "org" and task.creator_id:
        return task.creator_id
    return None


class WorkspaceService:
    def __init__(
        self,
        workspace_repository: IWorkspaceRepository,
        agent_service: AgentService,
        org_service: OrgService | None = None,
        task_repository: ITaskRepository | None = None,
    ) -> None:
        self.repository = workspace_repository
        self.agent_service = agent_service
        self.org_service = org_service
        self.task_repository = task_repository

    async def create_workspace(
        self,
        *,
        caller_type: CallerType,
        caller_sub: str,
        display_name: str,
        execution_env: dict[str, Any],
        admit: Literal["org", "task", "allowlist"],
        org_id: str | None = None,
        task_id: str | None = None,
        allowlist: list[str] | None = None,
    ) -> Workspace:
        if caller_type != "agent":
            raise WorkspacePermissionError(
                "agent_only", "Only a registered agent can own a workspace"
            )
        try:
            await self.agent_service.get_agent(caller_sub)
        except AgentNotFoundException as e:
            raise WorkspacePermissionError(
                "agent_only", "Only a registered agent can own a workspace"
            ) from e

        env = normalize_workspace_execution_env(execution_env)

        org = None
        if admit == "org":
            if not org_id or not self.org_service:
                raise ValueError("admit=org requires org_id")
            org = await self.org_service.get_org(org_id)
            self._assert_may_register_for_org(org, caller_type, caller_sub)
            existing = await self.repository.find_active_by_org_id(org_id)
            if existing is not None:
                raise _conflict_from_active(existing, "org")
        elif admit == "task":
            if not task_id:
                raise ValueError("admit=task requires task_id")
            if not self.task_repository:
                raise ValueError("admit=task requires task lookup")
            task = await self.task_repository.find_by_id(task_id)
            if task is None:
                raise ValueError(f"task not found: {task_id}")
            await self._assert_may_bind_task(task, caller_type, caller_sub)
            existing = await self.repository.find_active_by_task_id(task_id)
            if existing is not None:
                raise _conflict_from_active(existing, "task")

        now = datetime.now(UTC)
        workspace = Workspace(
            workspace_id=f"ws_{uuid4().hex}",
            owner_agent_id=caller_sub,
            display_name=display_name,
            execution_env=env,
            admit=admit,
            org_id=org_id if admit == "org" else None,
            task_id=task_id if admit == "task" else None,
            allowlist=list(allowlist or []),
            status="active",
            created_at=now,
            updated_at=now,
        )
        try:
            await self.repository.save_workspace(workspace)
        except WorkspaceAlreadyActiveError as e:
            raise _conflict_from_repo(e) from e
        if admit == "org" and org is not None and self.org_service:
            previous_env = org.execution_env
            bound = {
                **workspace.execution_env,
                "workspace_id": workspace.workspace_id,
            }
            org.execution_env = normalize_execution_env(bound)
            org.updated_at = now
            try:
                await self.org_service.repository.save_org(org)
            except Exception:
                org.execution_env = previous_env
                workspace.status = "closed"
                workspace.updated_at = datetime.now(UTC)
                await self.repository.save_workspace(workspace)
                raise
        return workspace

    def _assert_may_register_for_org(
        self,
        org: Any,
        caller_type: CallerType,
        caller_sub: str,
    ) -> None:
        """D12: named steward agent, else Org governance (agent owner / unclaimed created_by).

        Human-owned Orgs fail ``_require_governance`` for an agent caller;
        the named ``steward_agent_id`` is the workspace owner.
        """
        if not self.org_service:
            raise WorkspacePermissionError(
                "task_publisher_only",
                "Only the task publisher can bind a workspace",
            )
        if getattr(org, "status", None) == "dissolved":
            self.org_service._require_governance(org, caller_type, caller_sub)
        if caller_type == "agent" and caller_sub == org.steward_agent_id:
            return
        self.org_service._require_governance(org, caller_type, caller_sub)

    async def _assert_may_bind_task(
        self,
        task: Any,
        caller_type: CallerType,
        caller_sub: str,
    ) -> None:
        """Agent creator, Org governor, or the human publisher's own agent."""
        if task.creator_type == "agent" and task.creator_id == caller_sub:
            return
        if task.creator_type == "agent":
            raise WorkspacePermissionError(
                "task_publisher_only",
                "Only the task publisher can bind a workspace",
            )
        org_id = _task_publisher_org_id(task)
        if org_id and self.org_service:
            try:
                org = await self.org_service.get_org(org_id)
                self._assert_may_register_for_org(org, caller_type, caller_sub)
                return
            except (OrgPermissionError, OrgNotFoundError):
                pass
        if task.creator_type == "human":
            try:
                agent = await self.agent_service.get_agent(caller_sub)
            except AgentNotFoundException:
                agent = None
            if agent is not None and agent.owner == task.creator_id:
                return
        raise WorkspacePermissionError(
            "task_publisher_only",
            "Only the task publisher can bind a workspace",
        )

    async def get_workspace(
        self,
        workspace_id: str,
        *,
        caller_type: CallerType,
        caller_sub: str,
    ) -> Workspace:
        workspace = await self.repository.find_workspace(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(workspace_id)
        if not await self._may_read(workspace, caller_type, caller_sub):
            raise WorkspaceNotFoundError(workspace_id)
        return workspace

    async def create_attestation(
        self,
        workspace_id: str,
        *,
        caller_type: CallerType,
        caller_sub: str,
        agent_id: str,
        run_id: str,
        work_id: str | None = None,
        task_id: str | None = None,
        hop_id: str | None = None,
        artifact: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> WorkspaceAttestation:
        workspace = await self.repository.find_workspace(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(workspace_id)
        if not await self._may_read(workspace, caller_type, caller_sub):
            raise WorkspaceNotFoundError(workspace_id)
        if caller_type != "agent" or workspace.owner_agent_id != caller_sub:
            raise WorkspacePermissionError(
                "owner_only", "Only the workspace owner agent can attest"
            )
        if workspace.status != "active":
            raise WorkspacePermissionError(
                "workspace_closed", "Workspace is closed"
            )

        kind = (workspace.execution_env or {}).get("kind")
        if usage is not None:
            if kind != "url":
                raise ValueError("usage is only allowed when execution_env.kind is url")
            if not isinstance(usage, dict):
                raise ValueError("usage must be an object")

        attestation = WorkspaceAttestation(
            attestation_id=f"att_{uuid4().hex}",
            workspace_id=workspace.workspace_id,
            agent_id=agent_id,
            run_id=run_id,
            work_id=work_id,
            task_id=task_id,
            hop_id=hop_id,
            artifact=artifact,
            usage=usage,
        )
        await self.repository.save_attestation(attestation)
        return attestation

    async def get_attestation(
        self,
        workspace_id: str,
        attestation_id: str,
        *,
        caller_type: CallerType,
        caller_sub: str,
    ) -> WorkspaceAttestation:
        await self.get_workspace(
            workspace_id,
            caller_type=caller_type,
            caller_sub=caller_sub,
        )
        attestation = await self.repository.find_attestation(attestation_id)
        if attestation is None or attestation.workspace_id != workspace_id:
            raise WorkspaceNotFoundError(workspace_id)
        return attestation

    async def close_workspace(
        self,
        workspace_id: str,
        *,
        caller_type: CallerType,
        caller_sub: str,
    ) -> Workspace:
        workspace = await self.repository.find_workspace(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(workspace_id)
        if not await self._may_read(workspace, caller_type, caller_sub):
            raise WorkspaceNotFoundError(workspace_id)
        if caller_type != "agent" or caller_sub != workspace.owner_agent_id:
            raise WorkspacePermissionError(
                "owner_only", "Only the workspace owner agent can close"
            )
        if workspace.status != "closed":
            workspace.status = "closed"
            workspace.updated_at = datetime.now(UTC)
            await self.repository.save_workspace(workspace)
        await self._unbind_org_pointer(workspace)
        return workspace

    async def _unbind_org_pointer(self, workspace: Workspace) -> None:
        """Drop Org ``execution_env.workspace_id`` when it still names this room."""
        if workspace.admit != "org" or not workspace.org_id or not self.org_service:
            return
        try:
            org = await self.org_service.get_org(workspace.org_id)
        except OrgNotFoundError:
            return
        env = dict(org.execution_env or {})
        if env.get("workspace_id") != workspace.workspace_id:
            return
        env.pop("workspace_id", None)
        org.execution_env = normalize_execution_env(env)
        org.updated_at = datetime.now(UTC)
        await self.org_service.repository.save_org(org)

    async def _human_org_governor_may_read(
        self,
        org_id: str | None,
        caller_type: CallerType,
        caller_sub: str,
    ) -> bool:
        if caller_type != "human" or not org_id or not self.org_service:
            return False
        try:
            org = await self.org_service.get_org(org_id)
        except OrgNotFoundError:
            return False
        return self.org_service._is_owner(
            org, caller_type, caller_sub
        ) or self.org_service._is_created_by(org, caller_type, caller_sub)

    async def _human_task_publisher_may_read(
        self,
        workspace: Workspace,
        caller_type: CallerType,
        caller_sub: str,
    ) -> bool:
        if caller_type != "human" or workspace.admit != "task":
            return False
        if not workspace.task_id or not self.task_repository:
            return False
        task = await self.task_repository.find_by_id(workspace.task_id)
        if task is None:
            return False
        if task.creator_type == "human" and task.creator_id == caller_sub:
            return True
        return await self._human_org_governor_may_read(
            _task_publisher_org_id(task), caller_type, caller_sub
        )

    async def _may_read(
        self,
        workspace: Workspace,
        caller_type: CallerType,
        caller_sub: str,
    ) -> bool:
        if caller_type == "internal":
            return True
        if caller_type == "agent" and caller_sub == workspace.owner_agent_id:
            return True
        if workspace.admit == "org" and await self._human_org_governor_may_read(
            workspace.org_id, caller_type, caller_sub
        ):
            return True
        if await self._human_task_publisher_may_read(
            workspace, caller_type, caller_sub
        ):
            return True
        if caller_type != "agent":
            return False
        if workspace.status != "active":
            return False
        if workspace.admit == "allowlist":
            return caller_sub in workspace.allowlist
        if workspace.admit == "org":
            if not workspace.org_id or not self.org_service:
                return False
            membership = await self.org_service.repository.find_membership(
                workspace.org_id, caller_sub
            )
            return bool(membership and membership.status == "active")
        if workspace.admit == "task":
            if not workspace.task_id or not self.task_repository:
                return False
            task = await self.task_repository.find_by_id(workspace.task_id)
            if task is None:
                return False
            if (
                caller_sub == task.assignee_id
                and task.status
                in (TaskStatus.IN_PROGRESS, TaskStatus.SUBMITTED)
            ):
                return True
            participation = await self.task_repository.find_participation_by_user_and_task(
                task_id=workspace.task_id,
                participant_id=caller_sub,
                active_only=False,
            )
            if participation is None:
                return False
            return participation.status in (
                ParticipationStatus.ACTIVE,
                ParticipationStatus.SUBMITTED,
            )
        return False


__all__ = [
    "WorkspaceNotFoundError",
    "WorkspacePermissionError",
    "WorkspaceService",
]
