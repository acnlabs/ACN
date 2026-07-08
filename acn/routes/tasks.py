"""Task API Routes

Clean Architecture implementation: Route → TaskService → Repository
"""

import secrets
from typing import Annotated

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from fastapi import Request as _Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from ..auth.middleware import verify_token
from ..config import get_settings
from ..core.entities import TaskStatus
from ..core.errors import ACN_DEFAULT_RESPONSES, ACNHTTPError, ErrorCode
from ..core.exceptions import AgentNotFoundException
from ..core.validators import check_dict_size_64k
from ..services import TaskNotFoundException, TaskService
from .dependencies import (  # type: ignore[import-untyped]
    AgentApiKeyDep,
    InternalTokenDep,
    ParticipationIdPath,
    TaskIdPath,
    _schedule_alive_renewal,
    get_agent_service,
    limiter,
)

settings = get_settings()

router = APIRouter(
    prefix="/api/v1/tasks",
    tags=["tasks"],
    responses=ACN_DEFAULT_RESPONSES,
)
logger = structlog.get_logger()

_bearer_scheme = HTTPBearer(auto_error=False)


# ========== Task Write Auth Dependency ==========
# Accepts three authentication paths:
#   1. X-Internal-Token header  → trusted Backend service call
#   2. Bearer acn_xxx           → agent API key (direct agent access)
#   3. Bearer <JWT>             → Auth0 JWT with acn:write permission


def require_task_write_auth():
    """Factory for task write endpoints: accepts internal token, agent API key, or JWT.

    Side effect (security audit H7): writes ``request.state.rate_limit_key``
    keyed on the resolved principal so ``@limiter.limit`` buckets per identity
    rather than per IP. Without this an authenticated agent could exhaust the
    shared-IP bucket of every other caller behind the same NAT/proxy, and a
    malicious caller could spoof XFF to dodge their own bucket.
    """
    from ..services.agent_service import AgentService

    async def checker(
        request: Request,
        background_tasks: BackgroundTasks,
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
        x_internal_token: str | None = Header(default=None),
        agent_service: AgentService = Depends(get_agent_service),
    ) -> dict:
        s = settings

        # Dev mode: accept anything — but if the bearer is an actual agent
        # API key, still resolve it to the agent's identity. Otherwise
        # downstream ACLs that key on agent UUID (subnet membership, task
        # ownership, ...) will trivially fail with "Bearer acn_…" being
        # treated as the agent id.
        if s.dev_mode:
            if credentials and credentials.credentials.startswith("acn_"):
                agent = await agent_service.get_agent_by_api_key(credentials.credentials)
                if agent:
                    request.state.rate_limit_key = f"agent:{agent.agent_id}"
                    return {
                        "sub": agent.agent_id,
                        "type": "agent",
                        "agent_name": agent.name,
                        "permissions": ["acn:read", "acn:write", "acn:admin"],
                    }
            sub = credentials.credentials if credentials else "dev@clients"
            request.state.rate_limit_key = f"dev:{sub}"
            return {"sub": sub, "type": "dev", "permissions": ["acn:read", "acn:write", "acn:admin"]}

        # 1. Internal token: trusted backend service call. Use constant-time
        # comparison to avoid timing-side-channel leaks of token contents.
        if (
            x_internal_token
            and s.internal_api_token
            and secrets.compare_digest(x_internal_token, s.internal_api_token)
        ):
            # Backend often forwards on behalf of many users — bucket per
            # X-Creator-Id when present so one runaway user can't blow out
            # the shared backend@internal budget; otherwise fall back to a
            # single shared bucket (which is fine: the backend is trusted
            # and observable, abuse is upstream of ACN).
            creator_id = request.headers.get("x-creator-id")
            request.state.rate_limit_key = (
                f"internal:{creator_id}" if creator_id else "internal:backend"
            )
            return {
                "sub": "backend@internal",
                "type": "internal",
                "permissions": ["acn:read", "acn:write", "acn:admin"],
            }

        # 2. Agent API key (starts with "acn_")
        if credentials and credentials.credentials.startswith("acn_"):
            agent = await agent_service.get_agent_by_api_key(credentials.credentials)
            if not agent:
                raise ACNHTTPError(
                    ErrorCode.AUTHENTICATION_REQUIRED,
                    401,
                    details={"reason": "invalid_agent_api_key"},
                )
            request.state.rate_limit_key = f"agent:{agent.agent_id}"
            # Implicit heartbeat: task-write traffic counts as agent activity,
            # same as direct /agents routes and proxy traffic.
            _schedule_alive_renewal(background_tasks, agent_service, agent.agent_id)
            return {
                "sub": agent.agent_id,
                "type": "agent",
                "agent_name": agent.name,
                "permissions": ["acn:read", "acn:write"],
            }

        # 3. Auth0 JWT with acn:write
        payload = await verify_token(request, credentials)
        perms: list[str] = payload.get("permissions", [])
        if "acn:write" not in perms:
            raise ACNHTTPError(
                ErrorCode.MISSING_PERMISSION,
                403,
                details={"required_permission": "acn:write"},
            )
        request.state.rate_limit_key = f"jwt:{payload.get('sub', 'unknown')}"
        return {**payload, "type": "jwt"}

    return checker


# ========== Dependency ==========

# Will be injected from dependencies.py
_task_service: TaskService | None = None


def set_task_service(service: TaskService) -> None:
    """Set the task service instance"""
    global _task_service
    _task_service = service


def get_task_service() -> TaskService:
    """Get the task service instance"""
    if _task_service is None:
        raise RuntimeError("TaskService not initialized") from None
    return _task_service


TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]


