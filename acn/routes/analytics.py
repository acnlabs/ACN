"""Analytics API Routes

Previously most endpoints here called methods that did not exist on the
Analytics class (`get_agent_analytics`, `get_message_analytics`,
`get_latency_analytics`, `get_subnet_analytics`) and/or passed the wrong
argument name (`start_time=` instead of `hours=`), so every one of them
returned 500. This module now calls the real method names exposed by
acn.monitoring.analytics.Analytics.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from .dependencies import (  # type: ignore[import-untyped]
    ActivityServiceDep,
    AnalyticsDep,
    InternalTokenDep,
    get_agent_service,
    limiter,
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
@limiter.limit("30/minute")
async def get_agent_analytics(request: Request, analytics: AnalyticsDep = None):
    """Get agent analytics summary (public, rate-limited)"""
    return await analytics.get_agent_stats()


@router.get("/agents/{agent_id}")
@limiter.limit("30/minute")
async def get_agent_activity(
    request: Request,
    agent_id: str,
    days: int = Query(default=7, le=90),
    analytics: AnalyticsDep = None,
):
    """Get specific agent activity (public, rate-limited).

    Note: Analytics.get_agent_activity() signature uses `hours`, not
    `start_time`. We convert days→hours here. Since SCALE_AUDIT P1-2
    collapsed the `acn_messages_total` labels to `status`-only, the
    per-agent message/error counters inside get_agent_activity() are
    permanently zero and are now explicitly returned as null; see
    BACKLOG for the PG activity_events plan.
    """
    return await analytics.get_agent_activity(agent_id, hours=days * 24)


@router.get("/messages")
async def get_message_analytics(_: InternalTokenDep, analytics: AnalyticsDep = None):
    """Get message analytics (requires X-Internal-Token)"""
    return await analytics.get_message_stats()


@router.get("/latency")
async def get_latency_analytics(
    _: InternalTokenDep,
    analytics: AnalyticsDep = None,
):
    """Get latency analytics (requires X-Internal-Token).

    Latency stats come straight from the Redis histogram buckets, which
    is an aggregate view with no time-window dimension. The previously
    accepted `hours` query arg was silently ignored by the underlying
    implementation, so it's removed here rather than kept as a lie.
    """
    return await analytics.get_latency_stats()


@router.get("/subnets")
async def get_subnet_analytics(_: InternalTokenDep, analytics: AnalyticsDep = None):
    """Get subnet analytics (requires X-Internal-Token)"""
    return await analytics.get_subnet_stats()


# ========== Activities Endpoints ==========


@router.get("/activities", response_model=ActivitiesResponse)
@limiter.limit("60/minute")
async def list_activities(
    request: Request,
    limit: int = Query(default=20, le=100),
    user_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    agent_ids: str | None = None,  # Comma-separated list of agent IDs
    authorization: str | None = Header(None, alias="Authorization"),
    activity_service: ActivityServiceDep = None,
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
