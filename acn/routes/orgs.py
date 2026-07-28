"""Org Harness HTTP API — ``/api/v1/orgs*`` (ADR-0014 Phase 1)."""

from __future__ import annotations

import secrets
from typing import Annotated, Any, Literal

import structlog  # type: ignore[import-untyped]
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Path,
    Request,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from ..auth.middleware import verify_token
from ..config import get_settings
from ..core.errors import ACNHTTPError, ErrorCode
from ..routes.dependencies import (
    _schedule_alive_renewal,
    get_agent_service,
    get_org_service,
    limiter,
)
from ..routes.tasks import TaskServiceDep, _task_to_response
from ..services.agent_service import AgentService
from ..services.org_service import (
    OrgConflictError,
    OrgMembershipNotFoundError,
    OrgNotFoundError,
    OrgPermissionError,
    OrgService,
    OrgTaskImportError,
    OrgWorkNotFoundError,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/api/v1/orgs", tags=["orgs"])
_bearer_scheme = HTTPBearer(auto_error=False)

OrgIdPath = Annotated[
    str,
    Path(max_length=128, description="Organisation identifier"),
]


# ---------------------------------------------------------------------------
# Auth: agent API key OR human JWT (same shape as task write auth)
# ---------------------------------------------------------------------------


def require_org_auth():
    async def checker(
        request: Request,
        background_tasks: BackgroundTasks,
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
        x_internal_token: str | None = Header(default=None),
        agent_service: AgentService = Depends(get_agent_service),
    ) -> dict:
        s = get_settings()
        if s.dev_mode:
            if credentials and credentials.credentials.startswith("acn_"):
                agent = await agent_service.get_agent_by_api_key(
                    credentials.credentials
                )
                if agent:
                    request.state.rate_limit_key = f"agent:{agent.agent_id}"
                    _schedule_alive_renewal(
                        background_tasks, agent_service, agent.agent_id
                    )
                    return {
                        "sub": agent.agent_id,
                        "type": "agent",
                        "permissions": ["acn:read", "acn:write"],
                    }
            sub = credentials.credentials if credentials else "dev@clients"
            request.state.rate_limit_key = f"dev:{sub}"
            return {
                "sub": sub,
                "type": "human",
                "permissions": ["acn:read", "acn:write", "acn:admin"],
            }

        if (
            x_internal_token
            and s.internal_api_token
            and secrets.compare_digest(x_internal_token, s.internal_api_token)
        ):
            request.state.rate_limit_key = "internal:backend"
            return {
                "sub": "backend@internal",
                "type": "internal",
                "permissions": ["acn:read", "acn:write", "acn:admin"],
            }

        if credentials and credentials.credentials.startswith("acn_"):
            agent = await agent_service.get_agent_by_api_key(credentials.credentials)
            if not agent:
                raise ACNHTTPError(
                    ErrorCode.AUTHENTICATION_REQUIRED,
                    401,
                    details={"reason": "invalid_agent_api_key"},
                )
            request.state.rate_limit_key = f"agent:{agent.agent_id}"
            _schedule_alive_renewal(background_tasks, agent_service, agent.agent_id)
            return {
                "sub": agent.agent_id,
                "type": "agent",
                "permissions": ["acn:read", "acn:write"],
            }

        payload = await verify_token(request, credentials)
        perms: list[str] = payload.get("permissions", [])
        # Align with task write auth: Org mutations require acn:write.
        if "acn:write" not in perms:
            raise ACNHTTPError(
                ErrorCode.MISSING_PERMISSION,
                403,
                details={"required": "acn:write"},
            )
        request.state.rate_limit_key = f"user:{payload.get('sub', '')}"
        return {
            "sub": payload.get("sub", ""),
            "type": "human",
            "permissions": perms,
        }

    return checker


OrgAuthDep = Depends(require_org_auth())


# ---------------------------------------------------------------------------
# Optional read-path auth: never rejects — anonymous / invalid credentials
# resolve to ``None`` so private-Org reads degrade to a redacted view
# (mirrors GET /subnets/{slug}: invalid token → treated as anonymous).
# ---------------------------------------------------------------------------


def resolve_org_reader():
    async def checker(
        request: Request,
        background_tasks: BackgroundTasks,
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
        x_internal_token: str | None = Header(default=None),
        agent_service: AgentService = Depends(get_agent_service),
    ) -> dict | None:
        s = get_settings()
        if s.dev_mode:
            if credentials and credentials.credentials.startswith("acn_"):
                agent = await agent_service.get_agent_by_api_key(
                    credentials.credentials
                )
                if agent:
                    _schedule_alive_renewal(
                        background_tasks, agent_service, agent.agent_id
                    )
                    return {
                        "sub": agent.agent_id,
                        "type": "agent",
                        "permissions": ["acn:read", "acn:write"],
                    }
            sub = credentials.credentials if credentials else "dev@clients"
            return {
                "sub": sub,
                "type": "human",
                "permissions": ["acn:read", "acn:write", "acn:admin"],
            }

        if (
            x_internal_token
            and s.internal_api_token
            and secrets.compare_digest(x_internal_token, s.internal_api_token)
        ):
            return {
                "sub": "backend@internal",
                "type": "internal",
                "permissions": ["acn:read", "acn:write", "acn:admin"],
            }

        if not credentials:
            return None

        if credentials.credentials.startswith("acn_"):
            agent = await agent_service.get_agent_by_api_key(credentials.credentials)
            if not agent:
                return None  # invalid key → anonymous (read path never 401s)
            _schedule_alive_renewal(background_tasks, agent_service, agent.agent_id)
            return {
                "sub": agent.agent_id,
                "type": "agent",
                "permissions": ["acn:read", "acn:write"],
            }

        try:
            payload = await verify_token(request, credentials)
        except Exception:  # noqa: BLE001 — invalid JWT → anonymous
            return None
        return {
            "sub": payload.get("sub", ""),
            "type": "human",
            "permissions": payload.get("permissions", []),
        }

    return checker


OrgReaderDep = Depends(resolve_org_reader())


def _caller(payload: dict) -> tuple[Literal["human", "agent"], str]:
    ptype = payload.get("type", "human")
    if ptype == "agent":
        return "agent", payload["sub"]
    # internal / human / dev → treat as human principal for created_by
    return "human", payload.get("sub", "")


def _reader_ctx(
    payload: dict | None,
) -> tuple[Literal["human", "agent"] | None, str | None, bool]:
    """(caller_type, caller_sub, admin) for optional read-path payloads."""
    if not payload:
        return None, None, False
    admin = (
        payload.get("type") == "internal"
        or "acn:admin" in payload.get("permissions", [])
    )
    caller_type, caller_sub = _caller(payload)
    return caller_type, caller_sub, admin


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class OrgCreateRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=200)
    steward_agent_id: str | None = Field(
        default=None,
        description="Required when caller is a human JWT; ignored for agent callers",
    )
    charter: dict[str, Any] | None = None
    subnet_id: str | None = Field(default=None, max_length=100)
    join_policy: Literal["open", "approval"] = "open"
    is_private: bool = False
    harness_url: str | None = Field(
        default=None,
        description="Optional Org Harness webhook URL registered on the fence subnet",
    )
    harness_secret: str | None = Field(
        default=None,
        description="HMAC secret for harness_url (optional)",
    )
    plugins: dict[str, str] | None = Field(
        default=None,
        description=(
            "Org plugin map; work defaults to builtin_work; "
            "knowledge: noop|git (K3). "
            "Legacy aliases minimal→builtin_work, thin→heartbeat."
        ),
    )


class OrgUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    charter: dict[str, Any] | None = None
    plugins: dict[str, str] | None = None


class OrgClaimRequest(BaseModel):
    owner_kind: Literal["human", "agent"] | None = None
    owner_subject: str | None = None


class OrgTransferRequest(BaseModel):
    new_owner_kind: Literal["human", "agent"]
    new_owner_subject: str = Field(..., min_length=1)


class OrgMemberAddRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128)
    role: str = Field(default="worker", max_length=64)
    reports_to: str | None = None


class OrgWorkCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    assignee_agent_id: str | None = None


class OrgWorkImportTaskRequest(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=128)
    assignee_agent_id: str | None = None


class OrgPublishTaskRequest(BaseModel):
    """Publish a Task Pool task attributed to an Org (org-wallet-v0 / bridge)."""

    title: str = Field(..., min_length=3, max_length=200)
    description: str = Field(..., min_length=10, max_length=10_000)
    required_tags: list[str] = Field(default_factory=list, max_length=20)
    deadline_hours: int = Field(default=48, ge=1, le=2160)
    reward: str = Field(default="0", max_length=64)
    task_type: str = Field(default="general", max_length=64)
    max_participants: int | None = Field(default=1)
    pay_from_org: bool = Field(
        default=False,
        description=(
            "If true: creator_type=org, force credits, escrow when reward>0 "
            "(treasury governance required). If false: agent-paid attribution only."
        ),
    )
    fence: bool = Field(
        default=False,
        description="Scope task to Org subnet fence",
    )
    subnet_slug: str | None = Field(
        default=None,
        max_length=100,
        description="Override fence subnet (implies fence)",
    )


