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
    AttentionFee,
    BroadcastRequest,
    BroadcastStrategy,
    ManifestContentResponse,
    ManifestEntry,
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
from .realtime import ACNRealtime, ACNRealtimeOptions, AuthMode, WSState

__version__ = "0.5.1"
__all__ = [
    # Client
    "ACNClient",
    "ACNError",
    "ACNRealtime",
    "ACNRealtimeOptions",
    "AuthMode",
    "WSState",
    # Agent models
    "AgentInfo",
    "AgentRegisterRequest",
    "AgentSearchOptions",
    "AgentStatus",
    # Communication models
    "AttentionFee",
    "BroadcastRequest",
    "BroadcastStrategy",
    "ManifestContentResponse",
    "ManifestEntry",
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