async def _resolve_caller_identity(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    """Best-effort resolve the calling principal from a Bearer token.

    Used by *read* endpoints that grant private-subnet visibility to the
    caller — both ``acn_xxx`` agent API keys and Auth0 JWTs are first-class
    callers, so we accept either form. Returns ``None`` if no credentials
    are supplied or the token doesn't validate as either form (caller is
    treated as anonymous and only sees public data).

    Why a shared helper? Security audit H8: ``list_tasks`` accepted both
    forms (subnet visibility worked for agents using API keys) but
    ``get_task`` only ran ``verify_token``, which fails on ``acn_xxx`` and
    silently degraded to anonymous — so an agent could see a private task
    in the list but get a 403 when fetching its detail. Centralising the
    resolution stops the two paths from drifting again.
    """
    if not credentials:
        return None
    token = credentials.credentials
    if token.startswith("acn_"):
        try:
            agent_svc = get_agent_service()
            agent = await agent_svc.get_agent_by_api_key(token)
            if agent:
                return agent.agent_id
        except Exception:
            return None
        return None
    try:
        payload = await verify_token(request, credentials)
        return payload.get("sub")
    except Exception:
        return None


def _resolve_actor(payload: dict, request: Request) -> tuple[str, str, str]:
    """Return (actor_id, actor_name, actor_type) from auth payload + request headers.

    Priority:
    - agent auth: identity comes from the API key directly
    - internal/dev: X-Creator-* headers override
    - jwt: sub from token
    """
    auth_type = payload.get("type", "jwt")
    token_owner = payload.get("sub", "dev@clients")

    if auth_type == "agent":
        actor_id = token_owner
        actor_name = request.headers.get("x-creator-name") or payload.get("agent_name", actor_id)
        actor_type = "agent"
    elif auth_type in ("internal", "dev") and request.headers.get("x-creator-id"):
        actor_id = request.headers.get("x-creator-id", token_owner)
        actor_name = request.headers.get("x-creator-name") or actor_id
        actor_type = request.headers.get("x-creator-type", "agent")
    else:
        actor_id = token_owner
        actor_name = request.headers.get("x-creator-name") or actor_id
        actor_type = request.headers.get("x-creator-type", "agent")

    return actor_id, actor_name, actor_type


# ========== Request/Response Models ==========


class TaskCreateRequest(BaseModel):
    """
    Request to create a task.

    Three-layer design for agent-first composability:
    - Layer 1 (required): title, description, deadline_hours, reward
    - Layer 2 (common options): max_participants, auto_approve, task_type, required_tags, reward_currency
    - Layer 3 (advanced): require_join_approval, allow_repeat_by_same, max_total_budget
    - Escrow: use_escrow (opt-in, Labs sets True when reward > 0)
    - Extension: metadata
    """

    # ── Layer 1: Required ────────────────────────────────
    title: str = Field(..., min_length=3, max_length=200)
    # description has a generous cap — task briefs can be long-form, but
    # 10 KB is enough for any realistic human-authored description and stops
    # an attacker from anchoring a 1 MB blob in a string field that gets
    # written to PG and rendered in every list response.
    description: str = Field(..., min_length=10, max_length=10_000)
    deadline_hours: int = Field(..., ge=1, le=2160, description="Deadline in hours (1h to 90 days)")
    reward: str = Field(..., max_length=64, description="Reward per completion (numeric string, e.g. '50' or '0')")

    # ── Layer 2: Common options ───────────────────────────
    max_participants: int | None = Field(default=1, description="1=single, N=fixed, None=unlimited")
    completion_mode: str = Field(default="independent", max_length=32, description="independent | competitive | collaborative")
    auto_approve: bool = Field(default=False, description="True: submissions auto-complete without review")
    task_type: str = Field(default="general", max_length=64, description="Task type category")
    required_tags: list[str] = Field(default_factory=list, max_length=20)
    reward_currency: str = Field(default="credits", max_length=32, description="Currency: credits (platform Credits, default), ap_points (legacy), USD, USDC, ETH")

    # ── Layer 3: Advanced options ─────────────────────────
    require_join_approval: bool = Field(default=False, description="True: solvers must apply and be approved to join")
    allow_repeat_by_same: bool = Field(default=False, description="True: same solver can complete again after finishing")
    max_total_budget: str | None = Field(default=None, max_length=64, description="Budget cap for bounty tasks (max_participants=None only)")

    # ── Escrow: opt-in ────────────────────────────────────
    use_escrow: bool = Field(default=False, description="True: lock reward in escrow at creation. Labs sets this when reward > 0.")

    # ── Collaboration ─────────────────────────────────────
    group_id: str | None = Field(default=None, max_length=128, description="Link related subtasks into a collaborative group")

    # ── Visibility ────────────────────────────────────────
    # Length matches ``SubnetCreateRequest.slug`` (64). Allowing a
    # wider value here would just defer the rejection to the subnet
    # lookup — and 65–128 char slugs are guaranteed to miss because
    # the creation path won't accept them.  Round-2 audit: pin them together.
    # Legacy alias ``subnet_id`` accepted on input for backward compat.
    subnet_slug: str | None = Field(
        default=None,
        max_length=64,
        validation_alias=AliasChoices("subnet_slug", "subnet_id"),
        description="Restrict task visibility to ACN Subnet members (NULL=public). Legacy alias 'subnet_id' accepted on input.",
    )

    # ── A2UI: Declarative interactive UI ─────────────────────────────────────
    ui_spec: dict | None = Field(
        default=None,
        description=(
            "A2UI v1: Declarative interactive UI spec rendered in the Operations tab. "
            "Merged into metadata.ui_spec on creation. No external backend required — "
            "the platform handles page:complete / task:submit / task:complete natively."
        ),
    )

    # ── Grader loop cap ───────────────────────────────────
    max_resubmit_attempts: int | None = Field(
        default=None,
        ge=1,
        description="Max times a participant can resubmit after rejection. None=unlimited. Useful for Harness grader loops.",
    )

    # ── Extension ─────────────────────────────────────────
    metadata: dict = Field(default_factory=dict, description="Extensible metadata (escrow_config, webhook, etc.)")

    @field_validator("metadata")
    @classmethod
    def _metadata_size(cls, v: dict) -> dict:
        return check_dict_size_64k("metadata", v)

    @field_validator("ui_spec")
    @classmethod
    def _ui_spec_size(cls, v: dict | None) -> dict | None:
        if v is None:
            return v
        return check_dict_size_64k("ui_spec", v)

    @model_validator(mode="after")
    def validate_budget_rules(self) -> "TaskCreateRequest":
        try:
            float(self.reward)
        except (ValueError, TypeError):
            raise ValueError(
                "reward must be a valid numeric string (e.g. '50' or '0')"
            ) from None
        if self.max_total_budget is not None and self.max_participants is not None:
            raise ValueError("max_total_budget is only valid when max_participants=None (unlimited/bounty mode)")
        if self.max_total_budget is not None:
            try:
                float(self.max_total_budget)
            except (ValueError, TypeError):
                raise ValueError(
                    "max_total_budget must be a valid numeric string"
                ) from None
        if self.max_participants is not None and self.max_participants < 1:
            raise ValueError("max_participants must be >= 1")
        if self.completion_mode not in ("independent", "competitive", "collaborative"):
            raise ValueError("completion_mode must be independent, competitive, or collaborative")
        if self.max_participants == 1 and self.completion_mode != "independent":
            raise ValueError("single-participant tasks must use independent mode")
        if self.max_participants is None and self.completion_mode == "collaborative":
            raise ValueError("collaborative mode requires finite max_participants")
        return self


class TaskResponse(BaseModel):
    """Task response model"""

    task_id: str
    status: str
    creator_type: str
    creator_id: str
    creator_name: str
    title: str
    description: str
    task_type: str
    required_tags: list[str]
    assignee_id: str | None = None
    assignee_name: str | None = None
    assignee_type: str | None = None
    reward: str
    reward_currency: str
    total_budget: str = "0"
    released_amount: str = "0"
    max_participants: int | None = 1
    completion_mode: str = "independent"
    max_total_budget: str | None = None
    require_join_approval: bool = False
    auto_approve: bool = False
    allow_repeat_by_same: bool = False
    use_escrow: bool = False
    invited_agent_ids: list[str] = Field(default_factory=list)
    active_participants_count: int = 0
    completed_count: int
    created_at: str
    deadline: str | None = None
    group_id: str | None = None
    metadata: dict = Field(default_factory=dict)
    ui_spec: dict | None = Field(default=None, description="A2UI: Declarative interactive UI spec (if provided by creator)")
    # Submission fields — included for single-participant tasks so the
    # frontend DeliverablesPanel can display the submitted content.
    submission: str | None = None
    submission_artifacts: list[dict] = Field(default_factory=list)
    subnet_slug: str | None = Field(
        default=None,
        description="Subnet slug restricting task visibility (NULL=public).",
    )
    max_resubmit_attempts: int | None = None

class ParticipationResponse(BaseModel):
    """Participation response model"""

    participation_id: str
    task_id: str
    participant_id: str
    participant_name: str
    participant_type: str = "agent"
    status: str
    joined_at: str
    submission: str | None = None
    submitted_at: str | None = None
    rejection_reason: str | None = None
    rejected_at: str | None = None
    review_notes: str | None = None
    reviewed_by: str | None = None
    completed_at: str | None = None
    cancelled_at: str | None = None
    resubmit_count: int = 0


class ParticipationListResponse(BaseModel):
    """List of participations"""

    participations: list[ParticipationResponse]
    total: int


class TaskListResponse(BaseModel):
    """Response containing list of tasks"""

    tasks: list[TaskResponse]
    total: int
    has_more: bool = False


class TaskHistoryItem(BaseModel):
    """One entry in an agent's task history — optimised for Dreaming / self-reflection."""

    task_id: str
    task_title: str
    task_type: str
    task_description: str
    role: str = Field(description="assignee (single-participant) or participant (multi-participant)")

    # My outcome
    status: str
    submission: str | None = None
    review_notes: str | None = None
    rejection_reason: str | None = None
    resubmit_count: int = 0

    # Reward
    reward: str = "0"
    reward_currency: str = "credits"

    # Context
    participation_id: str | None = None
    subnet_slug: str | None = Field(
        default=None,
        description="Subnet slug (NULL=public).",
    )

    model_config = ConfigDict(populate_by_name=True)

    # Timestamps
    joined_at: str | None = None
    submitted_at: str | None = None
    completed_at: str | None = None


class TaskHistoryResponse(BaseModel):
    """Agent task history response."""

    agent_id: str
    items: list[TaskHistoryItem]
    total: int


class TaskAcceptRequest(BaseModel):
    """Request to accept/join a task"""

    message: str = Field(default="", max_length=2_000, description="Optional message to creator")


class TaskInviteRequest(BaseModel):
    """Request to invite a solver to a task (creator only)"""

    agent_id: str = Field(..., max_length=128, description="ID of the solver to invite")
    agent_name: str = Field(default="", max_length=200, description="Display name of the invited solver")


class TaskAcceptResponse(BaseModel):
    """Response for accept/join — includes participation_id for multi-participant tasks"""

    task: TaskResponse
    participation_id: str | None = None


class TaskSubmitRequest(BaseModel):
    """Request to submit task result"""

    # 50 KB is a comfortable headroom for any text submission; binary/large
    # deliverables should be uploaded out-of-band and referenced via
    # ``artifacts`` URLs, not inlined here.
    submission: str = Field(..., min_length=5, max_length=50_000, description="Task result/deliverable")
    artifacts: list[dict] = Field(default_factory=list, max_length=50, description="Optional artifacts")
    participation_id: str | None = Field(
        None, max_length=128, description="Participation ID (for multi-participant tasks)"
    )

    @field_validator("artifacts")
    @classmethod
    def _validate_artifact_sizes(cls, v: list[dict]) -> list[dict]:
        for i, artifact in enumerate(v):
            check_dict_size_64k(f"artifacts[{i}]", artifact)
        return v


class TaskReviewRequest(BaseModel):
    """Request to approve or reject submission"""

    approved: bool = Field(..., description="Whether to approve")
    notes: str = Field(default="", max_length=5_000, description="Review notes")
    participation_id: str | None = Field(
        None, max_length=128, description="Participation ID (for multi-participant tasks)"
    )
    agent_id: str | None = Field(None, max_length=128, description="Agent ID (alternative to participation_id)")


def _task_to_response(task, *, expose_submission: bool = False) -> TaskResponse:
    """Convert Task entity to response model.

    ``expose_submission`` controls whether the work-product fields
    (``submission``, ``submission_artifacts``) are included in the response.
    They should only be set to True when the caller is the task creator,
    the task assignee, a confirmed participant, ``acn:admin``, or an
    internal backend token. Anonymous and unrelated callers receive ``None``
    to avoid leaking sensitive deliverable content (ACL V6 Scope B).
    """
    return TaskResponse(
        task_id=task.task_id,
        status=task.status.value,
        creator_type=task.creator_type,
        creator_id=task.creator_id,
        creator_name=task.creator_name,
        title=task.title,
        description=task.description,
        task_type=task.task_type,
        required_tags=task.required_tags,
        assignee_id=task.assignee_id,
        assignee_name=task.assignee_name,
        assignee_type=task.assignee_type,
        reward=task.reward,
        reward_currency=task.reward_currency,
        total_budget=task.total_budget,
        released_amount=task.released_amount,
        max_participants=task.max_participants,
        completion_mode=task.completion_mode,
        max_total_budget=task.max_total_budget,
        require_join_approval=task.require_join_approval,
        auto_approve=task.auto_approve,
        allow_repeat_by_same=task.allow_repeat_by_same,
        use_escrow=task.use_escrow,
        invited_agent_ids=task.invited_agent_ids or [],
        active_participants_count=task.active_participants_count,
        completed_count=task.completed_count,
        created_at=task.created_at.isoformat(),
        deadline=task.deadline.isoformat() if task.deadline else None,
        group_id=task.group_id,
        metadata=task.metadata or {},
        ui_spec=(task.metadata or {}).get("ui_spec"),
        submission=task.submission if expose_submission else None,
        submission_artifacts=task.submission_artifacts or [] if expose_submission else [],
        subnet_slug=task.subnet_slug,
        max_resubmit_attempts=task.max_resubmit_attempts,
    )


def _caller_can_see_submission(task, caller_id: str | None, payload: dict | None) -> bool:
    """Return True when the caller is entitled to see work-product fields.

    Entitled callers:
    - task creator (creator_id match)
    - task assignee (assignee_id match)
    - acn:admin permission holders
    - internal / dev token types (payload.type in {"internal", "dev"})

    Note: multi-participant membership is not checked here — callers who
    accepted a task will either have creator_id == caller_id (if they're the
    task agent), or will see submission through the /participations endpoint
    which already gates by creator/participant.  This function is a
    belt-and-braces safeguard against leaking submitted deliverables to
    unrelated parties on task-detail and task-list endpoints.
    """
    if caller_id is None:
        return False
    if payload is not None:
        ptype = payload.get("type", "")
        perms = payload.get("permissions", [])
        if ptype in ("internal", "dev") or "acn:admin" in perms:
            return True
    if task.creator_id and caller_id == task.creator_id:
        return True
    if task.assignee_id and caller_id == task.assignee_id:
        return True
    return False


def _participation_to_response(p) -> ParticipationResponse:
    """Convert Participation entity to response model."""
    return ParticipationResponse(
        participation_id=p.participation_id,
        task_id=p.task_id,
        participant_id=p.participant_id,
        participant_name=p.participant_name,
        participant_type=p.participant_type,
        status=p.status.value,
        joined_at=p.joined_at.isoformat(),
        submission=p.submission,
        submitted_at=p.submitted_at.isoformat() if p.submitted_at else None,
        rejection_reason=p.rejection_reason,
        rejected_at=p.rejected_at.isoformat() if p.rejected_at else None,
        review_notes=p.review_notes,
        reviewed_by=p.reviewed_by,
        completed_at=p.completed_at.isoformat() if p.completed_at else None,
        cancelled_at=p.cancelled_at.isoformat() if p.cancelled_at else None,
        resubmit_count=p.resubmit_count,
    )


# ========== Public Endpoints ==========


@router.get("/agent/{agent_id}/history", response_model=TaskHistoryResponse)
@limiter.limit("30/minute")
async def get_agent_task_history(
    request: _Request,
    agent_id: str,
    limit: int = Query(50, ge=1, le=200),
    task_service: TaskServiceDep = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
):
    """
    Retrieve an agent's task history — submissions, feedback, and outcomes.

    Designed as the data source for agent self-reflection and Dreaming loops:
    returns everything the agent submitted, plus all review feedback, so an
    Org Harness or the agent itself can extract patterns without issuing
    multiple API calls.

    **Auth**:
    - Agent API key: may only query its own history (key must belong to ``agent_id``).
    - JWT / human: must be the registered owner of ``agent_id``.
    - Internal backend token: unrestricted.
    """
    if not credentials:
        raise ACNHTTPError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            401,
            message="Authentication required to view task history",
        )

    token = credentials.credentials

    # Internal backend: unrestricted access
    if (
        settings.internal_api_token
        and token
        and secrets.compare_digest(token, settings.internal_api_token)
    ):
        items_raw = await task_service.get_agent_task_history(agent_id, limit=limit)
        return TaskHistoryResponse(
            agent_id=agent_id,
            items=[TaskHistoryItem(**e) for e in items_raw],
            total=len(items_raw),
        )

    caller_id = await _resolve_caller_identity(request, credentials)
    if not caller_id:
        raise ACNHTTPError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            401,
            message="Invalid or expired credentials",
        )

    if token.startswith("acn_"):
        # Agent API key: must be the agent itself
        if caller_id != agent_id:
            raise ACNHTTPError(
                ErrorCode.MISSING_PERMISSION,
                403,
                message="Agents can only view their own task history",
            )
    else:
        # JWT (human): must be the registered owner of the agent
        agent_svc = get_agent_service()
        try:
            target_agent = await agent_svc.get_agent(agent_id)
        except AgentNotFoundException as exc:
            raise ACNHTTPError(ErrorCode.RESOURCE_NOT_FOUND, 404, message="Agent not found") from exc
        if target_agent.owner != caller_id:
            raise ACNHTTPError(
                ErrorCode.MISSING_PERMISSION,
                403,
                message="You are not the owner of this agent",
            )

    items_raw = await task_service.get_agent_task_history(agent_id, limit=limit)
    items = [TaskHistoryItem(**entry) for entry in items_raw]
    return TaskHistoryResponse(
        agent_id=agent_id,
        items=items,
        total=len(items),
    )


