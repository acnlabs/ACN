"""Task API Routes

Clean Architecture implementation: Route → TaskService → Repository
"""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from ..auth.middleware import get_subject, require_permission
from ..core.entities import TaskMode, TaskStatus
from ..services import TaskNotFoundException, TaskService
from .dependencies import AgentApiKeyDep, InternalTokenDep, OptionalInternalTokenDep  # type: ignore[import-untyped]

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])
logger = structlog.get_logger()


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
        raise RuntimeError("TaskService not initialized")
    return _task_service


TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]


# ========== Request/Response Models ==========


class TaskCreateRequest(BaseModel):
    """Request to create a task"""

    title: str = Field(..., min_length=3, max_length=200)
    description: str = Field(..., min_length=10, max_length=10000)
    mode: str = Field(default="open", description="Task mode: open or assigned")
    task_type: str = Field(default="general", max_length=64, description="Task type category")
    required_skills: list[str] = Field(default_factory=list)
    reward_amount: str = Field(default="0", description="Reward amount (non-negative number as string)")
    reward_currency: str = Field(default="points", description="Currency: USD, USDC, points")
    is_repeatable: bool = Field(default=False, description="DEPRECATED: use is_multi_participant", deprecated=True)

    @field_validator("reward_amount")
    @classmethod
    def reward_amount_must_be_non_negative(cls, v: str) -> str:
        try:
            value = float(v)
        except (ValueError, TypeError):
            raise ValueError("reward_amount must be a valid number (e.g. '100' or '9.99')")
        if value < 0:
            raise ValueError("reward_amount must be >= 0")
        return v
    is_multi_participant: bool = Field(default=False, description="Multiple agents can work in parallel")
    allow_repeat_by_same: bool = Field(default=False, description="Same agent can rejoin after completing")
    max_completions: int | None = Field(None, description="Max completions (open/multi mode)")
    deadline_hours: int | None = Field(None, ge=1, le=720, description="Deadline in hours")
    assignee_id: str | None = Field(None, max_length=128, description="Pre-assigned agent (assigned mode)")
    assignee_name: str | None = Field(None, max_length=128)
    approval_type: str = Field(
        default="manual", description="Approval type: manual, auto, validator"
    )
    validator_id: str | None = Field(None, description="Validator ID for validator approval type")
    metadata: dict = Field(default_factory=dict)


class TaskResponse(BaseModel):
    """Task response model"""

    task_id: str
    mode: str
    status: str
    creator_type: str
    creator_id: str
    creator_name: str
    title: str
    description: str
    task_type: str
    required_skills: list[str]
    assignee_id: str | None = None
    assignee_name: str | None = None
    reward_amount: str
    reward_currency: str
    reward_unit: str = "completion"
    total_budget: str = "0"
    released_amount: str = "0"
    is_repeatable: bool
    is_multi_participant: bool = False
    allow_repeat_by_same: bool = False
    active_participants_count: int = 0
    completed_count: int
    max_completions: int | None = None
    approval_type: str = "manual"
    validator_id: str | None = None
    created_at: str
    deadline: str | None = None
    ui_spec: dict | None = None


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


class ParticipationListResponse(BaseModel):
    """List of participations"""

    participations: list[ParticipationResponse]
    total: int


class TaskListResponse(BaseModel):
    """Response containing list of tasks"""

    tasks: list[TaskResponse]
    total: int
    has_more: bool = False


class TaskAcceptRequest(BaseModel):
    """Request to accept/join a task"""

    message: str = Field(default="", max_length=1000, description="Optional message to creator")


class TaskAcceptResponse(BaseModel):
    """Response for accept/join — includes participation_id for multi-participant tasks"""

    task: TaskResponse
    participation_id: str | None = None


class TaskSubmitRequest(BaseModel):
    """Request to submit task result"""

    submission: str = Field(..., min_length=5, max_length=20000, description="Task result/deliverable")
    artifacts: list[dict] = Field(default_factory=list, description="Optional artifacts")
    participation_id: str | None = Field(None, description="Participation ID (for multi-participant tasks)")


