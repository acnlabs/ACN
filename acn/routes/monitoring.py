"""Monitoring & Metrics API Routes"""

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from .dependencies import AnalyticsDep, InternalTokenDep, MetricsDep  # type: ignore[import-untyped]

router = APIRouter(tags=["monitoring"])


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics(_: InternalTokenDep, metrics: MetricsDep = None):
    """Prometheus metrics endpoint (requires X-Internal-Token)"""
    return await metrics.export_prometheus()


@router.get("/api/v1/monitoring/metrics")
async def get_all_metrics(_: InternalTokenDep, metrics: MetricsDep = None):
    """Get all metrics (requires X-Internal-Token)"""
    return await metrics.get_all_metrics()


@router.get("/api/v1/monitoring/health")
async def get_system_health(_: InternalTokenDep, metrics: MetricsDep = None):
    """Get system health status (requires X-Internal-Token)"""
    return await metrics.get_health_status()


@router.get("/api/v1/monitoring/dashboard")
async def get_dashboard_data(
    _: InternalTokenDep,
    metrics: MetricsDep = None,
    analytics: AnalyticsDep = None,
):
    """Get dashboard data (requires X-Internal-Token)"""
    return {
        "metrics": await metrics.get_summary(),
        "analytics": await analytics.get_summary(),
    }
