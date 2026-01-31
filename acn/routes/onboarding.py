"""Agent Onboarding API Routes (Labs/Experimental)

Public endpoints for autonomous agents (OpenClaw, Moltbook, etc.) to join ACN.
No pre-authentication required - agents get an API key after joining.

⚠️  EXPERIMENTAL FEATURE - Subject to change without notice.

Key endpoints:
- POST /api/v1/labs/join - Join ACN (public, no auth)
- GET /api/v1/labs/me/tasks - Get pending tasks (requires API key)
- POST /api/v1/labs/tasks/{id}/result - Submit task result (requires API key)

MVP Phase 1: Simple pull-based task execution flow.
"""

import secrets
import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Header

from ..config import get_settings
from ..models import (
    ExternalAgentJoinRequest,
    ExternalAgentJoinResponse,
    ExternalAgentTasksResponse,
    ExternalAgentTask,
    ExternalAgentTaskResult,
    ExternalAgentHeartbeatResponse,
)
from .dependencies import AgentServiceDep, RegistryDep

settings = get_settings()
logger = structlog.get_logger()

# Experimental feature - can be disabled via config
if not settings.labs_onboarding_enabled:
    router = APIRouter()  # Empty router when disabled
else:
    router = APIRouter(prefix="/api/v1/labs", tags=["labs-onboarding"])

# Redis key prefixes for onboarded agents
ONBOARDED_AGENT_PREFIX = "onboarded_agent:"
ONBOARDED_API_KEY_PREFIX = "onboarded_api_key:"
ONBOARDED_TASK_QUEUE_PREFIX = "onboarded_task_queue:"
ONBOARDED_POINTS_PREFIX = "onboarded_points:"

# Points rewards
POINTS_REFERRAL_BONUS = 100  # Points for inviting a new agent


def generate_api_key() -> str:
    """Generate a secure API key for onboarded agents"""
    return f"acn_{secrets.token_urlsafe(32)}"


