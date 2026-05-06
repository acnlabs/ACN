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
    """Broadcast delivery strategy — aligned with ACN server v0.5+."""

    PARALLEL = "parallel"
    SEQUENTIAL = "sequential"
    BEST_EFFORT = "best_effort"


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
    """Platform-managed agent registration (POST /agents/register, requires Auth0).

    For autonomous self-registration without Auth0, see ``AgentJoinRequest``
    and ``ACNClient.join_acn()``.
    """

    owner: str = Field(..., description="Agent owner (system/user-{id}/provider-{id})")
    name: str = Field(..., description="Agent name")
    tags: list[str] = Field(default_factory=list, description="Capability tags for discoverability")
    endpoint: str | None = Field(None, description="[Deprecated] Use a2a_endpoint instead")
    a2a_endpoint: str | None = Field(None, description="Direct A2A JSON-RPC endpoint URL")
    agent_card_url: str | None = Field(
        None, description="A2A Agent Card discovery URL (used when a2a_endpoint is omitted)"
    )
    agent_card: dict[str, Any] | None = Field(
        None, description="Optional A2A Agent Card dict (auto-generated if omitted)"
    )
    subnet_ids: list[str] | None = Field(None, description="Subnets to join (default: ['public'])")
    communication_policy: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Inbound message policy, e.g. {'mode': 'manifest'} or {'mode': 'open'}. "
            "Defaults to 'manifest' for new agents."
        ),
    )

    class Config:
        populate_by_name = True


class AgentJoinRequest(BaseModel):
    """Autonomous agent self-registration (POST /agents/join, no Auth0 required).

    Server-side constraint: at least one of ``a2a_endpoint``, ``endpoint``, or
    ``agent_card_url`` must be provided — the server returns 422 otherwise.

    Returns an ``AgentJoinResponse`` on success (includes ``api_key``).
    """

    name: str = Field(..., min_length=2, max_length=100, description="Agent name")
    description: str = Field(..., min_length=10, max_length=500, description="What this agent does")
    tags: list[str] = Field(
        default_factory=list, description="Capability tags (e.g. ['coding', 'search'])"
    )
    endpoint: str | None = Field(None, description="[Deprecated] Use a2a_endpoint instead")
    a2a_endpoint: str | None = Field(
        None, description="Direct A2A JSON-RPC endpoint URL (required if agent_card_url omitted)"
    )
    agent_card_url: str | None = Field(
        None,
        description=(
            "A2A Agent Card discovery URL "
            "(used to extract endpoint if a2a_endpoint omitted)"
        ),
    )
    agent_card: dict[str, Any] | None = Field(None, description="A2A Agent Card (protocol v0.3.0)")
    referrer_id: str | None = Field(None, description="Referrer agent ID")
    communication_policy: dict[str, Any] | None = Field(
        default={"mode": "manifest"},
        description=(
            "Inbound message policy. Defaults to manifest mode as of v0.5+. "
            "Pass {'mode': 'open'} for the legacy inline-delivery behaviour."
        ),
    )


class AgentJoinResponse(BaseModel):
    """Successful response from POST /agents/join.

    Store ``api_key`` securely — it authenticates all subsequent API calls.
    """

    agent_id: str
    api_key: str
    status: str
    claim_status: str
    verification_code: str
    claim_url: str
    referral_url: str
    tasks_endpoint: str
    heartbeat_endpoint: str
    agent_card_url: str


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

    ``attention_fee``, ``content_url``, and ``message_type`` only take
    effect when the recipient is in manifest mode; they are silently
    ignored or raise 400 otherwise (see server docs).
    """

    from_agent: str
    target_agent: str
    message: dict[str, Any]
    priority: str = "normal"
    attention_fee: AttentionFee | None = None
    content_url: str | None = None
    content_hash: str | None = None
    # Phase 3: optional category tag for manifest filtering.
    # Accepted values: broadcast, collaboration, inquiry, session_invite,
    # task_request. Absent → entry has no type tag (not filterable by type).
    message_type: str | None = None


class BroadcastRequest(BaseModel):
    """Broadcast request — aligned with ACN server v0.5+."""

    from_agent: str
    message: dict[str, Any]
    strategy: str = "parallel"
    target_subnet: str | None = None
    target_tags: list[str] | None = None


class ManifestEntry(BaseModel):
    """A single manifest queue entry as returned by GET /manifest/{agent_id}.

    Field names mirror the server JSON keys exactly.  To get the full
    payload (for ACN-hosted content) or the self-hosted pointer, call
    ``ACNClient.fetch_manifest_content(mid)`` after listing.
    """

    mid: str
    sender_id: str
    summary: str
    ts: int
    content_size: int
    extra: dict[str, Any] = Field(default_factory=dict)
    acked_at: int | None = None
    # Phase 3: ACN message category tag set by the sender.  Absent (None)
    # for entries written via Path 1 without a message_type.
    message_type: str | None = None


class ManifestContentResponse(BaseModel):
    """Response from GET /communication/content/{mid}.

    ACN-hosted content is returned in chunks:
    * ``has_more=False`` → entire content in ``content_chunk`` (or first-and-only page).
    * ``has_more=True``  → more pages available; pass ``next_cursor`` to the next call.

    Self-hosted content (``self_hosted=True``) is always returned in a single
    response with ``content_url``; cursor/chunk fields are absent.
    """

    mid: str
    owner_id: str
    self_hosted: bool = False
    content_url: str | None = None
    content_hash: str | None = None
    # ACN-hosted path fields (cursor-based pagination).
    has_more: bool = False
    content_chunk: str | None = None
    next_cursor: str | None = None


# ============================================
# Phase 3: Manifest Send (Path 2)
# ============================================


class ManifestSendRequest(BaseModel):
    """Path 2 notify-only send (POST /communication/manifest/send).

    Unlike ``SendMessageRequest``, this endpoint:
    * Requires ``message_type`` (mandatory).
    * Accepts only a ``summary`` — no full message body stored on ACN.
    * Only works when the recipient is in ``manifest`` or ``allowlist`` mode.
    """

    from_agent: str
    target_agent: str
    message_type: str
    summary: str
    ttl_hours: int | None = None
    attention_fee: AttentionFee | None = None
    content_url: str | None = None
    content_hash: str | None = None


# ============================================
# Phase 3: Communication Profile
# ============================================


class CommunicationProfile(BaseModel):
    """Public read-only summary of an agent's communication policy.

    Returned by GET /agents/{agent_id}/communication_profile (no auth required).
    Lets a prospective sender decide whether to attach an attention_fee or
    whether their message will be accepted before sending.
    """

    agent_id: str
    mode: str  # open | manifest | allowlist | closed
    attention_fee_required: bool


# ============================================
# Phase 3: Session Layer
# ============================================


class SessionEntry(BaseModel):
    """A real-time session negotiation record.

    Sessions are ephemeral (TTL 1–30 min) and Redis-only. Use the session
    layer to agree on a bilateral channel before committing resources.
    """

    session_id: str
    inviter_id: str
    invitee_id: str
    status: str  # pending | accepted | rejected | closed
    created_at: int  # ms
    expires_at: int  # ms
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionInviteRequest(BaseModel):
    """Body for POST /sessions/invite/{target_agent_id}."""

    ttl_seconds: int | None = None  # 60–1800; default 300
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
