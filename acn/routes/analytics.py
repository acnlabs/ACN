"""Analytics API Routes"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from ..services.activity_service import ActivityService
from .dependencies import (  # type: ignore[import-untyped]
    AgentApiKeyDep,
    AnalyticsDep,
    RegistryDep,
    get_agent_service,
)

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


# ========== Activities Response Models ==========


class ActivityEvent(BaseModel):
    """Activity event model"""
    event_id: str
    type: str
    agent_id: str = ""
    agent_name: str = "Unknown"
    description: str = ""
    points: int | None = None
    timestamp: datetime


class ActivitiesResponse(BaseModel):
    """Activities list response"""
    activities: list[ActivityEvent]
    total: int


@router.get("/agents")
async def get_agent_analytics(analytics: AnalyticsDep = None):
    """Get agent analytics summary"""
    return await analytics.get_agent_analytics()


@router.get("/agents/{agent_id}")
async def get_agent_activity(
    agent_id: str,
    agent_info: AgentApiKeyDep,
    days: int = Query(default=7, le=90),
    analytics: AnalyticsDep = None,
):
    """Get specific agent activity (requires Agent API Key; agent may only query its own data)"""
    if agent_info["agent_id"] != agent_id:
        raise HTTPException(
            status_code=403,
            detail="API key does not match agent_id",
        )
    start_time = datetime.now(UTC) - timedelta(days=days)
    return await analytics.get_agent_activity(agent_id, start_time=start_time)


@router.get("/messages")
async def get_message_analytics(analytics: AnalyticsDep = None):
    """Get message analytics"""
    return await analytics.get_message_analytics()


@router.get("/latency")
async def get_latency_analytics(
    hours: int = Query(default=24, le=168),
    analytics: AnalyticsDep = None,
):
    """Get latency analytics"""
    start_time = datetime.now(UTC) - timedelta(hours=hours)
    return await analytics.get_latency_analytics(start_time=start_time)


@router.get("/subnets")
async def get_subnet_analytics(analytics: AnalyticsDep = None):
    """Get subnet analytics"""
    return await analytics.get_subnet_analytics()


# ========== Activities Endpoints ==========


@router.get("/activities", response_model=ActivitiesResponse)
async def list_activities(
    limit: int = Query(default=20, ge=1, le=100),
    user_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    agent_ids: str | None = None,  # Comma-separated list of agent IDs
    authorization: str | None = Header(None, alias="Authorization"),
    registry: RegistryDep = None,
):
    """
    Get recent network activities.

    Without filters: public endpoint, returns latest network-wide activity feed.
    With `agent_id` / `agent_ids` filter: requires Agent API Key (`Authorization: Bearer <key>`);
    the authenticated agent may only query its own activity.

    Query parameters:
    - limit: Maximum number of activities to return (default: 20)
    - user_id: Filter by user/actor (optional)
    - task_id: Filter by task (optional)
    - agent_id: Filter by single agent (optional, requires auth)
    - agent_ids: Filter by multiple agents, comma-separated (optional, requires auth)
    """
    # Enforce auth when filtering by specific agent identity to prevent enumeration
    if agent_id or agent_ids:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail="Authorization header required when filtering by agent_id or agent_ids",
            )
        api_key = authorization[7:]
        agent_service = get_agent_service()
        authed_agent = await agent_service.get_agent_by_api_key(api_key)
        if not authed_agent:
            raise HTTPException(status_code=401, detail="Invalid API key")

        # Agent may only query its own activity
        requested_ids = set()
        if agent_id:
            requested_ids.add(agent_id)
        if agent_ids:
            requested_ids.update(aid.strip() for aid in agent_ids.split(",") if aid.strip())
        if any(aid != authed_agent.agent_id for aid in requested_ids):
            raise HTTPException(
                status_code=403,
                detail="API key does not match the requested agent_id(s)",
            )
    # Create ActivityService with registry's redis
    activity_service = ActivityService(redis=registry.redis)

    # Parse agent_ids if provided
    agent_id_list = None
    if agent_ids:
        agent_id_list = [aid.strip() for aid in agent_ids.split(",") if aid.strip()]

    # Get activities
    raw_activities = await activity_service.list_activities(
        limit=limit,
        user_id=user_id,
        task_id=task_id,
        agent_id=agent_id,
        agent_ids=agent_id_list,
    )

    # Convert to response model
    activities = []
    for event_dict in raw_activities:
        try:
            timestamp_str = event_dict.get("timestamp", datetime.now(UTC).isoformat())
            timestamp = datetime.fromisoformat(timestamp_str)
        except (ValueError, TypeError):
            timestamp = datetime.now(UTC)

        activities.append(
            ActivityEvent(
                event_id=event_dict.get("event_id", ""),
                type=event_dict.get("type", "unknown"),
                agent_id=event_dict.get("actor_id", event_dict.get("agent_id", "")),
                agent_name=event_dict.get("actor_name", event_dict.get("agent_name", "Unknown")),
                description=event_dict.get("description", ""),
                points=event_dict.get("points"),
                timestamp=timestamp,
            )
        )

    return ActivitiesResponse(activities=activities, total=len(activities))