@router.get("", response_model=TaskListResponse)
@limiter.limit("60/minute")
async def list_tasks(
    request: _Request,
    mode: str | None = Query(None, description="Filter by mode: open, assigned"),
    status: str | None = Query(None, description="Filter by status"),
    tags: str | None = Query(None, description="Filter by capability tags (comma-separated)"),
    creator_id: str | None = Query(None, description="Filter by creator"),
    assignee_id: str | None = Query(None, description="Filter by assignee"),
    group_id: str | None = Query(None, description="Filter by collaboration group"),
    board_id: str | None = Query(None, description="Filter by TaskBoard (metadata hint; SoT enforced by backend)"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    task_service: TaskServiceDep = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
):
    """
    List tasks with optional filters.

    Anonymous callers only see public tasks (subnet_id IS NULL).
    Authenticated callers (Agent API key or JWT) also see tasks in their subnets.
    """
    # Best-effort caller resolution — both ``acn_xxx`` and JWT are valid here.
    requesting_agent_id = await _resolve_caller_identity(request, credentials)

    # mode filter passed through as-is (string); repository handles DB column match
    task_mode = mode or None

    # Parse status
    task_status = None
    if status:
        try:
            task_status = TaskStatus(status)
        except ValueError:
            raise ACNHTTPError(
                ErrorCode.INVALID_REQUEST,
                400,
                message=f"Invalid status: {status}",
                details={
                    "field": "status",
                    "value": status,
                    "allowed": [s.value for s in TaskStatus],
                },
            ) from None

    # Parse tags
    tag_list = tags.split(",") if tags else None

    tasks = await task_service.list_tasks(
        mode=task_mode,
        status=task_status,
        creator_id=creator_id,
        assignee_id=assignee_id,
        tags=tag_list,
        group_id=group_id,
        board_id=board_id,
        limit=limit + 1,  # Get one extra to check has_more
        offset=offset,
        requesting_agent_id=requesting_agent_id,
    )

    has_more = len(tasks) > limit
    if has_more:
        tasks = tasks[:limit]

    # Resolve full payload once for acn:admin / internal check (best-effort).
    # We already resolved requesting_agent_id above; now resolve the payload
    # so _caller_can_see_submission can check admin permissions.
    list_payload: dict | None = None
    if credentials:
        try:
            token = credentials.credentials
            if not token.startswith("acn_"):
                list_payload = await verify_token(request, credentials)
            else:
                list_payload = {"type": "agent", "sub": requesting_agent_id, "permissions": []}
        except Exception:  # noqa: BLE001
            pass

    task_responses = []
    for t in tasks:
        expose = _caller_can_see_submission(t, requesting_agent_id, list_payload)
        task_responses.append(_task_to_response(t, expose_submission=expose))

    return TaskListResponse(
        tasks=task_responses,
        total=len(task_responses),
        has_more=has_more,
    )


@router.get("/match")
@limiter.limit("30/minute")
async def match_tasks_for_agent(
    request: _Request,
    tags: str = Query(..., description="Agent capability tags (comma-separated)"),
    limit: int = Query(20, ge=1, le=100),
    task_service: TaskServiceDep = None,
):
    """
    Find tasks matching agent's tags

    Returns open tasks that the agent can work on based on their tags.
    """
    tag_list = [s.strip() for s in tags.split(",") if s.strip()]

    if not tag_list:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            message="At least one tag is required.",
            details={"field": "tags", "reason": "tag_list_empty"},
        ) from None

    tasks = await task_service.get_tasks_for_agent(tag_list, limit)

    return TaskListResponse(
        tasks=[_task_to_response(t) for t in tasks],
        total=len(tasks),
    )


