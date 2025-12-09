"""
ACN Client Models

Type definitions synced with ACN API models.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# ============================================
# Enums
# ============================================


class AgentStatus(str, Enum):
    """Agent status"""

    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"


class MessageType(str, Enum):
    """Message types"""

    TEXT = "text"
    DATA = "data"
    NOTIFICATION = "notification"
    TASK = "task"
    RESULT = "result"


class BroadcastStrategy(str, Enum):
    """Broadcast strategy"""

    ALL = "all"
    RANDOM = "random"
    ROUND_ROBIN = "round_robin"
    LOAD_BALANCED = "load_balanced"


class PaymentMethod(str, Enum):
    """Supported payment methods"""

    USDC = "USDC"
    USDT = "USDT"
    ETH = "ETH"
    DAI = "DAI"
    CREDIT_CARD = "CREDIT_CARD"
    BANK_TRANSFER = "BANK_TRANSFER"
    PLATFORM_CREDITS = "PLATFORM_CREDITS"


class PaymentNetwork(str, Enum):
    """Supported networks"""

    ETHEREUM = "ETHEREUM"
    POLYGON = "POLYGON"
    BASE = "BASE"
    ARBITRUM = "ARBITRUM"
    OPTIMISM = "OPTIMISM"
    SOLANA = "SOLANA"


class PaymentTaskStatus(str, Enum):
    """Payment task status"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ============================================
# Agent Models
# ============================================


class AgentInfo(BaseModel):
    """Agent information"""

    id: str = Field(alias="agent_id")
    name: str

    class Config:
        populate_by_name = True

    description: str | None = None
    skills: list[str] = Field(default_factory=list)
    status: AgentStatus = AgentStatus.OFFLINE
    endpoint: str | None = None
    metadata: dict[str, Any] | None = None
    subnets: list[str] | None = None
    created_at: datetime | None = None
    last_seen: datetime | None = None

    # Payment capability
    wallet_address: str | None = None
    accepts_payment: bool = False
    payment_methods: list[str] | None = None
    supported_networks: list[str] | None = None


class AgentRegisterRequest(BaseModel):
    """Agent registration request"""

    id: str = Field(alias="agent_id")
    name: str
    description: str | None = None
    skills: list[str] = Field(default_factory=list)
    endpoint: str | None = None
    metadata: dict[str, Any] | None = None
    wallet_address: str | None = None
    payment_capability: "PaymentCapability | None" = None

    class Config:
        populate_by_name = True


class AgentSearchOptions(BaseModel):
    """Agent search options"""

    skills: str | None = None
    status: AgentStatus | None = None
    subnet_id: str | None = None


# ============================================
# Subnet Models
# ============================================


class SubnetInfo(BaseModel):
    """Subnet information"""

    id: str = Field(alias="subnet_id")
    name: str

    class Config:
        populate_by_name = True

    description: str | None = None
    created_at: datetime | None = None
    agent_count: int = 0
    metadata: dict[str, Any] | None = None


class SubnetCreateRequest(BaseModel):
    """Subnet creation request"""

    name: str
    description: str | None = None
    metadata: dict[str, Any] | None = None


# ============================================
# Communication Models
# ============================================


class Message(BaseModel):
    """A2A Message"""

    id: str
    type: MessageType
    from_agent: str
    to_agent: str | None = None
    content: Any
    timestamp: datetime
    metadata: dict[str, Any] | None = None


class SendMessageRequest(BaseModel):
    """Send message request"""

    from_agent: str
    to_agent: str
    message_type: MessageType
    content: Any
    metadata: dict[str, Any] | None = None


class BroadcastRequest(BaseModel):
    """Broadcast request"""

    from_agent: str
    message_type: MessageType
    content: Any
    strategy: BroadcastStrategy = BroadcastStrategy.ALL
    target_agents: list[str] | None = None
    metadata: dict[str, Any] | None = None


# ============================================
# Payment Models
# ============================================


class PaymentCapability(BaseModel):
    """Payment capability"""

    accepts_payment: bool = True
    wallet_address: str | None = None
    supported_methods: list[PaymentMethod] = Field(default_factory=list)
    supported_networks: list[PaymentNetwork] = Field(default_factory=list)
    min_amount: float | None = None
    max_amount: float | None = None
    currency: str = "USD"


class PaymentTask(BaseModel):
    """Payment task"""

    id: str
    payer_agent_id: str
    payee_agent_id: str
    amount: float
    currency: str
    method: PaymentMethod
    network: PaymentNetwork | None = None
    status: PaymentTaskStatus
    created_at: datetime
    updated_at: datetime
    transaction_hash: str | None = None
    metadata: dict[str, Any] | None = None


class PaymentStats(BaseModel):
    """Payment statistics"""

    total_received: float = 0
    total_sent: float = 0
    transaction_count: int = 0
    avg_amount: float = 0


# ============================================
# Monitoring Models
# ============================================


class DashboardData(BaseModel):
    """Dashboard data"""

    agents: dict[str, int] = Field(default_factory=dict)
    messages: dict[str, int] = Field(default_factory=dict)
    subnets: dict[str, int] = Field(default_factory=dict)
