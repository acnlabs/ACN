"""ACN API Routes

Modular routing structure for better maintainability.
"""

from . import (
    analytics,
    communication,
    dependencies,
    monitoring,
    onboarding,
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
    "onboarding",
]