@router.get("/{task_id}", response_model=TaskResponse)
@limiter.limit("120/minute")
async def get_task(
    request: _Request,
    task_id: TaskIdPath,
    task_service: TaskServiceDep = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
):
    """Get task details. Private tasks (subnet_id set) require subnet membership.

    Security audit H8: caller identity must be resolved with the same logic
    as ``list_tasks`` (both ``acn_xxx`` API keys and Auth0 JWTs). Previously
    only ``verify_token`` was run, which fails on agent API keys — the
    consequence was an agent could see a private task in ``GET /tasks`` but
    receive 403 when fetching its detail. Both endpoints now share
    ``_resolve_caller_identity``.

    ACL V6 Scope B — submission redaction: work-product fields
    (``submission``, ``submission_artifacts``) are only exposed to the task
    creator, the assignee, ``acn:admin``, and internal callers.  All other
    callers (anonymous or unrelated agents/users) receive ``null`` so
    sensitive deliverable content is not leaked on the public detail endpoint.
    """
    try:
        task = await task_service.get_task(task_id)
    except TaskNotFoundException:
        raise ACNHTTPError(
            ErrorCode.TASK_NOT_FOUND,
            404,
            details={"task_id": task_id},
        ) from None

    # Resolve caller once — used for both subnet-membership gate and
    # submission-visibility decision.
    caller_id = await _resolve_caller_identity(request, credentials)

    if task.subnet_slug:
        if not caller_id:
            raise ACNHTTPError(
                ErrorCode.NOT_SUBNET_MEMBER,
                403,
                message="Authentication required to view this task.",
                details={
                    "task_id": task_id,
                    "slug": task.subnet_slug,
                    "reason": "anonymous_caller",
                },
            )
        is_member = await task_service.is_subnet_member(task.subnet_slug, caller_id)
        if not is_member:
            raise ACNHTTPError(
                ErrorCode.NOT_SUBNET_MEMBER,
                403,
                details={
                    "task_id": task_id,
                    "slug": task.subnet_slug,
                    "agent_id": caller_id,
                    "reason": "not_member",
                },
            )

    # Resolve full payload for acn:admin / internal check (best-effort).
    caller_payload: dict | None = None
    if credentials:
        try:
            token = credentials.credentials
            if not token.startswith("acn_"):
                caller_payload = await verify_token(request, credentials)
            else:
                caller_payload = {"type": "agent", "sub": caller_id, "permissions": []}
        except Exception:  # noqa: BLE001
            pass

    expose = _caller_can_see_submission(task, caller_id, caller_payload)
    return _task_to_response(task, expose_submission=expose)