class OrgWorkUpdateRequest(BaseModel):
    status: Literal["todo", "in_progress", "done", "cancelled"]
    assignee_agent_id: str | None = None


def _org_response(org) -> dict[str, Any]:
    return org.to_dict()


def _map_permission(exc: OrgPermissionError, org_id: str) -> ACNHTTPError:
    """Map OrgPermissionError → 403/400.

    Keep ``error_code`` / ``details.reason`` stable for clients; surface the
    service prose in ``message`` so adapters can tell *membership* failures
    apart from *governance* (created_by / owner) without reading ADR text.
    """
    ownership_reasons = {
        "ownership_mismatch",
        "created_by_only",
        "unclaimed",
        "steward_not_owned",
        "steward_mismatch",
        "owner_subject_mismatch",
        "owner_agent_not_owned",
        "cannot_remove_steward",
        "private_org",
    }
    code = (
        ErrorCode.OWNERSHIP_MISMATCH
        if exc.reason in ownership_reasons
        else ErrorCode.INVALID_REQUEST
    )
    prose = str(exc).strip()
    return ACNHTTPError(
        code,
        403 if code == ErrorCode.OWNERSHIP_MISMATCH else 400,
        message=prose if prose and prose != exc.reason else None,
        details={"org_id": org_id, "reason": exc.reason},
    )


def _map_conflict(exc: OrgConflictError) -> ACNHTTPError:
    """Map OrgConflictError → 409; keep ``details={reason}`` shape stable.

    ``message`` carries the service prose (e.g. bound ``org_…`` id) so
    adapters can recover without widening the details schema.
    """
    prose = str(exc).strip()
    return ACNHTTPError(
        ErrorCode.RESOURCE_CONFLICT,
        409,
        message=prose if prose and prose != exc.reason else None,
        details={"reason": exc.reason},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("")
@limiter.limit("10/minute")
async def create_org(
    request: Request,
    body: OrgCreateRequest,
    payload: dict = OrgAuthDep,
    org_service: OrgService = Depends(get_org_service),
):
    caller_type, caller_sub = _caller(payload)
    try:
        org = await org_service.create_org(
            display_name=body.display_name,
            caller_type=caller_type,
            caller_sub=caller_sub,
            steward_agent_id=body.steward_agent_id,
            charter=body.charter,
            subnet_id=body.subnet_id,
            join_policy=body.join_policy,
            is_private=body.is_private,
            plugins=body.plugins,
            harness_url=body.harness_url,
            harness_secret=body.harness_secret,
        )
        # Creator is trivially entitled to the full view of its new Org.
        return await org_service.get_org_view(
            org.org_id, caller_type=caller_type, caller_sub=caller_sub
        )
    except OrgPermissionError as e:
        raise _map_permission(e, org_id="(new)") from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"reason": str(e)},
        ) from e
    except OrgConflictError as e:
        raise _map_conflict(e) from e


@router.get("/{org_id}")
async def get_org(
    org_id: OrgIdPath,
    reader: dict | None = OrgReaderDep,
    org_service: OrgService = Depends(get_org_service),
):
    """Get an Org.

    Orgs fenced by a **private** subnet are redacted for anonymous /
    unentitled callers (same philosophy as ``GET /subnets/{slug}``
    returning a ``SubnetStub``); public-fence Orgs are fully visible.
    """
    caller_type, caller_sub, admin = _reader_ctx(reader)
    try:
        return await org_service.get_org_view(
            org_id,
            caller_type=caller_type,
            caller_sub=caller_sub,
            admin=admin,
        )
    except OrgNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.ORG_NOT_FOUND,
            404,
            details={"org_id": org_id},
        ) from e