class TaskReviewRequest(BaseModel):
    """Request to approve or reject submission"""

    approved: bool = Field(..., description="Whether to approve")
    notes: str = Field(default="", max_length=2000, description="Review notes")
    participation_id: str | None = Field(None, description="Participation ID (for multi-participant tasks)")
    agent_id: str | None = Field(None, description="Agent ID (alternative to participation_id)")


def _task_to_response(task) -> TaskResponse:
    """Convert Task entity to response model."""
    ui_spec = task.metadata.get("ui_spec") if task.metadata else None

    return TaskResponse(
        task_id=task.task_id,
        mode=task.mode.value,
        status=task.status.value,
        creator_type=task.creator_type,
        creator_id=task.creator_id,
        creator_name=task.creator_name,
        title=task.title,
        description=task.description,
        task_type=task.task_type,
        required_skills=task.required_skills,
        assignee_id=task.assignee_id,
        assignee_name=task.assignee_name,
        reward_amount=task.reward_amount,
        reward_currency=task.reward_currency,
        reward_unit=task.reward_unit,
        total_budget=task.total_budget,
        released_amount=task.released_amount,
        is_repeatable=task.is_repeatable,
        is_multi_participant=task.is_multi_participant,
        allow_repeat_by_same=task.allow_repeat_by_same,
        active_participants_count=task.active_participants_count,
        completed_count=task.completed_count,
        max_completions=task.max_completions,
        approval_type=task.approval_type,
        validator_id=task.validator_id,
        created_at=task.created_at.isoformat(),
        deadline=task.deadline.isoformat() if task.deadline else None,
        ui_spec=ui_spec,
    )


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
    )


# ========== Public Endpoints ==========


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    mode: str | None = Query(None, description="Filter by mode: open, assigned"),
    status: str | None = Query(None, description="Filter by status"),
    skills: str | None = Query(None, description="Filter by skills (comma-separated)"),
    creator_id: str | None = Query(None, description="Filter by creator"),
    assignee_id: str | None = Query(None, description="Filter by assignee"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    task_service: TaskServiceDep = None,
):
    """
    List tasks with optional filters

    Public endpoint - no authentication required.
    """
    # Parse mode
    task_mode = None
    if mode:
        try:
            task_mode = TaskMode(mode)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}")

    # Parse status
    task_status = None
    if status:
        try:
            task_status = TaskStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    # Parse skills
    skill_list = skills.split(",") if skills else None

    tasks = await task_service.list_tasks(
        mode=task_mode,
        status=task_status,
        creator_id=creator_id,
        assignee_id=assignee_id,
        skills=skill_list,
        limit=limit + 1,  # Get one extra to check has_more
        offset=offset,
    )

    has_more = len(tasks) > limit
    if has_more:
        tasks = tasks[:limit]

    return TaskListResponse(
        tasks=[_task_to_response(t) for t in tasks],
        total=len(tasks),
        has_more=has_more,
    )


@router.get("/match")
async def match_tasks_for_agent(
    skills: str = Query(..., description="Agent skills (comma-separated)"),
    limit: int = Query(20, ge=1, le=100),
    task_service: TaskServiceDep = None,
):
    """
    Find tasks matching agent's skills

    Returns open tasks that the agent can work on based on their skills.
    """
    skill_list = [s.strip() for s in skills.split(",") if s.strip()]

    if not skill_list:
        raise HTTPException(status_code=400, detail="At least one skill is required")

    tasks = await task_service.get_tasks_for_agent(skill_list, limit)

    return TaskListResponse(
        tasks=[_task_to_response(t) for t in tasks],
        total=len(tasks),
    )


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    task_service: TaskServiceDep = None,
):
    """Get task details"""
    try:
        task = await task_service.get_task(task_id)
        return _task_to_response(task)
    except TaskNotFoundException:
        raise HTTPException(status_code=404, detail="Task not found")


# ========== Authenticated Endpoints ==========


