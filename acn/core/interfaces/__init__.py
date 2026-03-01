"""Repository Interfaces

Abstract interfaces for data access (Port pattern in Hexagonal Architecture).
Infrastructure layer implements these interfaces.
"""

from .agent_repository import IAgentRepository
from .subnet_repository import ISubnetRepository
from .task_repository import ITaskRepository

# IActivityRepository and IBillingRepository are imported directly from their
# modules to avoid circular imports (they reference service-layer types).
__all__ = [
    "IAgentRepository",
    "ISubnetRepository",
    "ITaskRepository",
]
