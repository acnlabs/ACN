"""PostgreSQL implementation of IWorkspaceRepository."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ....core.entities.workspace import Workspace, WorkspaceAttestation
from ....core.exceptions import WorkspaceAlreadyActiveError
from ....core.interfaces.workspace_repository import IWorkspaceRepository
from .models import ExecWorkspaceAttestationModel, ExecWorkspaceModel


class PostgresWorkspaceRepository(IWorkspaceRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def _row_to_workspace(self, row: ExecWorkspaceModel) -> Workspace:
        return Workspace(
            workspace_id=row.workspace_id,
            owner_agent_id=row.owner_agent_id,
            display_name=row.display_name,
            execution_env=row.execution_env,
            admit=row.admit,  # type: ignore[arg-type]
            org_id=row.org_id,
            task_id=row.task_id,
            allowlist=list(row.allowlist or []),
            status=row.status,  # type: ignore[arg-type]
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _workspace_values(self, workspace: Workspace) -> dict:
        return {
            "workspace_id": workspace.workspace_id,
            "owner_agent_id": workspace.owner_agent_id,
            "display_name": workspace.display_name,
            "execution_env": workspace.execution_env,
            "admit": workspace.admit,
            "org_id": workspace.org_id,
            "task_id": workspace.task_id,
            "allowlist": list(workspace.allowlist),
            "status": workspace.status,
            "created_at": workspace.created_at,
            "updated_at": workspace.updated_at,
        }

    async def _raise_active_conflict(
        self, session: AsyncSession, workspace: Workspace, exc: IntegrityError
    ) -> None:
        msg = str(exc.orig or exc)
        if "uq_exec_workspaces_active_task" in msg and workspace.task_id:
            holder = await session.execute(
                select(ExecWorkspaceModel.workspace_id)
                .where(
                    ExecWorkspaceModel.task_id == workspace.task_id,
                    ExecWorkspaceModel.status == "active",
                )
                .limit(1)
            )
            existing = holder.scalar_one_or_none() or "(unknown)"
            raise WorkspaceAlreadyActiveError(
                "task", workspace.task_id, existing
            ) from exc
        if "uq_exec_workspaces_active_org" in msg and workspace.org_id:
            holder = await session.execute(
                select(ExecWorkspaceModel.workspace_id)
                .where(
                    ExecWorkspaceModel.org_id == workspace.org_id,
                    ExecWorkspaceModel.admit == "org",
                    ExecWorkspaceModel.status == "active",
                )
                .limit(1)
            )
            existing = holder.scalar_one_or_none() or "(unknown)"
            raise WorkspaceAlreadyActiveError(
                "org", workspace.org_id, existing
            ) from exc
        raise exc

    async def save_workspace(self, workspace: Workspace) -> None:
        values = self._workspace_values(workspace)
        async with self._session_factory() as session:
            existing = await session.get(ExecWorkspaceModel, workspace.workspace_id)
            if existing:
                await session.execute(
                    update(ExecWorkspaceModel)
                    .where(ExecWorkspaceModel.workspace_id == workspace.workspace_id)
                    .values(**{k: v for k, v in values.items() if k != "workspace_id"})
                )
            else:
                session.add(ExecWorkspaceModel(**values))
            try:
                await session.commit()
            except IntegrityError as e:
                await session.rollback()
                if (
                    "uq_exec_workspaces_active_task" not in str(e.orig or e)
                    and "uq_exec_workspaces_active_org" not in str(e.orig or e)
                ):
                    raise
                await self._raise_active_conflict(session, workspace, e)

    async def find_workspace(self, workspace_id: str) -> Workspace | None:
        async with self._session_factory() as session:
            row = await session.get(ExecWorkspaceModel, workspace_id)
            if row is None:
                return None
            return self._row_to_workspace(row)

    async def find_active_by_task_id(self, task_id: str) -> Workspace | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ExecWorkspaceModel)
                    .where(
                        ExecWorkspaceModel.task_id == task_id,
                        ExecWorkspaceModel.status == "active",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            return self._row_to_workspace(row) if row else None

    async def find_active_by_org_id(self, org_id: str) -> Workspace | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ExecWorkspaceModel)
                    .where(
                        ExecWorkspaceModel.org_id == org_id,
                        ExecWorkspaceModel.admit == "org",
                        ExecWorkspaceModel.status == "active",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            return self._row_to_workspace(row) if row else None

    async def save_attestation(self, attestation: WorkspaceAttestation) -> None:
        async with self._session_factory() as session:
            session.add(
                ExecWorkspaceAttestationModel(
                    attestation_id=attestation.attestation_id,
                    workspace_id=attestation.workspace_id,
                    kind=attestation.kind,
                    agent_id=attestation.agent_id,
                    run_id=attestation.run_id,
                    work_id=attestation.work_id,
                    task_id=attestation.task_id,
                    hop_id=attestation.hop_id,
                    artifact=attestation.artifact,
                    usage=attestation.usage,
                    issued_at=attestation.issued_at,
                )
            )
            await session.commit()

    async def find_attestation(
        self, attestation_id: str
    ) -> WorkspaceAttestation | None:
        async with self._session_factory() as session:
            row = await session.get(ExecWorkspaceAttestationModel, attestation_id)
            if row is None:
                return None
            return WorkspaceAttestation(
                attestation_id=row.attestation_id,
                workspace_id=row.workspace_id,
                kind=row.kind,
                agent_id=row.agent_id,
                run_id=row.run_id,
                work_id=row.work_id,
                task_id=row.task_id,
                hop_id=row.hop_id,
                artifact=row.artifact,
                usage=row.usage,
                issued_at=row.issued_at,
            )
