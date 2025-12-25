"""Communication API Routes

Clean Architecture implementation: Route → MessageService → MessageRouter
"""

import structlog  # type: ignore[import-untyped]
from a2a.types import Message, TextPart  # type: ignore[import-untyped]
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..core.exceptions import AgentNotFoundException
from .dependencies import (  # type: ignore[import-untyped]
    AuditDep,
    MessageServiceDep,
    MetricsDep,
    RouterDep,
)

router = APIRouter(prefix="/api/v1/communication", tags=["communication"])
logger = structlog.get_logger()


class SendMessageRequest(BaseModel):
    from_agent: str
    target_agent: str
    message: dict  # A2A Message dict
    priority: str = "normal"


class BroadcastRequest(BaseModel):
    from_agent: str
    message: dict  # A2A Message dict
    strategy: str = "parallel"
    target_subnet: str | None = None
    target_skills: list[str] | None = None


class BroadcastBySkillRequest(BaseModel):
    from_agent: str
    skills: list[str]
    message: dict  # A2A Message dict
    limit: int | None = None


@router.post("/send")
async def send_message(
    request: SendMessageRequest,
    message_service: MessageServiceDep = None,
    metrics: MetricsDep = None,
    audit: AuditDep = None,
):
    """Send message to specific agent

    Clean Architecture: Route → MessageService → Repository + MessageRouter
    """
    try:
        # Convert dict to A2A Message (simplified)
        # In production, use proper A2A Message construction
        message = Message(
            role="user",
            parts=[TextPart(text=str(request.message))],
        )

        # Use MessageService (Clean Architecture)
        result = await message_service.send_message(
            from_agent_id=request.from_agent,
            to_agent_id=request.target_agent,
            message=message,
            priority=request.priority,
        )

        # Record metrics
        await metrics.record_message(
            from_agent=request.from_agent,
            to_agent=request.target_agent,
            message_type="direct",
            success=True,
        )

        # Audit log
        await audit.log_event(
            event_type="message_sent",
            actor=request.from_agent,
            resource=request.target_agent,
            details={"message_id": result.get("message_id")},
        )

        logger.info(
            "message_sent",
            from_agent=request.from_agent,
            to_agent=request.target_agent,
        )

        return result

    except AgentNotFoundException as e:
        logger.error("message_send_failed", error=str(e))
        await metrics.record_message(
            from_agent=request.from_agent,
            to_agent=request.target_agent,
            message_type="direct",
            success=False,
        )
        raise HTTPException(status_code=404, detail=str(e)) from e

    except Exception as e:
        logger.error("message_send_failed", error=str(e))
        await metrics.record_message(
            from_agent=request.from_agent,
            to_agent=request.target_agent,
            message_type="direct",
            success=False,
        )
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/broadcast")
async def broadcast_message(
    request: BroadcastRequest,
    message_service: MessageServiceDep = None,
    metrics: MetricsDep = None,
):
    """Broadcast message to multiple agents

    Clean Architecture: Route → MessageService → Repository + MessageRouter
    """
    try:
        # Convert dict to A2A Message
        message = Message(
            role="user",
            parts=[TextPart(text=str(request.message))],
        )

        # Use MessageService (Clean Architecture)
        responses = await message_service.broadcast_message(
            from_agent_id=request.from_agent,
            message=message,
            subnet_id=request.target_subnet,
            skills=request.target_skills,
            strategy=request.strategy,
        )

        # Record metrics
        success_count = len([r for r in responses if r.get("status") == "success"])
        await metrics.record_broadcast(
            message_type="broadcast",
            target_count=len(responses),
            success=True,
        )

        logger.info(
            "message_broadcasted",
            from_agent=request.from_agent,
            target_count=len(responses),
            success_count=success_count,
        )

        return {
            "status": "broadcasted",
            "from_agent": request.from_agent,
            "responses": responses,
            "total": len(responses),
            "successful": success_count,
        }

    except AgentNotFoundException as e:
        logger.error("broadcast_failed", error=str(e))
        raise HTTPException(status_code=404, detail=str(e)) from e

    except Exception as e:
        logger.error("broadcast_failed", error=str(e))
        await metrics.record_broadcast(
            message_type="broadcast",
            target_count=0,
            success=False,
        )
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/broadcast-by-skill")
async def broadcast_by_skill(
    request: BroadcastBySkillRequest,
    message_service: MessageServiceDep = None,
    metrics: MetricsDep = None,
):
    """Broadcast to agents with specific skills

    Clean Architecture: Route → MessageService → Repository
    """
    try:
        # Convert dict to A2A Message
        message = Message(
            role="user",
            parts=[TextPart(text=str(request.message))],
        )

        # Use MessageService with skill filter
        responses = await message_service.broadcast_message(
            from_agent_id=request.from_agent,
            message=message,
            skills=request.skills,
            strategy="parallel",
        )

        # Apply limit if specified
        if request.limit:
            responses = responses[: request.limit]

        # Record metrics
        success_count = len([r for r in responses if r.get("status") == "success"])
        await metrics.record_broadcast(
            message_type="skill_broadcast",
            target_count=len(responses),
            success=True,
        )

        logger.info(
            "skill_broadcast_completed",
            from_agent=request.from_agent,
            skills=request.skills,
            target_count=len(responses),
        )

        return {
            "status": "broadcasted",
            "from_agent": request.from_agent,
            "skills": request.skills,
            "responses": responses,
            "total": len(responses),
            "successful": success_count,
        }

    except AgentNotFoundException as e:
        logger.error("skill_broadcast_failed", error=str(e))
        raise HTTPException(status_code=404, detail=str(e)) from e

    except Exception as e:
        logger.error("skill_broadcast_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/history/{agent_id}")
async def get_message_history(
    agent_id: str,
    limit: int = Query(default=100, le=1000),
    message_service: MessageServiceDep = None,
):
    """Get message history for agent

    Clean Architecture: Route → MessageService → MessageRouter
    """
    try:
        # Use MessageService
        history = await message_service.get_message_history(
            agent_id=agent_id,
            limit=limit,
        )

        logger.info("message_history_retrieved", agent_id=agent_id, count=len(history))

        return {
            "agent_id": agent_id,
            "messages": history,
            "count": len(history),
            "limit": limit,
        }

    except AgentNotFoundException as e:
        logger.error("message_history_failed", error=str(e))
        raise HTTPException(status_code=404, detail=str(e)) from e

    except Exception as e:
        logger.error("message_history_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/retry-dlq")
async def retry_dead_letter_queue(
    max_retries: int = Query(default=3, le=10),
    router: RouterDep = None,
):
    """Retry messages from dead letter queue

    Note: Uses MessageRouter directly (infrastructure operation)
    """
    try:
        result = await router.retry_failed_messages(max_retries=max_retries)

        logger.info("dlq_retry_completed", retried=result.get("retried", 0))

        return result

    except Exception as e:
        logger.error("dlq_retry_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e