# ========== Authenticated Endpoints ==========


@router.post("", response_model=TaskResponse)
@limiter.limit("20/minute")
async def create_task(
    request: Request,
    body: TaskCreateRequest,
    payload: dict = Depends(require_task_write_auth()),
    task_service: TaskServiceDep = None,
):
    """
    Create a new task.

    Accepts three auth methods:
    - X-Internal-Token header: trusted Backend→ACN service call (X-Creator-* headers used)
    - Bearer acn_xxx: agent direct call (agent becomes creator automatically)
    - Bearer <JWT>: Auth0 user with acn:write permission
    """
    auth_type = payload.get("type", "jwt")
    token_owner = payload.get("sub", "dev@clients")

    creator_id_header = request.headers.get("x-creator-id")
    creator_name_header = request.headers.get("x-creator-name")
    creator_type_header = request.headers.get("x-creator-type", "human")

    if auth_type == "agent":
        # Agent direct call: agent itself is the creator
        token_owner = payload["sub"]
        creator_type_header = "agent"
        creator_name_header = creator_name_header or payload.get("agent_name", token_owner)
    elif creator_id_header and (settings.dev_mode or auth_type in ("internal", "dev")):
        # Internal/dev: allow X-Creator-Id override
        token_owner = creator_id_header

    # ACL V6 Scope B — subnet membership gate on creation.
    # If the caller specifies a subnet_id, they must be a member of that subnet.
    # internal / admin callers are exempt (they act on behalf of others).
    if body.subnet_slug and auth_type not in ("internal", "dev"):
        if "acn:admin" not in payload.get("permissions", []):
            is_creator_member = await task_service.is_subnet_member(body.subnet_slug, token_owner)
            if not is_creator_member:
                raise ACNHTTPError(
                    ErrorCode.NOT_SUBNET_MEMBER,
                    403,
                    message="You must be a member of the subnet to create a task within it.",
                    details={
                        "slug": body.subnet_slug,
                        "reason": "creator_not_subnet_member",
                    },
                )

    # Merge ui_spec into metadata so it's stored and returned transparently
    merged_metadata = dict(body.metadata)
    if body.ui_spec is not None:
        merged_metadata["ui_spec"] = body.ui_spec

    try:
        task = await task_service.create_task(
            creator_type=creator_type_header,
            creator_id=token_owner,
            creator_name=creator_name_header or token_owner,
            title=body.title,
            description=body.description,
            task_type=body.task_type,
            required_tags=body.required_tags,
            reward=body.reward,
            reward_currency=body.reward_currency,
            max_participants=body.max_participants,
            completion_mode=body.completion_mode,
            auto_approve=body.auto_approve,
            require_join_approval=body.require_join_approval,
            allow_repeat_by_same=body.allow_repeat_by_same,
            max_total_budget=body.max_total_budget,
            use_escrow=body.use_escrow,
            group_id=body.group_id,
            deadline_hours=body.deadline_hours,
            metadata=merged_metadata,
            subnet_slug=body.subnet_slug,
            max_resubmit_attempts=body.max_resubmit_attempts,
        )

        logger.info(
            "task_created",
            task_id=task.task_id,
            creator=token_owner,
            auto_approve=body.auto_approve,
        )
        # Creator always sees their own submission on the creation response.
        return _task_to_response(task, expose_submission=True)

    except ACNHTTPError:
        # P3 cross-module catch-all defence: ``ACNHTTPError`` is
        # ``Exception``-typed (not ``HTTPException``-typed); without
        # this re-raise, any caller-actionable 4xx raised inside the
        # try body would be silently rewritten as a sanitised 500.
        raise
    except HTTPException:
        # Mirror defence for legacy ``HTTPException`` raises — same
        # swallow risk via the catch-all below.
        raise
    except Exception as e:
        logger.error("task_creation_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Task creation failed") from e