@router.patch("/{org_id}")
@limiter.limit("30/minute")
async def update_org(
    request: Request,
    body: OrgUpdateRequest,
    org_id: OrgIdPath,
    payload: dict = OrgAuthDep,
    org_service: OrgService = Depends(get_org_service),
):
    if body.display_name is None and body.charter is None and body.plugins is None:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"reason": "no_fields_to_update"},
        )
    caller_type, caller_sub = _caller(payload)
    try:
        await org_service.update_org(
            org_id,
            caller_type=caller_type,
            caller_sub=caller_sub,
            display_name=body.display_name,
            charter=body.charter,
            plugins=body.plugins,
        )
        # update_org already enforced governance; return the full view.
        return await org_service.get_org_view(
            org_id, caller_type=caller_type, caller_sub=caller_sub
        )
    except OrgNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.ORG_NOT_FOUND, 404, details={"org_id": org_id}
        ) from e
    except OrgPermissionError as e:
        raise _map_permission(e, org_id) from e
    except OrgConflictError as e:
        raise _map_conflict(e) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST, 400, details={"reason": str(e)}
        ) from e


@router.post("/{org_id}/claim")
@limiter.limit("10/minute")
async def claim_org(
    request: Request,
    body: OrgClaimRequest,
    org_id: OrgIdPath,
    payload: dict = OrgAuthDep,
    org_service: OrgService = Depends(get_org_service),
):
    caller_type, caller_sub = _caller(payload)
    try:
        org = await org_service.claim(
            org_id,
            caller_type=caller_type,
            caller_sub=caller_sub,
            owner_kind=body.owner_kind,
            owner_subject=body.owner_subject,
        )
        return _org_response(org)
    except OrgNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.ORG_NOT_FOUND, 404, details={"org_id": org_id}
        ) from e
    except OrgPermissionError as e:
        raise _map_permission(e, org_id) from e
    except OrgConflictError as e:
        raise _map_conflict(e) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST, 400, details={"reason": str(e)}
        ) from e


@router.post("/{org_id}/transfer")
@limiter.limit("10/minute")
async def transfer_org(
    request: Request,
    body: OrgTransferRequest,
    org_id: OrgIdPath,
    payload: dict = OrgAuthDep,
    org_service: OrgService = Depends(get_org_service),
):
    caller_type, caller_sub = _caller(payload)
    try:
        org = await org_service.transfer(
            org_id,
            caller_type=caller_type,
            caller_sub=caller_sub,
            new_owner_kind=body.new_owner_kind,
            new_owner_subject=body.new_owner_subject,
        )
        return _org_response(org)
    except OrgNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.ORG_NOT_FOUND, 404, details={"org_id": org_id}
        ) from e
    except OrgPermissionError as e:
        raise _map_permission(e, org_id) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST, 400, details={"reason": str(e)}
        ) from e


@router.post("/{org_id}/release")
@limiter.limit("10/minute")
async def release_org(
    request: Request,
    org_id: OrgIdPath,
    payload: dict = OrgAuthDep,
    org_service: OrgService = Depends(get_org_service),
):
    caller_type, caller_sub = _caller(payload)
    try:
        org = await org_service.release(
            org_id, caller_type=caller_type, caller_sub=caller_sub
        )
        return _org_response(org)
    except OrgNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.ORG_NOT_FOUND, 404, details={"org_id": org_id}
        ) from e
    except OrgPermissionError as e:
        raise _map_permission(e, org_id) from e
    except OrgConflictError as e:
        raise _map_conflict(e) from e


@router.post("/{org_id}/dissolve")
@limiter.limit("5/minute")
async def dissolve_org(
    request: Request,
    org_id: OrgIdPath,
    payload: dict = OrgAuthDep,
    org_service: OrgService = Depends(get_org_service),
):
    caller_type, caller_sub = _caller(payload)
    try:
        org = await org_service.dissolve(
            org_id, caller_type=caller_type, caller_sub=caller_sub
        )
        return _org_response(org)
    except OrgNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.ORG_NOT_FOUND, 404, details={"org_id": org_id}
        ) from e
    except OrgPermissionError as e:
        raise _map_permission(e, org_id) from e


