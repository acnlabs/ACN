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
    subnets,
    tasks,
    websocket,
)

__all__ = [
    "dependencies",
    "registry",
    "follows",
    "communication",
    "subnets",
    "monitoring",
    "analytics",
    "payments",
    "tasks",
    "websocket",
]
