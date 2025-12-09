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

    # Record metrics
    metrics.inc_message_count(from_agent="a", to_agent="b")
    metrics.observe_latency(operation="route", latency_ms=15.5)

    # Audit logging
    await audit.log_event(
        event_type="agent_registered",
        agent_id="cursor-agent",
        details={"subnet": "team-a"}
    )

    # Get stats
    stats = await analytics.get_agent_stats()
"""

from .analytics import Analytics
from .audit import AuditLogger
from .metrics import MetricsCollector

__all__ = [
    "MetricsCollector",
    "AuditLogger",
    "Analytics",
]