@router.get("/{org_id}/members")
async def list_members(
    org_id: OrgIdPath,
    reader: dict | None = OrgReaderDep,
    org_service: OrgService = Depends(get_org_service),
):
    """List members. Private-fence Orgs require an entitled reader (403)."""
    caller_type, caller_sub, admin = _reader_ctx(reader)
    try:
        await org_service.ensure_private_readable(
            org_id,
            caller_type=caller_type,
            caller_sub=caller_sub,
            admin=admin,
        )
        return await org_service.list_members_view(org_id)
    except OrgNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.ORG_NOT_FOUND, 404, details={"org_id": org_id}
        ) from e
    except OrgPermissionError as e:
        raise _map_permission(e, org_id) from e


@router.post("/{org_id}/members")
@limiter.limit("30/minute")
async def add_member(
    request: Request,
    body: OrgMemberAddRequest,
    org_id: OrgIdPath,
    payload: dict = OrgAuthDep,
    org_service: OrgService = Depends(get_org_service),
):
    caller_type, caller_sub = _caller(payload)
    try:
        m = await org_service.add_member(
            org_id,
            body.agent_id,
            caller_type=caller_type,
            caller_sub=caller_sub,
            role=body.role,
            reports_to=body.reports_to,
        )
        return m.to_dict()
    except OrgNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.ORG_NOT_FOUND, 404, details={"org_id": org_id}
        ) from e
    except OrgPermissionError as e:
        raise _map_permission(e, org_id) from e
    except OrgConflictError as e:
        if e.reason == "membership_sync_failed":
            # ADR-0014 D4: compensate failed → 503 (retryable operator alert).
            # ACNHTTPError forbids 5xx; use HTTPException like registry/deps.
            raise HTTPException(
                status_code=503,
                detail=(
                    "Org membership write failed and subnet leave compensation "
                    "failed; retry later"
                ),
                headers={"Retry-After": "5"},
            ) from e
        raise _map_conflict(e) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST, 400, details={"reason": str(e)}
        ) from e


@router.delete("/{org_id}/members/{agent_id}")
@limiter.limit("30/minute")
async def remove_member(
    request: Request,
    org_id: OrgIdPath,
    agent_id: str = Path(max_length=128),
    payload: dict = OrgAuthDep,
    org_service: OrgService = Depends(get_org_service),
):
    caller_type, caller_sub = _caller(payload)
    try:
        m = await org_service.remove_member(
            org_id,
            agent_id,
            caller_type=caller_type,
            caller_sub=caller_sub,
        )
        return m.to_dict()
    except OrgNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.ORG_NOT_FOUND, 404, details={"org_id": org_id}
        ) from e
    except OrgMembershipNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"org_id": org_id, "agent_id": agent_id},
        ) from e
    except OrgPermissionError as e:
        raise _map_permission(e, org_id) from e


@router.post("/{org_id}/work")
@limiter.limit("60/minute")
async def create_work(
    request: Request,
    body: OrgWorkCreateRequest,
    org_id: OrgIdPath,
    payload: dict = OrgAuthDep,
    org_service: OrgService = Depends(get_org_service),
):
    caller_type, caller_sub = _caller(payload)
    try:
        work = await org_service.create_work(
            org_id,
            title=body.title,
            caller_type=caller_type,
            caller_sub=caller_sub,
            assignee_agent_id=body.assignee_agent_id,
        )
        return work.to_dict()
    except OrgNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.ORG_NOT_FOUND, 404, details={"org_id": org_id}
        ) from e
    except OrgPermissionError as e:
        raise _map_permission(e, org_id) from e
    except OrgConflictError as e:
        # Legacy Phase 1 Orgs may store unavailable work plugins.
        raise _map_conflict(e) from e


