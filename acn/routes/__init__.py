"""ACN API Routes

Modular routing structure for better maintainability.
"""

from . import (
    analytics,
    communication,
    dependencies,
    follows,
    gateway_connect,
    manifest,
    monitoring,
    payments,
    registry,
    sessions,
    subnets,
    tasks,
    websocket,
)

__all__ = [
    "dependencies",
    "registry",
    "follows",
    "communication",
    "gateway_connect",
    "sessions",
    "subnets",
    "monitoring",
    "analytics",
    "payments",
    "tasks",
    "websocket",
]
