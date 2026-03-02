"""
ACN Client - Official Python SDK for Agent Collaboration Network

Example:
    >>> from acn_client import ACNClient
    >>>
    >>> async with ACNClient("http://localhost:9000") as client:
    ...     agents = await client.search_agents(skills=["coding"])
    ...     print(f"Found {len(agents)} agents")
"""

from .client import ACNClient, ACNError
from .models import (
    AgentInfo,
    AgentRegisterRequest,
    AgentSearchOptions,
    AgentStatus,
    BroadcastRequest,
    BroadcastStrategy,
    MessageType,
    ParticipationInfo,
    PaymentCapability,
    PaymentMethod,
    PaymentNetwork,
    PaymentTask,
    PaymentTaskStatus,
    SendMessageRequest,
    SubnetInfo,
    TaskAcceptRequest,
    TaskAcceptResponse,
    TaskCreateRequest,
    TaskInfo,
    TaskReviewRequest,
    TaskSubmitRequest,
)
from .realtime import ACNRealtime

__version__ = "0.4.0"
__all__ = [
    # Client
    "ACNClient",
    "ACNError",
    "ACNRealtime",
    # Agent models
    "AgentInfo",
    "AgentRegisterRequest",
    "AgentSearchOptions",
    "AgentStatus",
    # Communication models
    "BroadcastRequest",
    "BroadcastStrategy",
    "MessageType",
    "SendMessageRequest",
    # Subnet models
    "SubnetInfo",
    # Payment models
    "PaymentCapability",
    "PaymentMethod",
    "PaymentNetwork",
    "PaymentTask",
    "PaymentTaskStatus",
    # Task models
    "TaskInfo",
    "TaskCreateRequest",
    "TaskAcceptRequest",
    "TaskAcceptResponse",
    "TaskSubmitRequest",
    "TaskReviewRequest",
    "ParticipationInfo",
]