@router.post("/{org_id}/publish-task")
@limiter.limit("20/minute")
async def publish_org_task(
    request: Request,
    body: OrgPublishTaskRequest,
    org_id: OrgIdPath,
    payload: dict = OrgAuthDep,
    org_service: OrgService = Depends(get_org_service),
    task_service: TaskServiceDep = None,
):
    """Publish a Task Pool task for an Org (not Org work; not P2b).

    * ``pay_from_org=false`` (default): attribution only — caller is creator
      (legacy bridge; no treasury check).
    * ``pay_from_org=true``: ``creator_type=org``, credits + escrow when
      reward>0; requires treasury governance (org-wallet-v0 B/C).
    """
    caller_type, caller_sub = _caller(payload)
    try:
        org = await org_service.get_org(org_id)
    except OrgNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.ORG_NOT_FOUND, 404, details={"org_id": org_id}
        ) from e

    if body.pay_from_org:
        try:
            org_service.assert_treasury_principal(org, caller_type, caller_sub)
        except OrgPermissionError as e:
            raise _map_permission(e, org_id) from e

    fence_slug = body.subnet_slug or org.subnet_id or None
    use_fence = bool(body.fence or body.subnet_slug)
    if use_fence and not fence_slug:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            message="Org has no fencing.subnet_id; cannot fence publish",
            details={"org_id": org_id, "reason": "missing_fence"},
        )

    if use_fence and fence_slug and caller_type == "agent":
        if not await org_service._agent_in_subnet(fence_slug, caller_sub):
            raise ACNHTTPError(
                ErrorCode.NOT_SUBNET_MEMBER,
                403,
                message="Caller must be a member of the Org subnet to fence-publish",
                details={"slug": fence_slug, "reason": "creator_not_subnet_member"},
            )

    try:
        reward_f = float(body.reward or "0")
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            message="reward must be a numeric string",
            details={"reason": "invalid_reward"},
        ) from e

    metadata: dict[str, Any] = {
        "org_id": org_id,
        "org_publish": True,
    }
    if body.pay_from_org:
        metadata["org_pay"] = True

    if body.pay_from_org:
        creator_type = "org"
        creator_id = org_id
        creator_name = org.display_name
        reward_currency = "credits"
        use_escrow = reward_f > 0
    else:
        # Legacy attribution: caller pays (if anything); currency matches old CLI default.
        creator_type = caller_type
        creator_id = caller_sub
        creator_name = caller_sub
        reward_currency = "ap_points"
        use_escrow = False

    try:
        task = await task_service.create_task(
            creator_type=creator_type,
            creator_id=creator_id,
            creator_name=creator_name,
            title=body.title,
            description=body.description,
            task_type=body.task_type,
            required_tags=body.required_tags,
            reward=body.reward,
            reward_currency=reward_currency,
            max_participants=body.max_participants,
            use_escrow=use_escrow,
            deadline_hours=body.deadline_hours,
            metadata=metadata,
            subnet_slug=fence_slug if use_fence else None,
        )
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            message=str(e),
            details={"org_id": org_id, "reason": "task_create_failed"},
        ) from e

    logger.info(
        "org_task_published",
        org_id=org_id,
        task_id=task.task_id,
        pay_from_org=body.pay_from_org,
        creator_type=creator_type,
    )
    return _task_to_response(task, expose_submission=True)


@router.get("/{org_id}/wallet")
@limiter.limit("60/minute")
async def get_org_wallet(
    request: Request,
    org_id: OrgIdPath,
    payload: dict = OrgAuthDep,
    org_service: OrgService = Depends(get_org_service),
):
    """Read Org wallet summary via Backend (org-wallet-v0 S6).

    Treasury / governance only (same principal as Org-paid publish).
    Lazy wallets that do not exist yet return ``exists=false`` and
    ``balance=0`` (not 404). Requires ``BACKEND_URL`` + ``INTERNAL_API_TOKEN``.
    """
    caller_type, caller_sub = _caller(payload)
    try:
        org = await org_service.get_org(org_id)
        org_service.assert_treasury_principal(org, caller_type, caller_sub)
    except OrgNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.ORG_NOT_FOUND, 404, details={"org_id": org_id}
        ) from e
    except OrgPermissionError as e:
        raise _map_permission(e, org_id) from e

    settings = get_settings()
    if not settings.backend_url:
        raise ACNHTTPError(
            ErrorCode.INTERNAL_SERVER_ERROR,
            503,
            message="BACKEND_URL is not configured",
            details={"org_id": org_id, "reason": "backend_unconfigured"},
        )

    from ..services.wallet_client import WalletClient

    client = WalletClient(
        backend_url=settings.backend_url,
        internal_token=settings.internal_api_token,
    )
    summary = await client.get_org_wallet(org_id)
    return summary.model_dump()


