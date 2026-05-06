"""ACN API Routes

Modular routing structure for better maintainability.
"""

from . import (
    analytics,
    communication,
    dependencies,
    follows,
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
    "sessions",
    "subnets",
    "monitoring",
    "analytics",
    "payments",
    "tasks",
    "websocket",
]