@router.post("/{task_id}/accept", response_model=TaskAcceptResponse)
@limiter.limit("60/minute")
async def accept_task(
    request: Request,
    task_id: TaskIdPath,
    body: TaskAcceptRequest = None,
    payload: dict = Depends(require_task_write_auth()),
    task_service: TaskServiceDep = None,
):
    """Accept/join a task. Returns participation_id for multi-participant tasks.

    ACL V6 Scope B — subnet membership gate: if the task is restricted to a
    subnet (``subnet_id`` is set) the caller must be a member of that subnet.
    Non-members receive ``403 NOT_SUBNET_MEMBER`` with existence-hiding
    semantics (same shape as the already-implemented gate on ``GET /tasks/{id}``).
    """
    agent_id, agent_name, agent_type = _resolve_actor(payload, request)

    try:
        task = await task_service.get_task(task_id)
    except TaskNotFoundException:
        raise ACNHTTPError(
            ErrorCode.TASK_NOT_FOUND,
            404,
            details={"task_id": task_id},
        ) from None

    # Gate: private subnet task — caller must be a subnet member.
    if task.subnet_slug and "acn:admin" not in payload.get("permissions", []):
        is_member = await task_service.is_subnet_member(task.subnet_slug, agent_id)
        if not is_member:
            raise ACNHTTPError(
                ErrorCode.NOT_SUBNET_MEMBER,
                403,
                details={
                    "task_id": task_id,
                    "slug": task.subnet_slug,
                    "reason": "not_subnet_member",
                },
            )

    try:
        task, participation_id = await task_service.accept_task(
            task_id=task_id,
            agent_id=agent_id,
            agent_name=agent_name,
            agent_type=agent_type,
        )
        expose = _caller_can_see_submission(task, agent_id, payload)
        return TaskAcceptResponse(
            task=_task_to_response(task, expose_submission=expose),
            participation_id=participation_id,
        )

    except TaskNotFoundException:
        raise ACNHTTPError(
            ErrorCode.TASK_NOT_FOUND,
            404,
            details={"task_id": task_id},
        ) from None
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"task_id": task_id, "reason": "permission_denied"},
        ) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"task_id": task_id, "reason": "invalid_request"},
        ) from e


@router.post("/{task_id}/invite", response_model=TaskResponse)
@limiter.limit("30/minute")
async def invite_solver(
    request: Request,
    task_id: TaskIdPath,
    body: TaskInviteRequest,
    payload: dict = Depends(require_task_write_auth()),
    task_service: TaskServiceDep = None,
):
    """Invite a specific solver to the task (creator only).

    Invited solvers can join via /accept even when require_join_approval is True.
    """
    inviter_id, _, _ = _resolve_actor(payload, request)

    try:
        task = await task_service.invite_agent(
            task_id=task_id,
            inviter_id=inviter_id,
            invitee_id=body.agent_id,
            invitee_name=body.agent_name,
        )
        expose = _caller_can_see_submission(task, inviter_id, payload)
        return _task_to_response(task, expose_submission=expose)

    except TaskNotFoundException:
        raise ACNHTTPError(
            ErrorCode.TASK_NOT_FOUND,
            404,
            details={"task_id": task_id},
        ) from None
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"task_id": task_id, "reason": "permission_denied"},
        ) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"task_id": task_id, "reason": "invalid_request"},
        ) from e


@router.post("/{task_id}/submit", response_model=TaskResponse)
@limiter.limit("30/minute")
async def submit_task(
    request: Request,
    task_id: TaskIdPath,
    body: TaskSubmitRequest,
    payload: dict = Depends(require_task_write_auth()),
    task_service: TaskServiceDep = None,
):
    """Submit task result"""
    agent_id, _, _ = _resolve_actor(payload, request)

    try:
        task = await task_service.submit_task(
            task_id=task_id,
            agent_id=agent_id,
            submission=body.submission,
            artifacts=body.artifacts,
            participation_id=body.participation_id,
        )
        # Submitter always sees their own submission in the confirmation response.
        expose = _caller_can_see_submission(task, agent_id, payload)
        return _task_to_response(task, expose_submission=expose)

    except TaskNotFoundException:
        raise ACNHTTPError(
            ErrorCode.TASK_NOT_FOUND,
            404,
            details={"task_id": task_id},
        ) from None
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"task_id": task_id, "reason": "permission_denied"},
        ) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"task_id": task_id, "reason": "invalid_request"},
        ) from e


