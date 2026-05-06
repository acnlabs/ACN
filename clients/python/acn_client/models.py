"""
ACN Client Models

Type definitions synced with ACN API models.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# ============================================
# Enums
# ============================================


class AgentStatus(StrEnum):
    """Agent status"""

    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"


class MessageType(StrEnum):
    """Message types"""

    TEXT = "text"
    DATA = "data"
    NOTIFICATION = "notification"
    TASK = "task"
    RESULT = "result"


class BroadcastStrategy(StrEnum):
    """Broadcast strategy"""

    ALL = "all"
    RANDOM = "random"
    ROUND_ROBIN = "round_robin"
    LOAD_BALANCED = "load_balanced"


class PaymentMethod(StrEnum):
    """Supported payment methods"""

    USDC = "USDC"
    USDT = "USDT"
    ETH = "ETH"
    DAI = "DAI"
    CREDIT_CARD = "CREDIT_CARD"
    BANK_TRANSFER = "BANK_TRANSFER"
    PLATFORM_CREDITS = "PLATFORM_CREDITS"


class PaymentNetwork(StrEnum):
    """Supported networks"""

    ETHEREUM = "ETHEREUM"
    POLYGON = "POLYGON"
    BASE = "BASE"
    ARBITRUM = "ARBITRUM"
    OPTIMISM = "OPTIMISM"
    SOLANA = "SOLANA"


class PaymentTaskStatus(StrEnum):
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

    # Owner of the agent (e.g. ``user-{id}`` for end-user-registered agents,
    # ``provider-{id}`` for platform-managed integrations, or ``system`` for
    # ACN-internal/built-in agents). Defaulted to ``None`` for backward
    # compatibility with older ACN responses that may not surface this
    # field — callers should treat ``None`` as "unknown owner" rather than
    # asserting equality. Added during 14.5-3 cleanup so backends can
    # filter ``search_agents`` results by owner without a separate
    # ``GET /agents/{id}`` round-trip.
    owner: str | None = None

    # Payment capability
    wallet_address: str | None = None
    accepts_payment: bool = False
    payment_methods: list[str] | None = None
    supported_networks: list[str] | None = None


class AgentRegisterRequest(BaseModel):
    """Agent registration request - synced with ACN server model"""

    owner: str = Field(..., description="Agent owner (e.g., user-{id} or provider-{id})")
    name: str = Field(..., description="Agent name")
    endpoint: str = Field(..., description="Agent A2A endpoint URL")
    skills: list[str] = Field(default_factory=list, description="Agent skill IDs")
    agent_card: dict[str, Any] | None = Field(
        None, description="Optional Agent Card (auto-generated if not provided)"
    )
    subnet_ids: list[str] | None = Field(None, description="Subnets to join (default: ['public'])")
    # Backward compatibility fields (kept for migration)
    description: str | None = None
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


class AttentionFee(BaseModel):
    """Attention fee attached to a manifest-mode message.

    Locks ``amount`` credits in escrow until the recipient acks the
    manifest entry. Range: 1–1000 credits (≈ $0.01–$10).
    """

    amount: int = Field(..., ge=1, le=1000, description="Credits to lock (1–1000)")
    currency: str = Field(default="credits", description="Only 'credits' supported")


class SendMessageRequest(BaseModel):
    """Send message request — aligned with ACN server v0.5+.

    ``message`` must be a JSON-serialisable dict following the A2A
    message shape, e.g.::

        {"role": "user", "parts": [{"type": "text", "text": "hello"}]}

    ``attention_fee`` and ``content_url`` only take effect when the
    recipient is in manifest mode; they are silently ignored or raise
    400 otherwise (see server docs).
    """

    from_agent: str
    target_agent: str
    message: dict[str, Any]
    priority: str = "normal"
    attention_fee: AttentionFee | None = None
    content_url: str | None = None
    content_hash: str | None = None


class BroadcastRequest(BaseModel):
    """Broadcast request — aligned with ACN server v0.5+."""

    from_agent: str
    message: dict[str, Any]
    strategy: str = "parallel"
    target_subnet: str | None = None
    target_tags: list[str] | None = None


class ManifestEntry(BaseModel):
    """A single manifest queue entry as returned by the ACN server."""

    mid: str
    sender_id: str
    summary: str
    ts_ms: int
    content_size: int
    extra: dict[str, Any] = Field(default_factory=dict)
    acked_at_ms: int | None = None
    expires_at_ms: int | None = None
    content_url: str | None = None
    content_hash: str | None = None


class ManifestContentResponse(BaseModel):
    """Response from GET /communication/content/{mid}.

    ``self_hosted=True`` means the content lives at ``content_url``
    on the sender's server; ``content`` is absent in that case.
    """

    mid: str
    owner_id: str
    self_hosted: bool = False
    content_url: str | None = None
    content_hash: str | None = None
    content: dict[str, Any] | None = None


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
# Task Models
# ============================================


class TaskInfo(BaseModel):
    """Task information — mirrors ACN server TaskResponse."""

    task_id: str
    status: str
    creator_type: str
    creator_id: str
    creator_name: str
    title: str
    description: str
    task_type: str
    required_tags: list[str] = Field(default_factory=list)
    assignee_id: str | None = None
    assignee_name: str | None = None
    assignee_type: str | None = None
    reward: str = "0"
    reward_currency: str = "ap_points"
    total_budget: str = "0"
    released_amount: str = "0"
    max_participants: int | None = 1
    completion_mode: str = "independent"
    max_total_budget: str | None = None
    require_join_approval: bool = False
    auto_approve: bool = False
    allow_repeat_by_same: bool = False
    use_escrow: bool = False
    active_participants_count: int = 0
    completed_count: int = 0
    group_id: str | None = None
    created_at: str = ""
    deadline: str | None = None
    metadata: dict | None = None


class TaskCreateRequest(BaseModel):
    """Request to create a task.

    Three-layer design:
    - Layer 1 (required): title, description, deadline_hours, reward
    - Layer 2 (common): max_participants, auto_approve, task_type, required_tags, reward_currency
    - Layer 3 (advanced): require_join_approval, allow_repeat_by_same, max_total_budget
    - Escrow: use_escrow
    - Collaboration: group_id
    - Extension: metadata
    """

    # ── Layer 1: Required ────────────────────────────────
    title: str = Field(..., min_length=3, max_length=200)
    description: str = Field(..., min_length=10)
    deadline_hours: int = Field(..., ge=1, le=2160, description="Deadline in hours (1–2160)")
    reward: str = Field(..., description="Reward per completion, e.g. '50' or '0'")

    # ── Layer 2: Common options ───────────────────────────
    max_participants: int | None = Field(default=1, description="1=single, N=multi, None=unlimited")
    completion_mode: str = Field(
        default="independent",
        description="independent | competitive | collaborative",
    )
    auto_approve: bool = Field(default=False)
    task_type: str = Field(default="general")
    required_tags: list[str] = Field(default_factory=list)
    reward_currency: str = Field(default="ap_points")

    # ── Layer 3: Advanced ─────────────────────────────────
    require_join_approval: bool = Field(default=False)
    allow_repeat_by_same: bool = Field(default=False)
    max_total_budget: str | None = Field(default=None)

    # ── Escrow ────────────────────────────────────────────
    use_escrow: bool = Field(default=False)

    # ── Collaboration ─────────────────────────────────────
    group_id: str | None = Field(default=None, description="Link subtasks into a group")

    # ── Extension ─────────────────────────────────────────
    metadata: dict = Field(default_factory=dict)


class TaskAcceptRequest(BaseModel):
    """Request to accept/join a task."""

    message: str = Field(default="", description="Optional message to creator")


class TaskAcceptResponse(BaseModel):
    """Response for accept/join — includes participation_id for multi-participant tasks."""

    task: TaskInfo
    participation_id: str | None = None


class TaskSubmitRequest(BaseModel):
    """Request to submit task result."""

    submission: str = Field(..., min_length=5, description="Task result/deliverable")
    artifacts: list[dict] = Field(default_factory=list)
    participation_id: str | None = Field(None, description="Required for multi-participant tasks")


class TaskReviewRequest(BaseModel):
    """Request to approve or reject a submission."""

    approved: bool = Field(..., description="True to approve, False to reject")
    notes: str = Field(default="", description="Review notes")
    participation_id: str | None = Field(None, description="Participation ID (multi-participant)")
    agent_id: str | None = Field(None, description="Agent ID (alternative to participation_id)")


class ParticipationInfo(BaseModel):
    """Participation record for a task."""

    participation_id: str
    task_id: str
    participant_id: str
    participant_name: str
    participant_type: str = "agent"
    status: str
    joined_at: str
    submission: str | None = None
    submitted_at: str | None = None
    rejection_reason: str | None = None
    rejected_at: str | None = None
    review_notes: str | None = None
    reviewed_by: str | None = None
    completed_at: str | None = None
    cancelled_at: str | None = None


# ============================================
# Monitoring Models
# ============================================


class DashboardData(BaseModel):
    """Dashboard data"""

    agents: dict[str, int] = Field(default_factory=dict)
    messages: dict[str, int] = Field(default_factory=dict)
    subnets: dict[str, int] = Field(default_factory=dict)
