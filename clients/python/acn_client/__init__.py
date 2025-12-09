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
    BroadcastRequest,
    BroadcastStrategy,
    MessageType,
    PaymentCapability,
    PaymentMethod,
    PaymentNetwork,
    PaymentTask,
    PaymentTaskStatus,
    SendMessageRequest,
    SubnetInfo,
)
from .realtime import ACNRealtime

__version__ = "0.1.0"
__all__ = [
    # Client
    "ACNClient",
    "ACNError",
    "ACNRealtime",
    # Models
    "AgentInfo",
    "AgentRegisterRequest",
    "AgentSearchOptions",
    "BroadcastRequest",
    "BroadcastStrategy",
    "MessageType",
    "PaymentCapability",
    "PaymentMethod",
    "PaymentNetwork",
    "PaymentTask",
    "PaymentTaskStatus",
    "SendMessageRequest",
    "SubnetInfo",
]