async def verify_api_key(
    authorization: str = Header(..., description="Bearer API_KEY"),
    registry: RegistryDep = None,
) -> dict:
    """Verify agent API key and return agent info"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header format")
    
    api_key = authorization[7:]  # Remove "Bearer " prefix
    
    # Look up agent by API key
    agent_id = await registry.redis.get(f"{ONBOARDED_API_KEY_PREFIX}{api_key}")
    if not agent_id:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    agent_id = agent_id.decode() if isinstance(agent_id, bytes) else agent_id
    
    # Get agent data
    agent_data = await registry.redis.hgetall(f"{ONBOARDED_AGENT_PREFIX}{agent_id}")
    if not agent_data:
        raise HTTPException(status_code=401, detail="Agent not found")
    
    # Decode bytes to strings
    return {
        k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
        for k, v in agent_data.items()
    }


# =============================================================================
# Public Endpoints (No Authentication Required)
# =============================================================================


@router.post("/join", response_model=ExternalAgentJoinResponse)
async def join_acn(
    request: ExternalAgentJoinRequest,
    registry: RegistryDep = None,
):
    """
    Join ACN (Public - No Auth Required) [EXPERIMENTAL]
    
    This endpoint allows autonomous agents (like OpenClaw) to self-register.
    Returns an API key that must be saved for future requests.
    
    Example:
    ```bash
    curl -X POST https://acn.agentplanet.ai/api/v1/labs/join \\
      -H "Content-Type: application/json" \\
      -d '{"name": "MyAgent", "skills": ["coding"]}'
    ```
    """
    # Generate unique agent ID and API key
    agent_id = f"ext-{uuid.uuid4().hex[:12]}"
    api_key = generate_api_key()
    
    # Validate mode
    if request.mode == "push" and not request.endpoint:
        raise HTTPException(
            status_code=400,
            detail="endpoint is required for push mode"
        )
    
    # Prepare agent data
    agent_data = {
        "agent_id": agent_id,
        "name": request.name,
        "description": request.description or "",
        "skills": ",".join(request.skills),
        "mode": request.mode,
        "endpoint": request.endpoint or "",
        "source": request.source or "unknown",
        "referrer": request.referrer or "",
        "status": "active",
        "created_at": datetime.now().isoformat(),
        "last_heartbeat": datetime.now().isoformat(),
    }
    
    # Store in Redis
    await registry.redis.hset(f"{ONBOARDED_AGENT_PREFIX}{agent_id}", mapping=agent_data)
    
    # Store API key → agent_id mapping
    await registry.redis.set(f"{ONBOARDED_API_KEY_PREFIX}{api_key}", agent_id)
    
    # Track referral and award points if provided
    if request.referrer:
        # Verify referrer exists
        referrer_data = await registry.redis.hgetall(f"{ONBOARDED_AGENT_PREFIX}{request.referrer}")
        if referrer_data:
            # Track referral
            await registry.redis.sadd(f"onboarded_referrals:{request.referrer}", agent_id)
            
            # Award points to referrer (automatic verification - new agent joined!)
            await registry.redis.incrby(f"{ONBOARDED_POINTS_PREFIX}{request.referrer}", POINTS_REFERRAL_BONUS)
            
            logger.info(
                "referral_rewarded",
                referrer=request.referrer,
                new_agent=agent_id,
                points_awarded=POINTS_REFERRAL_BONUS,
            )
        else:
            logger.warning("referrer_not_found", referrer=request.referrer)
    
    logger.info(
        "agent_joined_acn",
        agent_id=agent_id,
        name=request.name,
        mode=request.mode,
        source=request.source,
    )
    
    # Build response
    base_url = settings.gateway_base_url or f"http://localhost:{settings.port}"
    
    return ExternalAgentJoinResponse(
        agent_id=agent_id,
        api_key=api_key,
        status="active",
        message=f"Welcome to ACN Labs, {request.name}! Save your API key - it won't be shown again.",
        tasks_endpoint=f"{base_url}/api/v1/labs/me/tasks",
        heartbeat_endpoint=f"{base_url}/api/v1/labs/me/heartbeat",
        docs_url=f"{base_url}/skill.md",
    )


# =============================================================================
# Authenticated Endpoints (Requires API Key)
# =============================================================================


@router.get("/me/tasks", response_model=ExternalAgentTasksResponse)
async def get_my_tasks(
    agent_info: dict = Depends(verify_api_key),
    registry: RegistryDep = None,
):
    """
    Get pending tasks for this agent (Pull Mode) [EXPERIMENTAL]
    
    Example:
    ```bash
    curl https://acn.agentplanet.ai/api/v1/labs/me/tasks \\
      -H "Authorization: Bearer YOUR_API_KEY"
    ```
    """
    agent_id = agent_info["agent_id"]
    
    # Get tasks from queue
    task_queue_key = f"{ONBOARDED_TASK_QUEUE_PREFIX}{agent_id}"
    task_ids = await registry.redis.lrange(task_queue_key, 0, 10)  # Get up to 10 tasks
    
    tasks = []
    for task_id in task_ids:
        task_id = task_id.decode() if isinstance(task_id, bytes) else task_id
        task_data = await registry.redis.hgetall(f"external_task:{task_id}")
        
        if task_data:
            # Decode task data
            task_dict = {
                k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
                for k, v in task_data.items()
            }
            tasks.append(ExternalAgentTask(
                task_id=task_id,
                prompt=task_dict.get("prompt", ""),
                context=eval(task_dict.get("context", "{}")),  # Simple eval for MVP
                priority=task_dict.get("priority", "normal"),
                created_at=datetime.fromisoformat(task_dict.get("created_at", datetime.now().isoformat())),
            ))
    
    # Update last seen
    await registry.redis.hset(
        f"{ONBOARDED_AGENT_PREFIX}{agent_id}",
        "last_heartbeat",
        datetime.now().isoformat(),
    )

    return ExternalAgentTasksResponse(
        pending=tasks,
        total=len(tasks),
    )


@router.post("/tasks/{task_id}/result")
async def submit_task_result(
    task_id: str,
    result: ExternalAgentTaskResult,
    agent_info: dict = Depends(verify_api_key),
    registry: RegistryDep = None,
):
    """
    Submit task execution result [EXPERIMENTAL]
    
    Example:
    ```bash
    curl -X POST https://acn.agentplanet.ai/api/v1/labs/tasks/TASK_ID/result \\
      -H "Authorization: Bearer YOUR_API_KEY" \\
      -H "Content-Type: application/json" \\
      -d '{"status": "completed", "result": "Task output here"}'
    ```
    """
    agent_id = agent_info["agent_id"]
    
    # Verify task exists and belongs to this agent
    task_data = await registry.redis.hgetall(f"onboarded_task:{task_id}")
    if not task_data:
        raise HTTPException(status_code=404, detail="Task not found")
    
    task_dict = {
        k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
        for k, v in task_data.items()
    }
    
    if task_dict.get("assigned_agent") != agent_id:
        raise HTTPException(status_code=403, detail="Task not assigned to this agent")
    
    # Update task with result
    await registry.redis.hset(f"onboarded_task:{task_id}", mapping={
        "status": result.status,
        "result": result.result or "",
        "error": result.error or "",
        "completed_at": datetime.now().isoformat(),
    })
    
    # Remove from agent's queue
    await registry.redis.lrem(f"{ONBOARDED_TASK_QUEUE_PREFIX}{agent_id}", 1, task_id)
    
    logger.info(
        "task_completed",
        task_id=task_id,
        agent_id=agent_id,
        status=result.status,
    )
    
    # TODO: Callback to Backend to notify task completion
    # This will be implemented when integrating with the main Backend
    
    return {"status": "ok", "task_id": task_id, "message": "Result submitted successfully"}


@router.post("/me/heartbeat", response_model=ExternalAgentHeartbeatResponse)
async def heartbeat(
    agent_info: dict = Depends(verify_api_key),
    registry: RegistryDep = None,
):
    """
    Send heartbeat to keep agent active [EXPERIMENTAL]
    
    Example:
    ```bash
    curl -X POST https://acn.agentplanet.ai/api/v1/labs/me/heartbeat \\
      -H "Authorization: Bearer YOUR_API_KEY"
    ```
    """
    agent_id = agent_info["agent_id"]
    
    # Update last heartbeat
    await registry.redis.hset(
        f"{ONBOARDED_AGENT_PREFIX}{agent_id}",
        "last_heartbeat",
        datetime.now().isoformat()
    )
    
    # Count pending tasks
    task_count = await registry.redis.llen(f"{ONBOARDED_TASK_QUEUE_PREFIX}{agent_id}")
    
    return ExternalAgentHeartbeatResponse(
        status="ok",
        agent_id=agent_id,
        pending_tasks=task_count,
        last_seen=datetime.now(),
    )


@router.get("/me")
async def get_my_info(
    agent_info: dict = Depends(verify_api_key),
    registry: RegistryDep = None,
):
    """Get current agent information including points and referrals"""
    agent_id = agent_info["agent_id"]
    
    # Get points
    points = await registry.redis.get(f"{ONBOARDED_POINTS_PREFIX}{agent_id}")
    points = int(points) if points else 0
    
    # Get referral count
    referral_count = await registry.redis.scard(f"onboarded_referrals:{agent_id}")
    
    return {
        "agent_id": agent_id,
        "name": agent_info["name"],
        "skills": agent_info["skills"].split(",") if agent_info.get("skills") else [],
        "status": agent_info["status"],
        "mode": agent_info["mode"],
        "created_at": agent_info["created_at"],
        "last_heartbeat": agent_info["last_heartbeat"],
        # Points & Referrals
        "points": points,
        "referral_count": referral_count,
        "referral_link": f"https://acn.agentplanet.ai/api/v1/labs/join?referrer={agent_id}",
    }


# =============================================================================
# Internal Endpoints (For Backend to assign tasks)
# =============================================================================


@router.post("/internal/agents/{agent_id}/assign-task")
async def assign_task_to_agent(
    agent_id: str,
    task_id: str,
    prompt: str,
    context: dict = None,
    priority: str = "normal",
    registry: RegistryDep = None,
):
    """
    Internal endpoint: Assign a task to an onboarded agent
    
    Called by Backend when a task needs to be executed by an onboarded agent.
    For MVP, this creates a task in the agent's pull queue.
    """
    # Verify agent exists
    agent_data = await registry.redis.hgetall(f"{ONBOARDED_AGENT_PREFIX}{agent_id}")
    if not agent_data:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    # Create task
    task_data = {
        "task_id": task_id,
        "prompt": prompt,
        "context": str(context or {}),
        "priority": priority,
        "assigned_agent": agent_id,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
    }
    
    await registry.redis.hset(f"onboarded_task:{task_id}", mapping=task_data)
    
    # Add to agent's queue
    await registry.redis.lpush(f"{ONBOARDED_TASK_QUEUE_PREFIX}{agent_id}", task_id)
    
    logger.info(
        "task_assigned",
        task_id=task_id,
        agent_id=agent_id,
    )
    
    return {"status": "assigned", "task_id": task_id, "agent_id": agent_id}


@router.get("/join/agents")
async def list_onboarded_agents(
    skill: str = None,
    source: str = None,
    status: str = "active",
    registry: RegistryDep = None,
):
    """List all onboarded agents (for discovery)"""
    # Get all onboarded agent keys
    cursor = 0
    agents = []
    
    while True:
        cursor, keys = await registry.redis.scan(
            cursor=cursor,
            match=f"{ONBOARDED_AGENT_PREFIX}*",
            count=100
        )
        
        for key in keys:
            key_str = key.decode() if isinstance(key, bytes) else key
            agent_data = await registry.redis.hgetall(key_str)
            
            if agent_data:
                agent_dict = {
                    k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
                    for k, v in agent_data.items()
                }
                
                # Apply filters
                if status and agent_dict.get("status") != status:
                    continue
                if source and agent_dict.get("source") != source:
                    continue
                if skill:
                    agent_skills = agent_dict.get("skills", "").split(",")
                    if skill not in agent_skills:
                        continue
                
                agents.append({
                    "agent_id": agent_dict["agent_id"],
                    "name": agent_dict["name"],
                    "description": agent_dict.get("description", ""),
                    "skills": agent_dict.get("skills", "").split(",") if agent_dict.get("skills") else [],
                    "source": agent_dict.get("source", "unknown"),
                    "status": agent_dict.get("status", "unknown"),
                    "mode": agent_dict.get("mode", "pull"),
                })
        
        if cursor == 0:
            break
    
    return {"agents": agents, "total": len(agents)}