@router.post("", response_model=TaskResponse)
async def create_task(
    request: TaskCreateRequest,
    http_request: Request,
    payload: dict = Depends(require_permission("acn:write")),
    is_internal: OptionalInternalTokenDep = False,
    task_service: TaskServiceDep = None,
):
    """
    Create a new task

    Requires authentication. The authenticated user becomes the creator.
    Backend services with X-Internal-Token may supply X-Creator-Id to proxy user identity.
    """
    token_owner = await get_subject()

    creator_id_header = http_request.headers.get("x-creator-id")
    creator_name_header = http_request.headers.get("x-creator-name")
    creator_type_header = http_request.headers.get("x-creator-type", "human")

    if is_internal and creator_id_header:
        token_owner = creator_id_header

    # Parse mode
    try:
        mode = TaskMode(request.mode)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid mode: {request.mode}")

    try:
        task = await task_service.create_task(
            creator_type=creator_type_header,
            creator_id=token_owner,
            creator_name=creator_name_header or token_owner,
            title=request.title,
            description=request.description,
            mode=mode,
            task_type=request.task_type,
            required_skills=request.required_skills,
            reward_amount=request.reward_amount,
            reward_currency=request.reward_currency,
            is_repeatable=request.is_repeatable,
            is_multi_participant=request.is_multi_participant,
            allow_repeat_by_same=request.allow_repeat_by_same,
            max_completions=request.max_completions,
            deadline_hours=request.deadline_hours,
            assignee_id=request.assignee_id,
            assignee_name=request.assignee_name,
            approval_type=request.approval_type,
            validator_id=request.validator_id,
            metadata=request.metadata,
        )

        logger.info(
            "task_created",
            task_id=task.task_id,
            creator=token_owner,
            approval_type=request.approval_type,
        )
        return _task_to_response(task)

    except Exception as e:
        logger.error("task_creation_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create task")


@router.post("/{task_id}/accept", response_model=TaskAcceptResponse)
async def accept_task(
    task_id: str,
    http_request: Request,
    request: TaskAcceptRequest = None,
    payload: dict = Depends(require_permission("acn:write")),
    is_internal: OptionalInternalTokenDep = False,
    task_service: TaskServiceDep = None,
):
    """Accept/join a task. Returns participation_id for multi-participant tasks."""
    token_owner = await get_subject()

    creator_id_header = http_request.headers.get("x-creator-id")
    agent_id = (creator_id_header if is_internal and creator_id_header else None) or token_owner
    agent_name = http_request.headers.get("x-creator-name") or agent_id
    agent_type = http_request.headers.get("x-creator-type", "agent")

    try:
        task, participation_id = await task_service.accept_task(
            task_id=task_id,
            agent_id=agent_id,
            agent_name=agent_name,
            agent_type=agent_type,
        )
        return TaskAcceptResponse(
            task=_task_to_response(task),
            participation_id=participation_id,
        )

    except TaskNotFoundException:
        raise HTTPException(status_code=404, detail="Task not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/submit", response_model=TaskResponse)
async def submit_task(
    task_id: str,
    http_request: Request,
    request: TaskSubmitRequest,
    payload: dict = Depends(require_permission("acn:write")),
    is_internal: OptionalInternalTokenDep = False,
    task_service: TaskServiceDep = None,
):
    """Submit task result"""
    token_owner = await get_subject()

    creator_id_header = http_request.headers.get("x-creator-id")
    agent_id = (creator_id_header if is_internal and creator_id_header else None) or token_owner

    try:
        task = await task_service.submit_task(
            task_id=task_id,
            agent_id=agent_id,
            submission=request.submission,
            artifacts=request.artifacts,
            participation_id=request.participation_id,
        )
        return _task_to_response(task)

    except TaskNotFoundException:
        raise HTTPException(status_code=404, detail="Task not found")
    except PermissionError as e:
        logger.warning("submit_task_permission_denied", error=str(e))
        raise HTTPException(status_code=403, detail="Permission denied")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/review", response_model=TaskResponse)
