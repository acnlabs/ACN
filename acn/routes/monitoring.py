"""Monitoring & Metrics API Routes

Endpoints delegate to the real method names on MetricsCollector / Analytics.
The previous version of this file invoked:
  - metrics.export_prometheus()  → real is prometheus_export()
  - metrics.get_health_status()  → no such method; correct source is
                                   analytics.get_system_health()
  - metrics.get_summary() and analytics.get_summary()
                                 → no such methods; analytics already
                                   exposes get_dashboard_data() which
                                   aggregates health + agents + messages
                                   + latency + subnets in a single call

so every endpoint here except `GET /metrics` previously returned 500.
"""

from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

from ..monitoring import AuditEventType, AuditLogger
from .dependencies import AnalyticsDep, AuditDep, InternalTokenDep, MetricsDep  # type: ignore[import-untyped]

router = APIRouter(tags=["monitoring"])

PublicSystemEventType = Literal["agent_registered", "subnet_created"]
_PUBLIC_EVENT_TYPE_MAP: dict[PublicSystemEventType, AuditEventType] = {
    "agent_registered": AuditEventType.AGENT_REGISTERED,
    "subnet_created": AuditEventType.SUBNET_CREATED,
}


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics(_: InternalTokenDep, metrics: MetricsDep = None):
    """Prometheus metrics endpoint (requires X-Internal-Token)"""
    return await metrics.prometheus_export()


@router.get("/api/v1/monitoring/metrics")
async def get_all_metrics(_: InternalTokenDep, metrics: MetricsDep = None):
    """Get all metrics as JSON (requires X-Internal-Token)"""
    return await metrics.get_all_metrics()


@router.get("/api/v1/monitoring/health")
async def get_system_health(_: InternalTokenDep, analytics: AnalyticsDep = None):
    """Get system health status (requires X-Internal-Token).

    Delegates to Analytics.get_system_health(), which derives health from
    agent counts, message success rate, and DLQ depth. MetricsCollector
    itself has no health method — it's a raw counter/gauge store.
    """
    return await analytics.get_system_health()


@router.get("/api/v1/monitoring/dashboard")
async def get_dashboard_data(_: InternalTokenDep, analytics: AnalyticsDep = None):
    """Get dashboard data (requires X-Internal-Token).

    Analytics.get_dashboard_data() already aggregates
    { health, agents, messages, latency, subnets, timestamp }
    in a single batched call, so there's no reason to do a second round
    trip to metrics here.
    """
    return await analytics.get_dashboard_data()


@router.get("/api/v1/public/system-events")
async def get_public_system_events(
    event_type: list[PublicSystemEventType] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    audit: AuditDep = None,
):
    """Public system-event feed with fixed, redacted output schema.

    This endpoint is intentionally open (no auth dependency) so frontend and
    third-party consumers can poll a stable read model without touching
    internal audit fields.
    """
    event_types = [_PUBLIC_EVENT_TYPE_MAP[value] for value in event_type] if event_type else None
    events = await audit.query_public_broadcast_events(
        event_types=event_types,
        limit=limit,
        offset=offset,
    )
    items = [
        payload
        for event in events
        if (payload := AuditLogger.to_public_broadcast_payload(event)) is not None
    ]
    return {
        "items": items,
        "limit": limit,
        "offset": offset,
        "count": len(items),
    }
