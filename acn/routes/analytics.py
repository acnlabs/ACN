"""Analytics API Routes"""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from .dependencies import AnalyticsDep, RegistryDep  # type: ignore[import-untyped]
from ..services.activity_service import ActivityService

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


# ========== Activities Response Models ==========


class ActivityEvent(BaseModel):
    """Activity event model"""
    event_id: str
    type: str
    agent_id: str = ""
    agent_name: str = "Unknown"
    description: str = ""
    points: Optional[int] = None
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
    days: int = Query(default=7, le=90),
    analytics: AnalyticsDep = None,
):
    """Get specific agent activity"""
    start_time = datetime.now() - timedelta(days=days)
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
    start_time = datetime.now() - timedelta(hours=hours)
    return await analytics.get_latency_analytics(start_time=start_time)


@router.get("/subnets")
async def get_subnet_analytics(analytics: AnalyticsDep = None):
    """Get subnet analytics"""
    return await analytics.get_subnet_analytics()


# ========== Activities Endpoints ==========


@router.get("/activities", response_model=ActivitiesResponse)
async def list_activities(
    limit: int = Query(default=20, le=100),
    user_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    agent_ids: str | None = None,  # Comma-separated list of agent IDs
    registry: RegistryDep = None,
):
    """
    Get recent network activities

    Query parameters:
    - limit: Maximum number of activities to return (default: 20)
    - user_id: Filter by user/actor (optional)
    - task_id: Filter by task (optional)
    - agent_id: Filter by single agent (optional)
    - agent_ids: Filter by multiple agents, comma-separated (optional)

    Example:
    ```bash
    curl https://acn.agenticplanet.space/api/v1/analytics/activities?limit=20
    curl https://acn.agenticplanet.space/api/v1/analytics/activities?user_id=user123
    curl https://acn.agenticplanet.space/api/v1/analytics/activities?agent_ids=agent1,agent2
    ```
    """
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
            timestamp_str = event_dict.get("timestamp", datetime.now().isoformat())
            timestamp = datetime.fromisoformat(timestamp_str)
        except (ValueError, TypeError):
            timestamp = datetime.now()
            
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