@router.post("/{task_id}/review", response_model=TaskResponse)
@limiter.limit("60/minute")
async def review_task(
    request: Request,
    task_id: TaskIdPath,
    body: TaskReviewRequest,
    payload: dict = Depends(require_task_write_auth()),
    task_service: TaskServiceDep = None,
):
    """Approve or reject task/participation submission"""
    reviewer_id, _, _ = _resolve_actor(payload, request)

    try:
        # Multi-participant review (participation_id or agent_id provided)
        if body.participation_id or body.agent_id:
            task = await task_service.review_participation(
                task_id=task_id,
                approver_id=reviewer_id,
                approved=body.approved,
                participation_id=body.participation_id,
                agent_id=body.agent_id,
                notes=body.notes,
            )
        elif body.approved:
            task = await task_service.complete_task(
                task_id=task_id,
                approver_id=reviewer_id,
                notes=body.notes,
            )
        else:
            task = await task_service.reject_task(
                task_id=task_id,
                reviewer_id=reviewer_id,
                notes=body.notes,
            )
        reviewer_expose = _caller_can_see_submission(task, reviewer_id, payload)
        return _task_to_response(task, expose_submission=reviewer_expose)

    except TaskNotFoundException:
        raise ACNHTTPError(
            ErrorCode.TASK_NOT_FOUND,
            404,
            details={"task_id": task_id},
        ) from None
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"task_id": task_id, "reason": "permission_denied"},
        ) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"task_id": task_id, "reason": "invalid_request"},
        ) from e


@router.post("/{task_id}/cancel", response_model=TaskResponse)
@limiter.limit("30/minute")
async def cancel_task(
    request: Request,
    task_id: TaskIdPath,
    payload: dict = Depends(require_task_write_auth()),
    task_service: TaskServiceDep = None,
):
    """Cancel a task (only creator can cancel)"""
    canceller_id, _, _ = _resolve_actor(payload, request)

    try:
        task = await task_service.cancel_task(
            task_id=task_id,
            canceller_id=canceller_id,
        )
        expose = _caller_can_see_submission(task, canceller_id, payload)
        return _task_to_response(task, expose_submission=expose)

    except TaskNotFoundException:
        raise ACNHTTPError(
            ErrorCode.TASK_NOT_FOUND,
            404,
            details={"task_id": task_id},
        ) from None
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"task_id": task_id, "reason": "permission_denied"},
        ) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"task_id": task_id, "reason": "invalid_request"},
        ) from e


# ========== Participation Endpoints ==========


@router.get("/{task_id}/participations", response_model=ParticipationListResponse)
@limiter.limit("60/minute")
async def list_participations(
    request: _Request,
    task_id: TaskIdPath,
    status: str | None = Query(
        None, description="Filter by status: active, submitted, completed, rejected, cancelled"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    task_service: TaskServiceDep = None,
    payload: dict = Depends(require_task_write_auth()),
):
    """List participations for a task.

    Requires authentication (agent API key, JWT, or internal token).
    Submission content is only visible to the task creator or the participant themselves.
    """
    caller_id: str = payload.get("agent_id") or payload.get("sub") or ""
    try:
        task = await task_service.get_task(task_id)

        # P1-2: subnet membership gate — same as get_task.
        # Internal / admin callers are exempted.
        permissions = payload.get("permissions", [])
        is_internal = payload.get("type") == "internal" or "acn:admin" in permissions
        if task.subnet_slug and not is_internal:
            is_member = await task_service.is_subnet_member(task.subnet_slug, caller_id)
            if not is_member:
                raise ACNHTTPError(
                    ErrorCode.NOT_SUBNET_MEMBER,
                    403,
                    details={
                        "task_id": task_id,
                        "slug": task.subnet_slug,
                        "reason": "not_member",
                    },
                )

        participations = await task_service.get_task_participations(
            task_id=task_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        is_creator = caller_id == task.creator_id

        def _maybe_redact(p: ParticipationResponse) -> ParticipationResponse:
            # P1-1: use participant_id (not agent_id which doesn't exist on this model)
            if is_creator or p.participant_id == caller_id:
                return p
            return p.model_copy(update={"submission": None, "submission_artifacts": []})

        return ParticipationListResponse(
            participations=[_maybe_redact(_participation_to_response(p)) for p in participations],
            total=len(participations),
        )
    except TaskNotFoundException:
        raise ACNHTTPError(
            ErrorCode.TASK_NOT_FOUND,
            404,
            details={"task_id": task_id},
        ) from None


@router.get("/{task_id}/participations/me", response_model=ParticipationResponse | None)
async def get_my_participation(
    task_id: TaskIdPath,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    task_service: TaskServiceDep = None,
):
    """Get the current user's participation in a task.

    Accepts both Agent API Key (acn_xxx) and Auth0 JWT so agents can check
    their own participation status with the same credential used for accept/submit.
    """
    agent_id = await _resolve_caller_identity(request, credentials)
    if not agent_id:
        raise ACNHTTPError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            401,
            details={"reason": "authentication_required"},
        )

    p = await task_service.get_user_participation(task_id, agent_id)
    if not p:
        return None
    return _participation_to_response(p)


@router.post("/{task_id}/participations/{participation_id}/cancel", response_model=TaskResponse)
@limiter.limit("60/minute")
async def cancel_participation(
    request: Request,
    task_id: TaskIdPath,
    participation_id: ParticipationIdPath,
    payload: dict = Depends(require_task_write_auth()),
    task_service: TaskServiceDep = None,
):
    """Cancel a participation (participant withdraws)"""
    agent_id, _, _ = _resolve_actor(payload, request)

    try:
        task = await task_service.cancel_participation(
            task_id=task_id,
            participation_id=participation_id,
            canceller_id=agent_id,
        )
        # P3-7: expose submission only to the task creator.
        return _task_to_response(task, expose_submission=(agent_id == task.creator_id))
    except TaskNotFoundException:
        raise ACNHTTPError(
            ErrorCode.TASK_NOT_FOUND,
            404,
            details={"task_id": task_id},
        ) from None
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"task_id": task_id, "reason": "permission_denied"},
        ) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"task_id": task_id, "reason": "invalid_request"},
        ) from e


@router.post("/{task_id}/participations/{participation_id}/approve", response_model=TaskResponse)
@limiter.limit("60/minute")
async def approve_applicant(
    request: Request,
    task_id: TaskIdPath,
    participation_id: ParticipationIdPath,
    payload: dict = Depends(require_task_write_auth()),
    task_service: TaskServiceDep = None,
):
    """Approve an applicant for an assigned task (creator only). Sets them as assignee."""
    approver_id, _, _ = _resolve_actor(payload, request)

    try:
        task = await task_service.approve_applicant(
            task_id=task_id,
            participation_id=participation_id,
            approver_id=approver_id,
        )
        return _task_to_response(task)
    except TaskNotFoundException:
        raise ACNHTTPError(
            ErrorCode.TASK_NOT_FOUND,
            404,
            details={"task_id": task_id},
        ) from None
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"task_id": task_id, "reason": "permission_denied"},
        ) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"task_id": task_id, "reason": "invalid_request"},
        ) from e


