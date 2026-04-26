"""
ACN Monitoring & Analytics Layer

Layer 3 of ACN architecture, providing:
- MetricsCollector: Collects and exposes metrics (Prometheus compatible)
- AuditLogger: Records all significant events for auditing
- Analytics: Agent and message statistics

Architecture:
    ┌─────────────────────────────────────────────────────┐
    │  ACN Layer 3: Monitoring & Analytics                │
    │                                                      │
    │  ┌──────────────────┐  ┌──────────────────┐        │
    │  │ MetricsCollector │  │   AuditLogger    │        │
    │  │ - agent_count    │  │ - log_event()    │        │
    │  │ - message_count  │  │ - query_logs()   │        │
    │  │ - latency_ms     │  │ - export()       │        │
    │  └──────────────────┘  └──────────────────┘        │
    │                                                      │
    │  ┌──────────────────────────────────────────┐      │
    │  │             Analytics                     │      │
    │  │ - agent_stats()                          │      │
    │  │ - message_stats()                        │      │
    │  │ - subnet_stats()                         │      │
    │  └──────────────────────────────────────────┘      │
    └─────────────────────────────────────────────────────┘

Usage:
    from acn.monitoring import MetricsCollector, AuditLogger, Analytics

    # Initialize
    metrics = MetricsCollector(redis_client)
    audit = AuditLogger(redis_client)
    analytics = Analytics(redis_client)

    # Record metrics. Note: from_agent / to_agent args exist for
    # signature stability but are intentionally ignored — per-agent
    # dimensions belong in an external TSDB, not in Redis counter keys.
    metrics.inc_message_count(status="success")
    metrics.observe_latency(operation="route", latency_seconds=0.0155)

    # Audit logging
    from acn.monitoring.audit import AuditEventType
    await audit.log_event(
        event_type=AuditEventType.AGENT_REGISTERED,
        actor_id="admin",
        target_id="cursor-agent",
        details={"subnet": "team-a"}
    )

    # Get stats
    stats = await analytics.get_agent_stats()
"""

from .analytics import Analytics
from .audit import (
    AuditEventType,
    AuditLevel,
    AuditLogger,
    fire_and_forget_event,
    get_audit_singleton,
    record_auth_failure,
    set_audit_singleton,
)
from .metrics import MetricsCollector

__all__ = [
    "MetricsCollector",
    "AuditLogger",
    "AuditEventType",
    "AuditLevel",
    "Analytics",
    "fire_and_forget_event",
    "set_audit_singleton",
    "get_audit_singleton",
    "record_auth_failure",
]
