"""Monitoring & Metrics API Routes"""

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from ..dependencies import AnalyticsDep, MetricsDep

router = APIRouter(tags=["monitoring"])


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics(metrics: MetricsDep = None):
    """Prometheus metrics endpoint"""
    return await metrics.export_prometheus()


@router.get("/api/v1/monitoring/metrics")
async def get_all_metrics(metrics: MetricsDep = None):
    """Get all metrics"""
    return await metrics.get_all_metrics()


@router.get("/api/v1/monitoring/health")
async def get_system_health(metrics: MetricsDep = None):
    """Get system health status"""
    return await metrics.get_health_status()


@router.get("/api/v1/monitoring/dashboard")
async def get_dashboard_data(
    metrics: MetricsDep = None,
    analytics: AnalyticsDep = None,
):
    """Get dashboard data"""
    return {
        "metrics": await metrics.get_summary(),
        "analytics": await analytics.get_summary(),
    }

