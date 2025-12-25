"""Analytics API Routes"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Query

from .dependencies import AnalyticsDep  # type: ignore[import-untyped]

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


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