async def review_task(
    task_id: str,
    http_request: Request,
    request: TaskReviewRequest,
    payload: dict = Depends(require_permission("acn:write")),
    is_internal: OptionalInternalTokenDep = False,
    task_service: TaskServiceDep = None,
):
    """Approve or reject task/participation submission"""
    token_owner = await get_subject()
    creator_id_header = http_request.headers.get("x-creator-id")
    reviewer_id = (creator_id_header if is_internal and creator_id_header else None) or token_owner

    try:
        # Multi-participant review (participation_id or agent_id provided)
        if request.participation_id or request.agent_id:
            task = await task_service.review_participation(
                task_id=task_id,
                approver_id=reviewer_id,
                approved=request.approved,
                participation_id=request.participation_id,
                agent_id=request.agent_id,
                notes=request.notes,
            )
        elif request.approved:
            task = await task_service.complete_task(
                task_id=task_id,
                approver_id=reviewer_id,
                notes=request.notes,
            )
        else:
            task = await task_service.reject_task(
                task_id=task_id,
                reviewer_id=reviewer_id,
                notes=request.notes,
            )
        return _task_to_response(task)

    except TaskNotFoundException:
        raise HTTPException(status_code=404, detail="Task not found")
    except PermissionError as e:
        logger.warning("review_task_permission_denied", error=str(e))
        raise HTTPException(status_code=403, detail="Permission denied")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(
    task_id: str,
    payload: dict = Depends(require_permission("acn:write")),
    task_service: TaskServiceDep = None,
):
    """Cancel a task (only creator can cancel)"""
    token_owner = await get_subject()

    try:
        task = await task_service.cancel_task(
            task_id=task_id,
            canceller_id=token_owner,
        )
        return _task_to_response(task)

    except TaskNotFoundException:
        raise HTTPException(status_code=404, detail="Task not found")
    except PermissionError as e:
        logger.warning("cancel_task_permission_denied", error=str(e))
        raise HTTPException(status_code=403, detail="Permission denied")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ========== Participation Endpoints ==========


