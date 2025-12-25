"""ACN API Routes

Modular routing structure for better maintainability.
"""

from . import (
    analytics,
    communication,
    dependencies,
    monitoring,
    payments,
    registry,
    subnets,
    websocket,
)

__all__ = [
    "dependencies",
    "registry",
    "communication",
    "subnets",
    "monitoring",
    "analytics",
    "payments",
    "websocket",
]
