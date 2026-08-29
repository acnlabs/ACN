"""Execution Workspace HTTP API — ``/api/v1/workspaces*``."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field, field_validator

from ..core.entities.workspace import normalize_workspace_execution_env
from ..core.errors import ACNHTTPError, ErrorCode
from ..core.validators import check_dict_size_64k
from ..routes.dependencies import get_workspace_service, limiter
from ..routes.orgs import OrgAuthDep, OrgAuthReadDep
from ..services.org_service import OrgNotFoundError, OrgPermissionError
from ..services.workspace_service import (
    WorkspaceConflictError,
    WorkspaceNotFoundError,
    WorkspacePermissionError,
    WorkspaceService,
)

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])

WorkspaceIdPath = Annotated[
    str,
    Path(max_length=128, description="Execution workspace identifier"),
]


class WorkspaceCreateRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=200)
    execution_env: dict[str, Any]
    admit: Literal["org", "task", "allowlist"]
    org_id: str | None = Field(None, max_length=128)
    task_id: str | None = Field(None, max_length=128)
    allowlist: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("execution_env")
    @classmethod
    def _env(cls, v: dict[str, Any]) -> dict[str, Any]:
        return normalize_workspace_execution_env(v)

    @field_validator("allowlist")
    @classmethod
    def _allowlist(cls, v: list[str]) -> list[str]:
        for i, item in enumerate(v):
            if not isinstance(item, str) or not item.strip() or len(item) > 128:
                raise ValueError(f"allowlist[{i}] must be an agent id")
        return v


class AttestationCreateRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128)
    run_id: str = Field(..., min_length=1, max_length=256)
    work_id: str | None = Field(None, max_length=128)
    task_id: str | None = Field(None, max_length=128)
    hop_id: str | None = Field(None, max_length=256)
    artifact: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None

    @field_validator("artifact", "usage")
    @classmethod
    def _obj_size(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is not None:
            check_dict_size_64k("attestation_field", v)
        return v


def _workspace_response(workspace: Any) -> dict[str, Any]:
    return workspace.to_dict() if hasattr(workspace, "to_dict") else workspace


@router.post("")
@limiter.limit("30/minute")
async def create_workspace(
    request: Request,
    body: WorkspaceCreateRequest,
    auth: dict = OrgAuthDep,
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    try:
        workspace = await workspace_service.create_workspace(
            caller_type=auth["type"],
            caller_sub=auth["sub"],
            display_name=body.display_name,
            execution_env=body.execution_env,
            admit=body.admit,
            org_id=body.org_id,
            task_id=body.task_id,
            allowlist=body.allowlist,
        )
    except WorkspacePermissionError as e:
        raise ACNHTTPError(
            ErrorCode.MISSING_PERMISSION,
            403,
            details={"reason": e.reason},
        ) from e
    except WorkspaceConflictError as e:
        raise ACNHTTPError(
            ErrorCode.RESOURCE_CONFLICT,
            409,
            message=str(e),
            details={"reason": e.reason},
        ) from e
    except OrgNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.ORG_NOT_FOUND,
            404,
            details={"org_id": e.org_id},
        ) from e
    except OrgPermissionError as e:
        raise ACNHTTPError(
            ErrorCode.MISSING_PERMISSION,
            403,
            details={"reason": e.reason},
        ) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"reason": str(e)},
        ) from e
    return _workspace_response(workspace)


@router.get("/{workspace_id}")
@limiter.limit("60/minute")
async def get_workspace(
    request: Request,
    workspace_id: WorkspaceIdPath,
    auth: dict = OrgAuthReadDep,
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    try:
        workspace = await workspace_service.get_workspace(
            workspace_id,
            caller_type=auth["type"],
            caller_sub=auth["sub"],
        )
    except WorkspaceNotFoundError:
        raise ACNHTTPError(
            ErrorCode.WORKSPACE_NOT_FOUND,
            404,
            details={"workspace_id": workspace_id},
        ) from None
    return _workspace_response(workspace)


@router.post("/{workspace_id}/attestations")
@limiter.limit("30/minute")
async def create_attestation(
    request: Request,
    workspace_id: WorkspaceIdPath,
    body: AttestationCreateRequest,
    auth: dict = OrgAuthDep,
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    try:
        attestation = await workspace_service.create_attestation(
            workspace_id,
            caller_type=auth["type"],
            caller_sub=auth["sub"],
            agent_id=body.agent_id,
            run_id=body.run_id,
            work_id=body.work_id,
            task_id=body.task_id,
            hop_id=body.hop_id,
            artifact=body.artifact,
            usage=body.usage,
        )
    except WorkspaceNotFoundError:
        raise ACNHTTPError(
            ErrorCode.WORKSPACE_NOT_FOUND,
            404,
            details={"workspace_id": workspace_id},
        ) from None
    except WorkspacePermissionError as e:
        raise ACNHTTPError(
            ErrorCode.MISSING_PERMISSION,
            403,
            details={"reason": e.reason},
        ) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"reason": str(e)},
        ) from e
    return attestation.to_dict()


@router.get("/{workspace_id}/attestations/{attestation_id}")
@limiter.limit("60/minute")
async def get_attestation(
    request: Request,
    workspace_id: WorkspaceIdPath,
    attestation_id: Annotated[str, Path(max_length=128)],
    auth: dict = OrgAuthReadDep,
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    try:
        attestation = await workspace_service.get_attestation(
            workspace_id,
            attestation_id,
            caller_type=auth["type"],
            caller_sub=auth["sub"],
        )
    except WorkspaceNotFoundError:
        raise ACNHTTPError(
            ErrorCode.WORKSPACE_NOT_FOUND,
            404,
            details={"workspace_id": workspace_id},
        ) from None
    return attestation.to_dict()


@router.post("/{workspace_id}/close")
@limiter.limit("30/minute")
async def close_workspace(
    request: Request,
    workspace_id: WorkspaceIdPath,
    auth: dict = OrgAuthDep,
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    try:
        workspace = await workspace_service.close_workspace(
            workspace_id,
            caller_type=auth["type"],
            caller_sub=auth["sub"],
        )
    except WorkspaceNotFoundError:
        raise ACNHTTPError(
            ErrorCode.WORKSPACE_NOT_FOUND,
            404,
            details={"workspace_id": workspace_id},
        ) from None
    except WorkspacePermissionError as e:
        raise ACNHTTPError(
            ErrorCode.MISSING_PERMISSION,
            403,
            details={"reason": e.reason},
        ) from e
    return _workspace_response(workspace)
