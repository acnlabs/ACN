"""Redis implementation of IWorkspaceRepository.

Assumes the shared production client is created with
``decode_responses=True`` (see ``acn/api.py`` lifespan).

D15 uniqueness (one active workspace per task / per org)
--------------------------------------------------------
Postgres enforces the invariant with unique partial indexes; Redis has
no unique constraint, so ``acn:exec_workspace:by_task:{id}`` /
``acn:exec_workspace:by_org:{id}`` are claimed with ``SET NX`` inside
:meth:`save_workspace`.

Rules mirror ``RedisOrgRepository`` fence-binding:

- **Fresh create**: payload is written BEFORE the ``SET NX`` claim; a
  lost claim deletes the just-written payload (loser leaves no row).
  Claim-first would let a concurrent create misread a freshly claimed
  pointer as dangling and double-bind.
- On claim failure the current holder is inspected — a **dangling**
  pointer (payload gone) or a **closed** holder is released and the
  claim retried once; an active holder raises
  :class:`WorkspaceAlreadyActiveError`.
- ``status="closed"`` saves release the pointer (only when it still
  points at this workspace), so a closed slot is immediately rebindable.
- ``admit=allowlist`` does not claim a uniqueness pointer.
"""

from __future__ import annotations

import json

import redis.asyncio as redis  # type: ignore[import-untyped]

from ....core.entities.workspace import Workspace, WorkspaceAttestation
from ....core.exceptions import WorkspaceAlreadyActiveError
from ....core.interfaces.workspace_repository import IWorkspaceRepository


def _ws_key(workspace_id: str) -> str:
    return f"acn:exec_workspace:{workspace_id}"


def _att_key(attestation_id: str) -> str:
    return f"acn:exec_workspace_att:{attestation_id}"


def _task_ptr(task_id: str) -> str:
    return f"acn:exec_workspace:by_task:{task_id}"


def _org_ptr(org_id: str) -> str:
    return f"acn:exec_workspace:by_org:{org_id}"


def _bind_ptr(workspace: Workspace) -> str | None:
    if workspace.admit == "task" and workspace.task_id:
        return _task_ptr(workspace.task_id)
    if workspace.admit == "org" and workspace.org_id:
        return _org_ptr(workspace.org_id)
    return None


def _bind_ids(workspace: Workspace) -> tuple[str, str] | None:
    if workspace.admit == "task" and workspace.task_id:
        return ("task", workspace.task_id)
    if workspace.admit == "org" and workspace.org_id:
        return ("org", workspace.org_id)
    return None


class RedisWorkspaceRepository(IWorkspaceRepository):
    def __init__(self, redis_client: redis.Redis) -> None:
        self.redis = redis_client

    async def _claim_bind(self, workspace: Workspace) -> None:
        ptr = _bind_ptr(workspace)
        ids = _bind_ids(workspace)
        if ptr is None or ids is None:
            return
        bind_kind, bind_id = ids
        for _ in range(2):
            claimed = await self.redis.set(ptr, workspace.workspace_id, nx=True)
            if claimed:
                return
            holder_id = await self.redis.get(ptr)
            if holder_id is None:
                continue
            if holder_id == workspace.workspace_id:
                return
            holder = await self.find_workspace(holder_id)
            if holder is None or holder.status == "closed":
                await self.redis.delete(ptr)
                continue
            raise WorkspaceAlreadyActiveError(bind_kind, bind_id, holder_id)
        holder_id = await self.redis.get(ptr)
        if holder_id and holder_id != workspace.workspace_id:
            raise WorkspaceAlreadyActiveError(bind_kind, bind_id, holder_id)

    async def _release_bind(self, workspace: Workspace) -> None:
        ptr = _bind_ptr(workspace)
        if ptr is None:
            return
        holder = await self.redis.get(ptr)
        if holder == workspace.workspace_id:
            await self.redis.delete(ptr)

    async def save_workspace(self, workspace: Workspace) -> None:
        old = await self.find_workspace(workspace.workspace_id)
        await self.redis.set(
            _ws_key(workspace.workspace_id), json.dumps(workspace.to_dict())
        )
        try:
            if workspace.status == "active":
                await self._claim_bind(workspace)
            else:
                await self._release_bind(workspace)
        except WorkspaceAlreadyActiveError:
            if old is None:
                await self.redis.delete(_ws_key(workspace.workspace_id))
            raise

    async def find_workspace(self, workspace_id: str) -> Workspace | None:
        raw = await self.redis.get(_ws_key(workspace_id))
        if not raw:
            return None
        data = json.loads(raw)
        return Workspace.from_dict(data)

    async def find_active_by_task_id(self, task_id: str) -> Workspace | None:
        holder_id = await self.redis.get(_task_ptr(task_id))
        if not holder_id:
            return None
        ws = await self.find_workspace(holder_id)
        if (
            ws is None
            or ws.status != "active"
            or ws.admit != "task"
            or ws.task_id != task_id
        ):
            return None
        return ws

    async def find_active_by_org_id(self, org_id: str) -> Workspace | None:
        holder_id = await self.redis.get(_org_ptr(org_id))
        if not holder_id:
            return None
        ws = await self.find_workspace(holder_id)
        if (
            ws is None
            or ws.status != "active"
            or ws.admit != "org"
            or ws.org_id != org_id
        ):
            return None
        return ws

    async def save_attestation(self, attestation: WorkspaceAttestation) -> None:
        await self.redis.set(
            _att_key(attestation.attestation_id), json.dumps(attestation.to_dict())
        )

    async def find_attestation(
        self, attestation_id: str
    ) -> WorkspaceAttestation | None:
        raw = await self.redis.get(_att_key(attestation_id))
        if not raw:
            return None
        return WorkspaceAttestation.from_dict(json.loads(raw))