@router.post("/{org_id}/work/import-task")
@limiter.limit("60/minute")
async def import_work_from_task(
    request: Request,
    body: OrgWorkImportTaskRequest,
    org_id: OrgIdPath,
    payload: dict = OrgAuthDep,
    org_service: OrgService = Depends(get_org_service),
):
    """Import a Task Pool task as Org work (governance only).

    Links via ``task.metadata.org_work_id`` / ``org_id`` / ``org_import``.
    Idempotent when the same Org re-imports the same task.
    """
    caller_type, caller_sub = _caller(payload)
    try:
        work, already = await org_service.import_work_from_task(
            org_id,
            task_id=body.task_id,
            caller_type=caller_type,
            caller_sub=caller_sub,
            assignee_agent_id=body.assignee_agent_id,
        )
        return {
            **work.to_dict(),
            "source_task_id": body.task_id,
            "already_imported": already,
        }
    except OrgNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.ORG_NOT_FOUND, 404, details={"org_id": org_id}
        ) from e
    except OrgPermissionError as e:
        raise _map_permission(e, org_id) from e
    except OrgConflictError as e:
        raise _map_conflict(e) from e
    except OrgTaskImportError as e:
        if e.reason == "task_not_found":
            # details shape must stay {task_id} (see test_error_code_details_consistency).
            raise ACNHTTPError(
                ErrorCode.TASK_NOT_FOUND,
                404,
                message=str(e),
                details={"task_id": body.task_id},
            ) from e
        if e.reason == "not_subnet_member":
            raise ACNHTTPError(
                ErrorCode.NOT_SUBNET_MEMBER,
                403,
                message=str(e),
                details={
                    "task_id": body.task_id,
                    "reason": e.reason,
                },
            ) from e
        status = 503 if e.reason == "task_repository_unavailable" else 400
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST if status == 400 else ErrorCode.INTERNAL_SERVER_ERROR,
            status,
            message=str(e),
            details={"reason": e.reason, "task_id": body.task_id},
        ) from e


@router.get("/{org_id}/work")
async def list_work(
    org_id: OrgIdPath,
    open_only: bool = False,
    reader: dict | None = OrgReaderDep,
    org_service: OrgService = Depends(get_org_service),
):
    """List work items. Private-fence Orgs require an entitled reader (403)."""
    caller_type, caller_sub, admin = _reader_ctx(reader)
    try:
        await org_service.ensure_private_readable(
            org_id,
            caller_type=caller_type,
            caller_sub=caller_sub,
            admin=admin,
        )
        items = await org_service.list_work(org_id, open_only=open_only)
        return {
            "org_id": org_id,
            "count": len(items),
            "work": [w.to_dict() for w in items],
        }
    except OrgNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.ORG_NOT_FOUND, 404, details={"org_id": org_id}
        ) from e
    except OrgPermissionError as e:
        raise _map_permission(e, org_id) from e
    except OrgConflictError as e:
        raise _map_conflict(e) from e


@router.patch("/{org_id}/work/{work_id}")
@limiter.limit("60/minute")
async def update_work(
    request: Request,
    body: OrgWorkUpdateRequest,
    org_id: OrgIdPath,
    work_id: str = Path(max_length=128),
    payload: dict = OrgAuthDep,
    org_service: OrgService = Depends(get_org_service),
):
    caller_type, caller_sub = _caller(payload)
    try:
        work = await org_service.update_work_status(
            org_id,
            work_id,
            status=body.status,
            caller_type=caller_type,
            caller_sub=caller_sub,
            assignee_agent_id=body.assignee_agent_id,
        )
        return work.to_dict()
    except OrgNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.ORG_NOT_FOUND, 404, details={"org_id": org_id}
        ) from e
    except OrgWorkNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.ORG_WORK_NOT_FOUND,
            404,
            details={"org_id": org_id, "work_id": work_id},
        ) from e
    except OrgPermissionError as e:
        raise _map_permission(e, org_id) from e
    except OrgConflictError as e:
        raise _map_conflict(e) from e


@router.post("/{org_id}/loop/tick")
@limiter.limit("30/minute")
async def tick_loop(
    request: Request,
    org_id: OrgIdPath,
    payload: dict = OrgAuthDep,
    org_service: OrgService = Depends(get_org_service),
):
    caller_type, caller_sub = _caller(payload)
    try:
        result = await org_service.tick_loop(
            org_id, caller_type=caller_type, caller_sub=caller_sub
        )
        return {"org_id": org_id, **result}
    except OrgNotFoundError as e:
        raise ACNHTTPError(
            ErrorCode.ORG_NOT_FOUND, 404, details={"org_id": org_id}
        ) from e
    except OrgPermissionError as e:
        raise _map_permission(e, org_id) from e
    except OrgConflictError as e:
        raise _map_conflict(e) from e