@router.get("/{task_id}/participations", response_model=ParticipationListResponse)
async def list_participations(
    task_id: str,
    status: str | None = Query(None, description="Filter by status: active, submitted, completed, rejected, cancelled"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    task_service: TaskServiceDep = None,
):
    """List participations for a task (public)"""
    try:
        participations = await task_service.get_task_participations(
            task_id=task_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        return ParticipationListResponse(
            participations=[_participation_to_response(p) for p in participations],
            total=len(participations),
        )
    except TaskNotFoundException:
        raise HTTPException(status_code=404, detail="Task not found")


@router.get("/{task_id}/participations/me", response_model=ParticipationResponse | None)
async def get_my_participation(
    task_id: str,
    http_request: Request,
    payload: dict = Depends(require_permission("acn:read")),
    is_internal: OptionalInternalTokenDep = False,
    task_service: TaskServiceDep = None,
):
    """Get the current user's participation in a task"""
    token_owner = await get_subject()
    creator_id_header = http_request.headers.get("x-creator-id")
    agent_id = (creator_id_header if is_internal and creator_id_header else None) or token_owner

    p = await task_service.get_user_participation(task_id, agent_id)
    if not p:
        return None
    return _participation_to_response(p)


@router.post("/{task_id}/participations/{participation_id}/cancel", response_model=TaskResponse)
async def cancel_participation(
    task_id: str,
    participation_id: str,
    http_request: Request,
    payload: dict = Depends(require_permission("acn:write")),
    is_internal: OptionalInternalTokenDep = False,
    task_service: TaskServiceDep = None,
):
    """Cancel a participation (participant withdraws)"""
    token_owner = await get_subject()
    creator_id_header = http_request.headers.get("x-creator-id")
    agent_id = (creator_id_header if is_internal and creator_id_header else None) or token_owner

    try:
        task = await task_service.cancel_participation(
            task_id=task_id,
            participation_id=participation_id,
            canceller_id=agent_id,
        )
        return _task_to_response(task)
    except TaskNotFoundException:
        raise HTTPException(status_code=404, detail="Task not found")
    except PermissionError as e:
        logger.warning("cancel_participation_permission_denied", error=str(e))
        raise HTTPException(status_code=403, detail="Permission denied")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ========== Internal Endpoints ==========
# For platform backend to access full task data including metadata


@router.get("/{task_id}/internal")
async def get_task_internal(
    task_id: str,
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
        raise HTTPException(status_code=404, detail="Task not found")


# ========== Agent API Key Endpoints ==========
# For autonomous agents using API key authentication


@router.post("/agent/create", response_model=TaskResponse)
async def agent_create_task(
    request: TaskCreateRequest,
    agent_info: AgentApiKeyDep,
    task_service: TaskServiceDep = None,
):
    """
    Create a task (Agent API Key auth)

    For autonomous agents to create tasks using their API key.
    """

    try:
        mode = TaskMode(request.mode)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid mode: {request.mode}")

    task = await task_service.create_task(
        creator_type="agent",
        creator_id=agent_info["agent_id"],
        creator_name=agent_info.get("name", "Agent"),
        title=request.title,
        description=request.description,
        mode=mode,
        task_type=request.task_type,
        required_skills=request.required_skills,
        reward_amount=request.reward_amount,
        reward_currency=request.reward_currency,
        is_repeatable=request.is_repeatable,
        deadline_hours=request.deadline_hours,
        metadata=request.metadata,
    )

    return _task_to_response(task)


@router.post("/agent/{task_id}/accept", response_model=TaskResponse)
async def agent_accept_task(
    task_id: str,
    agent_info: AgentApiKeyDep,
    task_service: TaskServiceDep = None,
):
    """Accept a task (Agent API Key auth)"""

    try:
        task = await task_service.accept_task(
            task_id=task_id,
            agent_id=agent_info["agent_id"],
            agent_name=agent_info.get("name", "Agent"),
        )
        return _task_to_response(task)

    except TaskNotFoundException:
        raise HTTPException(status_code=404, detail="Task not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/agent/{task_id}/submit", response_model=TaskResponse)
async def agent_submit_task(
    task_id: str,
    request: TaskSubmitRequest,
    agent_info: AgentApiKeyDep,
    task_service: TaskServiceDep = None,
):
    """Submit task result (Agent API Key auth)"""

    try:
        task = await task_service.submit_task(
            task_id=task_id,
            agent_id=agent_info["agent_id"],
            submission=request.submission,
            artifacts=request.artifacts,
        )
        return _task_to_response(task)

    except TaskNotFoundException:
        raise HTTPException(status_code=404, detail="Task not found")
    except PermissionError as e:
        logger.warning("agent_submit_task_permission_denied", error=str(e))
        raise HTTPException(status_code=403, detail="Permission denied")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/retry-payment")
async def retry_task_payment(
    task_id: str,
    _token: InternalTokenDep,
    task_service: TaskServiceDep = None,
):
    """Retry releasing payment for a completed task where payment was not released.

    Internal endpoint — requires internal service token.
    Safe to call multiple times (idempotent via payment_released flag).
    """
    try:
        task = await task_service.get_task(task_id)
    except TaskNotFoundException:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status.value != "completed":
        raise HTTPException(status_code=400, detail="Task is not completed")

    if task.payment_released:
        return {"status": "already_released", "task_id": task_id}

    # Delegate to TaskService reward distribution
    try:
        result = await task_service._distribute_reward(
            task=task,
            amount=float(task.reward_amount),
            description=f"Retry payment for task: {task.title}",
        )
        if result.get("success"):
            task.payment_released = True
            await task_service.repository.save(task)
            logger.info("retry_payment_success", task_id=task_id)
            return {"status": "released", "task_id": task_id}
        else:
            err_msg = result.get("error", "unknown")
            logger.error("retry_payment_failed", task_id=task_id, error=err_msg)
            raise HTTPException(status_code=502, detail=f"Payment failed: {err_msg}") from None
    except HTTPException:
        raise
    except Exception as e:
        logger.error("retry_payment_error", task_id=task_id, error=str(e))
        raise HTTPException(status_code=500, detail="Internal error during payment retry") from e
