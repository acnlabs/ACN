"""
ACN - Agent Collaboration Network

Open-source infrastructure for AI Agent collaboration.

Architecture:
┌─────────────────────────────────────────────────────────┐
│  Layer 3: Monitoring & Analytics                         │
│  - MetricsCollector: Performance metrics (Prometheus)   │
│  - AuditLogger: Event logging and compliance            │
│  - Analytics: Statistics and reporting                  │
└─────────────────────────────────────────────────────────┘
                        │ observes
                        ▼
┌─────────────────────────────────────────────────────────┐
│  Layer 2: Communication                                  │
│  - MessageRouter: A2A message routing                   │
│  - BroadcastService: Multi-agent broadcasting           │
│  - WebSocketManager: Real-time client connections       │
│  - SubnetManager: Multi-subnet gateway                  │
└─────────────────────────────────────────────────────────┘
                        │ uses
                        ▼
┌─────────────────────────────────────────────────────────┐
│  Layer 1: Registry & Discovery                          │
│  - AgentRegistry: Agent registration and discovery      │
│  - AgentCard: A2A-compliant agent metadata             │
└─────────────────────────────────────────────────────────┘

Note: Settlement/Payment is NOT part of ACN.
- For platform credits: Use backend PlatformBillingEngine
- For on-chain payments: Integrate AP2 (Agent Payments Protocol)
  https://agentic-commerce-protocol.com/

Based on A2A Protocol: https://github.com/a2aproject/A2A
"""

__version__ = "0.2.0"

# Layer 1: Registry & Discovery
# Layer 2: Communication
from .config import Settings, get_settings
from .infrastructure.messaging import (
    BroadcastResult,
    BroadcastService,
    BroadcastStrategy,
    # Official A2A types (re-exported)
    DataPart,
    GatewayMessageType,
    Message,
    MessageRouter,
    SubnetManager,
    TextPart,
    WebSocketManager,
    # Helper functions
    create_data_message,
    create_notification_message,
    create_text_message,
)

# Layer 3: Monitoring & Analytics
from .infrastructure.persistence.redis.registry import AgentRegistry
from .models import AgentCard, AgentInfo, AgentRegisterRequest, AgentRegisterResponse
from .monitoring import Analytics, AuditLogger, MetricsCollector

__all__ = [
    # Version
    "__version__",
    # Config
    "Settings",
    "get_settings",
    # Layer 1: Models
    "AgentCard",
    "AgentInfo",
    "AgentRegisterRequest",
    "AgentRegisterResponse",
    # Layer 1: Registry
    "AgentRegistry",
    # Layer 2: Communication
    "MessageRouter",
    "BroadcastService",
    "WebSocketManager",
    "SubnetManager",
    "GatewayMessageType",
    "BroadcastResult",
    "BroadcastStrategy",
    # Official A2A types
    "Message",
    "TextPart",
    "DataPart",
    # Helper functions
    "create_text_message",
    "create_data_message",
    "create_notification_message",
    # Layer 3: Monitoring
    "MetricsCollector",
    "AuditLogger",
    "Analytics",
]