@router.post("/{task_id}/participations/{participation_id}/reject", response_model=TaskResponse)
@limiter.limit("60/minute")
async def reject_applicant(
    request: Request,
    task_id: TaskIdPath,
    participation_id: ParticipationIdPath,
    payload: dict = Depends(require_task_write_auth()),
    task_service: TaskServiceDep = None,
):
    """Reject an applicant for an assigned task (creator only)."""
    approver_id, _, _ = _resolve_actor(payload, request)

    try:
        task = await task_service.reject_applicant(
            task_id=task_id,
            participation_id=participation_id,
            approver_id=approver_id,
        )
        return _task_to_response(task)
    except TaskNotFoundException:
        raise ACNHTTPError(
            ErrorCode.TASK_NOT_FOUND,
            404,
            details={"task_id": task_id},
        ) from None
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"task_id": task_id, "reason": "permission_denied"},
        ) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"task_id": task_id, "reason": "invalid_request"},
        ) from e


# ========== Internal Endpoints ==========
# For platform backend to access full task data including metadata


@router.get("/{task_id}/internal")
async def get_task_internal(
    task_id: TaskIdPath,
    _: InternalTokenDep,
    task_service: TaskServiceDep = None,
):
    """
    Get full task data including metadata (internal use only).

    Used by the platform backend to read action_endpoint and platform_secret
    from task metadata. These fields are NOT included in the public TaskResponse.

    Requires X-Internal-Token header matching the shared INTERNAL_API_TOKEN.
    """
    try:
        task = await task_service.get_task(task_id)
        return task.to_dict()
    except TaskNotFoundException:
        raise ACNHTTPError(
            ErrorCode.TASK_NOT_FOUND,
            404,
            details={"task_id": task_id},
        ) from None


# ========== Agent API Key Endpoints ==========
# For autonomous agents using API key authentication


@router.post("/agent/create", response_model=TaskResponse)
@limiter.limit("20/minute")
async def agent_create_task(
    request: Request,
    body: TaskCreateRequest,
    agent_info: AgentApiKeyDep,
    task_service: TaskServiceDep = None,
):
    """
    Create a task (Agent API Key auth)

    For autonomous agents to create tasks using their API key.
    """

    merged_metadata = dict(body.metadata)
    if body.ui_spec is not None:
        merged_metadata["ui_spec"] = body.ui_spec

    # Subnet membership gate for agent/create (same as POST /tasks).
    if body.subnet_slug:
        is_creator_member = await task_service.is_subnet_member(
            body.subnet_slug, agent_info["agent_id"]
        )
        if not is_creator_member:
            raise ACNHTTPError(
                ErrorCode.NOT_SUBNET_MEMBER,
                403,
                message="Agent must be a member of the subnet to create a task within it.",
                details={
                    "slug": body.subnet_slug,
                    "reason": "creator_not_subnet_member",
                },
            )

    task = await task_service.create_task(
        creator_type="agent",
        creator_id=agent_info["agent_id"],
        creator_name=agent_info.get("name", "Agent"),
        title=body.title,
        description=body.description,
        task_type=body.task_type,
        required_tags=body.required_tags,
        reward=body.reward,
        reward_currency=body.reward_currency,
        max_participants=body.max_participants,
        completion_mode=body.completion_mode,
        auto_approve=body.auto_approve,
        require_join_approval=body.require_join_approval,
        allow_repeat_by_same=body.allow_repeat_by_same,
        max_total_budget=body.max_total_budget,
        use_escrow=body.use_escrow,
        group_id=body.group_id,
        deadline_hours=body.deadline_hours,
        metadata=merged_metadata,
        subnet_slug=body.subnet_slug,  # P1-3: was missing, task was created without subnet context
    )

    return _task_to_response(task, expose_submission=True)


@router.post("/agent/{task_id}/accept", response_model=TaskResponse)
@limiter.limit("60/minute")
async def agent_accept_task(
    request: Request,
    task_id: TaskIdPath,
    agent_info: AgentApiKeyDep,
    task_service: TaskServiceDep = None,
):
    """Accept a task (Agent API Key auth)"""

    # Fetch task first to check subnet membership gate.
    try:
        task_entity = await task_service.get_task(task_id)
    except TaskNotFoundException:
        raise ACNHTTPError(
            ErrorCode.TASK_NOT_FOUND,
            404,
            details={"task_id": task_id},
        ) from None

    if task_entity.subnet_slug:
        is_member = await task_service.is_subnet_member(
            task_entity.subnet_slug, agent_info["agent_id"]
        )
        if not is_member:
            raise ACNHTTPError(
                ErrorCode.NOT_SUBNET_MEMBER,
                403,
                details={
                    "task_id": task_id,
                    "slug": task_entity.subnet_slug,
                    "reason": "not_subnet_member",
                },
            )

    try:
        task, _participation_id = await task_service.accept_task(
            task_id=task_id,
            agent_id=agent_info["agent_id"],
            agent_name=agent_info.get("name", "Agent"),
        )
        return _task_to_response(task, expose_submission=True)

    except TaskNotFoundException:
        raise ACNHTTPError(
            ErrorCode.TASK_NOT_FOUND,
            404,
            details={"task_id": task_id},
        ) from None
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"task_id": task_id, "reason": "permission_denied"},
        ) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"task_id": task_id, "reason": "invalid_request"},
        ) from e


@router.post("/agent/{task_id}/submit", response_model=TaskResponse)
@limiter.limit("30/minute")
async def agent_submit_task(
    request: Request,
    task_id: TaskIdPath,
    body: TaskSubmitRequest,
    agent_info: AgentApiKeyDep,
    task_service: TaskServiceDep = None,
):
    """Submit task result (Agent API Key auth)"""

    try:
        task = await task_service.submit_task(
            task_id=task_id,
            agent_id=agent_info["agent_id"],
            submission=body.submission,
            artifacts=body.artifacts,
            participation_id=body.participation_id,
        )
        # Agent submitter always sees their own submission in the confirmation.
        return _task_to_response(task, expose_submission=True)

    except TaskNotFoundException:
        raise ACNHTTPError(
            ErrorCode.TASK_NOT_FOUND,
            404,
            details={"task_id": task_id},
        ) from None
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"task_id": task_id, "reason": "permission_denied"},
        ) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"task_id": task_id, "reason": "invalid_request"},
        ) from e
