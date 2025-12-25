"""Communication API Routes"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ...communication import BroadcastStrategy
from ..dependencies import AuditDep, BroadcastDep, MetricsDep, RegistryDep, RouterDep

router = APIRouter(prefix="/api/v1/communication", tags=["communication"])


class SendMessageRequest(BaseModel):
    target_agent: str
    message: dict
    priority: str = "normal"


class BroadcastRequest(BaseModel):
    message: dict
    strategy: BroadcastStrategy = BroadcastStrategy.PARALLEL
    target_agents: list[str] | None = None
    target_skill: str | None = None
    target_subnet: str | None = None


class BroadcastBySkillRequest(BaseModel):
    skill: str
    message: dict
    limit: int | None = None


@router.post("/send")
async def send_message(
    request: SendMessageRequest,
    router: RouterDep = None,
    metrics: MetricsDep = None,
    audit: AuditDep = None,
):
    """Send message to specific agent"""
    try:
        result = await router.route(
            target_agent=request.target_agent,
            message=request.message,
            priority=request.priority,
        )

        await metrics.record_message(
            from_agent="system",
            to_agent=request.target_agent,
            message_type="direct",
            success=True,
        )

        await audit.log_event(
            event_type="message_sent",
            actor="system",
            resource=request.target_agent,
            details={"message_id": result.get("message_id")},
        )

        return result

    except Exception as e:
        await metrics.record_message(
            from_agent="system",
            to_agent=request.target_agent,
            message_type="direct",
            success=False,
        )
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/broadcast")
async def broadcast_message(
    request: BroadcastRequest,
    broadcast: BroadcastDep = None,
    metrics: MetricsDep = None,
):
    """Broadcast message to multiple agents"""
    try:
        result = await broadcast.broadcast(
            message=request.message,
            strategy=request.strategy,
            target_agents=request.target_agents,
            target_skill=request.target_skill,
            target_subnet=request.target_subnet,
        )

        await metrics.record_broadcast(
            message_type="broadcast",
            target_count=len(result.get("recipients", [])),
            success=True,
        )

        return result

    except Exception as e:
        await metrics.record_broadcast(
            message_type="broadcast",
            target_count=0,
            success=False,
        )
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/broadcast-by-skill")
async def broadcast_by_skill(
    request: BroadcastBySkillRequest,
    broadcast: BroadcastDep = None,
    registry: RegistryDep = None,
):
    """Broadcast to agents with specific skill"""
    try:
        agents = await registry.search_agents(skills=[request.skill], status="online")

        if request.limit:
            agents = agents[: request.limit]

        result = await broadcast.broadcast(
            message=request.message,
            strategy=BroadcastStrategy.SKILL,
            target_skill=request.skill,
        )

        return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/history/{agent_id}")
async def get_message_history(
    agent_id: str,
    limit: int = Query(default=100, le=1000),
    offset: int = Query(default=0, ge=0),
    router: RouterDep = None,
):
    """Get message history for agent"""
    try:
        history = await router.get_message_history(
            agent_id=agent_id,
            limit=limit,
            offset=offset,
        )
        return {"agent_id": agent_id, "messages": history, "limit": limit, "offset": offset}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/retry-dlq")
async def retry_dead_letter_queue(
    max_retries: int = Query(default=3, le=10),
    router: RouterDep = None,
):
    """Retry messages from dead letter queue"""
    try:
        result = await router.retry_failed_messages(max_retries=max_retries)
        return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

