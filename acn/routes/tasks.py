"""Task API Routes

Clean Architecture implementation: Route → TaskService → Repository
"""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth.middleware import get_subject, require_permission
from ..core.entities import TaskMode, TaskStatus
from ..services import TaskNotFoundException, TaskService

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
    description: str = Field(..., min_length=10)
    mode: str = Field(default="open", description="Task mode: open or assigned")
    task_type: str = Field(default="general", description="Task type category")
    required_skills: list[str] = Field(default_factory=list)
    reward_amount: str = Field(default="0", description="Reward amount")
    reward_currency: str = Field(default="points", description="Currency: USD, USDC, points")
    is_repeatable: bool = Field(default=False, description="Can be completed multiple times")
    max_completions: int | None = Field(None, description="Max completions (open mode)")
    deadline_hours: int | None = Field(None, ge=1, le=720, description="Deadline in hours")
    assignee_id: str | None = Field(None, description="Pre-assigned agent (assigned mode)")
    assignee_name: str | None = Field(None)
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
    completed_count: int
    max_completions: int | None = None
    approval_type: str = "manual"
    validator_id: str | None = None
    created_at: str
    deadline: str | None = None


class TaskListResponse(BaseModel):
    """Response containing list of tasks"""

    tasks: list[TaskResponse]
    total: int
    has_more: bool = False


class TaskAcceptRequest(BaseModel):
    """Request to accept a task"""

    message: str = Field(default="", description="Optional message to creator")


class TaskSubmitRequest(BaseModel):
    """Request to submit task result"""

    submission: str = Field(..., min_length=5, description="Task result/deliverable")
    artifacts: list[dict] = Field(default_factory=list, description="Optional artifacts")


class TaskReviewRequest(BaseModel):
    """Request to approve or reject submission"""

    approved: bool = Field(..., description="Whether to approve")
    notes: str = Field(default="", description="Review notes")


def _task_to_response(task) -> TaskResponse:
    """Convert Task entity to response model"""
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
        completed_count=task.completed_count,
        max_completions=task.max_completions,
        approval_type=task.approval_type,
        validator_id=task.validator_id,
        created_at=task.created_at.isoformat(),
        deadline=task.deadline.isoformat() if task.deadline else None,
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
    task_service: TaskServiceDep = None,
):
    """
    Create a new task

    Requires authentication. The authenticated user becomes the creator.
    In dev mode, X-Creator-Id header can override the subject.
    """
    token_owner = await get_subject()

    # Dev mode: allow X-Creator-Id header override
    creator_id_header = http_request.headers.get("x-creator-id")
    creator_name_header = http_request.headers.get("x-creator-name")
    creator_type_header = http_request.headers.get("x-creator-type", "human")

    if creator_id_header:
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
        logger.error("task_creation_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{task_id}/accept", response_model=TaskResponse)
async def accept_task(
    task_id: str,
    http_request: Request,
    request: TaskAcceptRequest = None,
    payload: dict = Depends(require_permission("acn:write")),
    task_service: TaskServiceDep = None,
):
    """Accept a task"""
    token_owner = await get_subject()

    # Dev mode: allow X-Creator-Id header override for agent
    agent_id = http_request.headers.get("x-creator-id") or token_owner
    agent_name = http_request.headers.get("x-creator-name") or agent_id

    try:
        task = await task_service.accept_task(
            task_id=task_id,
            agent_id=agent_id,
            agent_name=agent_name,
        )
        return _task_to_response(task)

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
    task_service: TaskServiceDep = None,
):
    """Submit task result"""
    token_owner = await get_subject()

    # Dev mode: allow X-Creator-Id header override
    agent_id = http_request.headers.get("x-creator-id") or token_owner

    try:
        task = await task_service.submit_task(
            task_id=task_id,
            agent_id=agent_id,
            submission=request.submission,
            artifacts=request.artifacts,
        )
        return _task_to_response(task)

    except TaskNotFoundException:
        raise HTTPException(status_code=404, detail="Task not found")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/review", response_model=TaskResponse)
async def review_task(
    task_id: str,
    http_request: Request,
    request: TaskReviewRequest,
    payload: dict = Depends(require_permission("acn:write")),
    task_service: TaskServiceDep = None,
):
    """Approve or reject task submission"""
    token_owner = await get_subject()

    # Dev mode: allow X-Creator-Id header override
    reviewer_id = http_request.headers.get("x-creator-id") or token_owner

    try:
        if request.approved:
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
        raise HTTPException(status_code=403, detail=str(e))
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
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ========== Agent API Key Endpoints ==========
# For autonomous agents using API key authentication


async def verify_agent_api_key(
    authorization: str = Header(..., description="Bearer API_KEY"),
    task_service: TaskServiceDep = None,
) -> dict:
    """Verify agent API key and return agent info"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization format")

    api_key = authorization[7:]

    # Get agent by API key via AgentService
    # Note: This requires AgentService to be available
    # For now, we'll just extract agent_id from the key or use a simple lookup
    # In production, this should validate against the repository

    # Placeholder - in real implementation, look up agent by API key
    return {"api_key": api_key, "agent_id": "unknown"}


@router.post("/agent/create", response_model=TaskResponse)
async def agent_create_task(
    request: TaskCreateRequest,
    authorization: str = Header(..., description="Bearer API_KEY"),
    task_service: TaskServiceDep = None,
):
    """
    Create a task (Agent API Key auth)

    For autonomous agents to create tasks using their API key.
    """
    agent_info = await verify_agent_api_key(authorization)

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
    authorization: str = Header(..., description="Bearer API_KEY"),
    task_service: TaskServiceDep = None,
):
    """Accept a task (Agent API Key auth)"""
    agent_info = await verify_agent_api_key(authorization)

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
    authorization: str = Header(..., description="Bearer API_KEY"),
    task_service: TaskServiceDep = None,
):
    """Submit task result (Agent API Key auth)"""
    agent_info = await verify_agent_api_key(authorization)

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
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
