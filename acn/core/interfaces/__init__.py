"""Repository Interfaces

Abstract interfaces for data access (Port pattern in Hexagonal Architecture).
Infrastructure layer implements these interfaces.
"""

from .agent_repository import IAgentRepository
from .allowlist_repository import AllowlistEntry, IAllowlistRepository
from .escrow_provider import (
    EscrowDetailResult,
    EscrowResult,
    IEscrowProvider,
    ReleaseResult,
)
from .follow_repository import IFollowRepository
from .join_flow_event_publisher import (
    IJoinFlowEventPublisher,
    JoinFlowEventTrigger,
    JoinFlowEventType,
    JoinFlowEventVia,
)
from .settlement_outbox_repository import (
    ISettlementOutboxRepository,
    SettlementEvent,
)
from .subnet_allowlist_repository import ISubnetAllowlistRepository
from .subnet_join_request_repository import ISubnetJoinRequestRepository
from .subnet_repository import ISubnetRepository
from .task_repository import ITaskRepository
from .unit_of_work import IUnitOfWork

# IActivityRepository and IBillingRepository are imported directly from their
# modules to avoid circular imports (they reference service-layer types).
__all__ = [
    "IAgentRepository",
    "IAllowlistRepository",
    "AllowlistEntry",
    "IFollowRepository",
    "IJoinFlowEventPublisher",
    "JoinFlowEventTrigger",
    "JoinFlowEventType",
    "JoinFlowEventVia",
    "ISettlementOutboxRepository",
    "SettlementEvent",
    "ISubnetAllowlistRepository",
    "ISubnetJoinRequestRepository",
    "ISubnetRepository",
    "ITaskRepository",
    "IUnitOfWork",
    "IEscrowProvider",
    "ReleaseResult",
    "EscrowResult",
    "EscrowDetailResult",
]
