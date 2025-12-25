"""
ACN Communication Layer

Layer 2 of ACN architecture, providing:
- Message Router: Routes A2A messages between agents
- Broadcast Service: Multi-agent message broadcasting
- WebSocket Manager: Real-time client connections
- Subnet Manager: Cross-subnet communication (A2A Gateway)

Uses official A2A SDK: https://github.com/a2aproject/A2A

Architecture:
    ┌─────────────────────────────────────────────────┐
    │  Business Services (Chat/Task)                   │
    └──────────────────────┬──────────────────────────┘
                           │ uses
                           ▼
    ┌─────────────────────────────────────────────────┐
    │  ACN Layer 2: Communication                      │
    │                                                  │
    │  ┌─────────────┐  ┌────────────────────────┐   │
    │  │MessageRouter│  │ BroadcastService       │   │
    │  │ - route()   │  │ - send()               │   │
    │  │ - stream()  │  │ - send_by_skill()      │   │
    │  └──────┬──────┘  └───────────┬────────────┘   │
    │         │                     │                 │
    │         └──────────┬──────────┘                 │
    │                    │                            │
    │         ┌──────────▼──────────┐                │
    │         │  Official A2A SDK   │                │
    │         │  (a2a.client)       │                │
    │         └─────────────────────┘                │
    │                                                  │
    │  ┌────────────────────────────────────────┐    │
    │  │  WebSocketManager                       │    │
    │  │  - connect/disconnect                   │    │
    │  │  - broadcast to channels               │    │
    │  │  - Redis Pub/Sub for scaling           │    │
    │  └────────────────────────────────────────┘    │
    │                                                  │
    │  ┌────────────────────────────────────────┐    │
    │  │  SubnetManager (A2A Gateway)           │    │
    │  │  - Bridge agents behind NAT/firewall   │    │
    │  │  - WebSocket tunnel for connectivity   │    │
    │  └────────────────────────────────────────┘    │
    └──────────────────────┬──────────────────────────┘
                           │ queries
                           ▼
    ┌─────────────────────────────────────────────────┐
    │  ACN Layer 1: Registry                          │
    │  - Agent discovery                              │
    │  - Endpoint resolution                          │
    └─────────────────────────────────────────────────┘

Usage:
    from a2a.types import Message, TextPart, DataPart
    from acn.communication import (
        MessageRouter,
        BroadcastService,
        WebSocketManager,
        create_text_message,
        create_notification_message,
    )

    # Initialize
    router = MessageRouter(registry, redis_client)
    broadcast = BroadcastService(router, redis_client)
    ws_manager = WebSocketManager(redis_client)

    # Route message to single agent (using official A2A types)
    response = await router.route(
        from_agent="chat-service",
        to_agent="cursor-agent",
        message=Message(role="user", parts=[TextPart(text="Generate login page")])
    )

    # Or use helper functions
    response = await router.route(
        from_agent="chat-service",
        to_agent="cursor-agent",
        message=create_text_message("Generate login page")
    )

    # Broadcast to multiple agents
    result = await broadcast.send(
        from_agent="chat-service",
        to_agents=["cursor-agent", "figma-agent"],
        message=create_notification_message(
            notification_type="group_chat_mention",
            content="@all 项目开始了！",
            metadata={"chat_id": "chat-123"}
        )
    )

    # WebSocket broadcast to frontend
    await ws_manager.broadcast_chat_message(
        chat_id="chat-123",
        message={"sender": "figma-agent", "content": "设计完成!"}
    )
"""

# Re-export official A2A types for convenience
from a2a.types import DataPart, Message, Part, Role, TextPart

from .broadcast_service import BroadcastResult, BroadcastService, BroadcastStrategy
from .message_router import (
    MessageRouter,
    create_data_message,
    create_notification_message,
    create_text_message,
)
from .subnet_manager import GatewayMessageType, SubnetManager
from .websocket_manager import Connection, MessageType, WebSocketManager

__all__ = [
    # Core classes
    "MessageRouter",
    "BroadcastService",
    "WebSocketManager",
    "SubnetManager",
    # Official A2A types (re-exported)
    "Message",
    "TextPart",
    "DataPart",
    "Part",
    "Role",
    # Helper functions
    "create_text_message",
    "create_data_message",
    "create_notification_message",
    # Other types
    "BroadcastResult",
    "BroadcastStrategy",
    "MessageType",
    "Connection",
    "GatewayMessageType",
]
