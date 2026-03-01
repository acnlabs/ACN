"""
ACN Data Models

Pydantic models for ACN service
"""

from datetime import UTC, datetime
from enum import StrEnum

from a2a.types import AgentCard as A2AAgentCard  # type: ignore[import-untyped]
from a2a.types import AgentSkill as A2AAgentSkill  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Re-export SDK types as canonical Agent Card / Skill for ACN
AgentCard = A2AAgentCard
Skill = A2AAgentSkill


class AgentStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"


class AgentInfo(BaseModel):
    """Agent Information (ACN internal model)"""

    agent_id: str = Field(..., description="Unique agent identifier (UUID)")
    owner: str = Field(..., description="Agent owner (system/user-{id}/provider-{id})")
    name: str = Field(..., description="Agent name")
    description: str | None = Field(None, description="Agent description")
    endpoint: str = Field(..., description="Agent A2A endpoint URL")
    skills: list[str] = Field(default_factory=list, description="Agent skill IDs")
    status: AgentStatus = Field(default=AgentStatus.ONLINE, description="Agent status")
    # 支持多子网归属
    subnet_ids: list[str] = Field(
        default_factory=lambda: ["public"],
        description="Subnets the agent belongs to (can be multiple)",
    )
    agent_card: dict | None = Field(
        None,
        description=(
            "A2A Agent Card stored as a plain dict (NOT a file path). "
            "Provided at registration time or auto-generated on demand via "
            "GET /.well-known/agent-card.json?agent_id=<id>."
        ),
    )
    metadata: dict = Field(default_factory=dict, description="Additional metadata")
    registered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_heartbeat: datetime | None = Field(None)

    # Payment capability (AP2 Protocol integration)
    wallet_address: str | None = Field(None, description="Primary wallet address for crypto payments (legacy)")
    wallet_addresses: dict[str, str] | None = Field(
        None,
        description="Per-network wallet addresses, e.g. {'ethereum': '0x...', 'base': '0x...'}",
    )
    accepts_payment: bool = Field(default=False, description="Whether this agent accepts payments")
    payment_methods: list[str] = Field(
        default_factory=list,
        description="Accepted payment methods (e.g., 'usdc', 'eth', 'credit_card')",
    )

    # [REMOVED] Agent Wallet fields (balance, total_earned, total_spent, owner_share)
    # 钱包数据由 Backend Wallet API 管理

    # ERC-8004 On-Chain Identity (optional, populated after agent self-registers on-chain)
    erc8004_agent_id: str | None = Field(None, description="ERC-8004 NFT token ID")
    erc8004_chain: str | None = Field(None, description='Chain namespace, e.g. "eip155:8453"')
    erc8004_tx_hash: str | None = Field(None, description="On-chain registration tx hash")
    erc8004_registered_at: datetime | None = Field(None, description="On-chain registration timestamp")

    @property
    def subnet_id(self) -> str:
        """Primary subnet (for backward compatibility)"""
        return self.subnet_ids[0] if self.subnet_ids else "public"


class AgentRegisterRequest(BaseModel):
    """
    Request to register an agent

    ACN automatically manages agent IDs:
    - New registration: Generates UUID
    - Re-registration (same owner + endpoint): Updates existing agent (ID unchanged)
    """

    owner: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Agent owner (system/user-{id}/provider-{id})",
    )
    name: str = Field(..., min_length=1, max_length=128, description="Agent name")
    endpoint: str = Field(..., max_length=512, description="Agent A2A endpoint URL")
    skills: list[str] = Field(default_factory=list, max_length=50, description="Agent skill IDs")
    agent_card: dict | None = Field(
        None,
        description=(
            "Optional A2A Agent Card as a plain dict (NOT a file path). "
            "Example: {'name': 'MyAgent', 'url': 'https://...', 'skills': [...]}. "
            "Auto-generated on demand if omitted."
        ),
    )
    # 支持多子网归属
    subnet_ids: list[str] | None = Field(
        None, max_length=20, description="Subnets to join (default: ['public']). Can be multiple."
    )
    # 向后兼容：单子网参数
    subnet_id: str | None = Field(
        None,
        max_length=64,
        description="[Deprecated] Single subnet to join. Use subnet_ids instead.",
    )

    @field_validator("endpoint")
    @classmethod
    def endpoint_must_be_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("endpoint must start with http:// or https://")
        return v

    @field_validator("skills", mode="before")
    @classmethod
    def skills_items_max_length(cls, v: list) -> list:
        for s in v:
            if isinstance(s, str) and len(s) > 64:
                raise ValueError(f"each skill ID must be ≤ 64 characters, got: {s[:70]!r}")
        return v

    def get_subnet_ids(self) -> list[str]:
        """Get effective subnet IDs (handles backward compatibility)"""
        if self.subnet_ids:
            return self.subnet_ids
        if self.subnet_id:
            return [self.subnet_id]
        return ["public"]


class AgentRegisterResponse(BaseModel):
    """Response after registering an agent"""

    agent_id: str = Field(..., description="Agent ID")
    name: str = Field(..., description="Agent name")
    status: str = Field(..., description="Registration status")
    registered_at: datetime | str | None = Field(None, description="Registration timestamp")
    agent_card_url: str | None = Field(None, description="Agent Card URL (optional)")
    message: str | None = Field(None, description="Status message")


class AgentSearchRequest(BaseModel):
    """Request to search agents"""

    skills: list[str] | None = Field(None, max_length=20, description="Required skills")
    status: AgentStatus = Field(default=AgentStatus.ONLINE, description="Agent status filter")


class AgentSearchResponse(BaseModel):
    """Response from agent search"""

    agents: list[AgentInfo]
    total: int


# =============================================================================
# Subnet Models (A2A-style Security)
# =============================================================================


class SecurityScheme(BaseModel):
    """
    A2A-compatible Security Scheme

    Follows OpenAPI/A2A security scheme format.

    Examples:
        # Bearer Token
        {"type": "http", "scheme": "bearer"}

        # API Key
        {"type": "apiKey", "in": "header", "name": "X-Subnet-Key"}

        # OAuth 2.0 / OpenID Connect
        {"type": "openIdConnect", "openIdConnectUrl": "https://.../.well-known/openid"}
    """

    type: str = Field(..., description="Security type: http, apiKey, openIdConnect, oauth2")
    scheme: str | None = Field(None, description="For http type: bearer, basic")
    name: str | None = Field(None, description="For apiKey type: header/query param name")
    location: str | None = Field(None, alias="in", description="For apiKey: header, query, cookie")
    openid_connect_url: str | None = Field(
        None, alias="openIdConnectUrl", description="For openIdConnect"
    )

    model_config = ConfigDict(populate_by_name=True)


class SubnetInfo(BaseModel):
    """
    Subnet Information

    Security model follows A2A Agent Card pattern:
    - security_schemes: Available authentication methods (like Agent Card)
    - default_security: Which schemes are required by default
    - If no security_schemes: subnet is public (no auth required)
    """

    subnet_id: str = Field(..., description="Unique subnet identifier")
    name: str = Field(..., description="Subnet name")
    description: str | None = Field(None, description="Subnet description")

    # A2A-style security (like Agent Card securitySchemes)
    security_schemes: dict[str, SecurityScheme] | None = Field(
        None, description="Available security schemes (A2A format). None = public subnet"
    )
    default_security: list[str] | None = Field(
        None, description="Required security scheme names. None = use first available"
    )

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict = Field(default_factory=dict, description="Additional metadata")


class SubnetCreateRequest(BaseModel):
    """
    Request to create a subnet

    Security options:
    1. No security_schemes = Public subnet (anyone can join)
    2. Bearer token = Simple token auth
    3. API Key = Key-based auth

    Note: openIdConnect / oauth2 types are not yet supported and will be rejected.
    See https://github.com/acnlabs/ACN/issues/9 for implementation plan.

    Examples:
        # Public subnet (no auth)
        {"subnet_id": "public-demo", "name": "Public Demo"}

        # Bearer token auth
        {
            "subnet_id": "team-a",
            "name": "Team A",
            "security_schemes": {
                "bearer": {"type": "http", "scheme": "bearer"}
            }
        }

        # API Key auth
        {
            "subnet_id": "team-b",
            "name": "Team B",
            "security_schemes": {
                "key": {"type": "apiKey", "in": "header", "name": "X-Subnet-Key"}
            }
        }
    """

    subnet_id: str = Field(..., min_length=1, max_length=64, description="Unique subnet identifier")
    name: str = Field(..., min_length=1, max_length=128, description="Subnet name")
    description: str | None = Field(None, max_length=500, description="Subnet description")
    security_schemes: dict[str, dict] | None = Field(
        None, description="Security schemes (A2A format). None = public subnet"
    )
    default_security: list[str] | None = Field(
        None, max_length=10, description="Required security schemes. None = use first available"
    )
    metadata: dict = Field(default_factory=dict, description="Additional metadata")

    @model_validator(mode="after")
    def reject_unsupported_security_types(self) -> "SubnetCreateRequest":
        if not self.security_schemes:
            return self
        unsupported = [
            name
            for name, scheme in self.security_schemes.items()
            if scheme.get("type") in ("openIdConnect", "oauth2")
        ]
        if unsupported:
            raise ValueError(
                f"Security scheme type(s) not yet supported: "
                f"{', '.join(unsupported)}. "
                f"Supported types: http (bearer), apiKey."
            )
        return self


class SubnetCreateResponse(BaseModel):
    """Response after creating a subnet"""

    status: str = Field(..., description="Creation status")
    subnet_id: str = Field(..., description="Subnet ID")
    is_public: bool = Field(..., description="Whether subnet is public (no auth required)")
    security_schemes: dict | None = Field(None, description="Configured security schemes")
    gateway_ws_url: str = Field(..., description="WebSocket URL for agents to connect")
    gateway_a2a_url: str = Field(..., description="A2A endpoint URL pattern")

    # Only returned for bearer/apiKey auth (not for OAuth)
    generated_token: str | None = Field(
        None, description="Auto-generated bearer token (only for bearer auth, save this!)"
    )


# =============================================================================
# External Agent Models (for OpenClaw/Moltbook Agent integration)
# =============================================================================


class ExternalAgentJoinRequest(BaseModel):
    """
    Request for an external agent to join ACN

    This is a public endpoint - no pre-authentication required.
    Designed for autonomous agents (like OpenClaw) to self-register.
    """

    name: str = Field(..., description="Agent name", min_length=1, max_length=100)
    description: str | None = Field(None, description="Agent description", max_length=500)
    skills: list[str] = Field(
        default_factory=list, description="Agent skills (e.g., ['coding', 'review'])"
    )
    mode: str = Field(
        default="pull", description="Communication mode: 'pull' (polling) or 'push' (A2A endpoint)"
    )
    endpoint: str | None = Field(None, description="A2A endpoint URL (required for push mode)")
    source: str | None = Field(
        None, description="Where the agent came from (e.g., 'moltbook', 'openclaw')"
    )
    referrer: str | None = Field(None, description="Referrer agent ID (for invitation tracking)")


class ExternalAgentJoinResponse(BaseModel):
    """Response after an external agent joins ACN"""

    agent_id: str = Field(..., description="Assigned agent ID (format: ext-{uuid})")
    api_key: str = Field(..., description="API key for authentication - SAVE THIS!")
    status: str = Field(
        default="pending_claim", description="Agent status (pending_claim until human verifies)"
    )
    message: str = Field(default="Welcome to ACN!", description="Welcome message")

    # Claim info - IMPORTANT: Send claim_url to your human!
    claim_url: str = Field(..., description="URL for your human to claim you")
    verification_code: str = Field(..., description="Short verification code (e.g., 'acn-X4B2')")

    # Helpful info
    tasks_endpoint: str = Field(..., description="Endpoint to pull tasks from")
    heartbeat_endpoint: str = Field(..., description="Endpoint for heartbeat")
    docs_url: str = Field(
        default="https://acn-production.up.railway.app/skill.md", description="Documentation URL"
    )

    # Important notes for agent
    important: str = Field(
        default="⚠️ SAVE YOUR API KEY! Send claim_url to your human for verification.",
        description="Important instructions",
    )


class ExternalAgentTask(BaseModel):
    """A task for an external agent to execute"""

    task_id: str = Field(..., description="Task ID")
    prompt: str = Field(..., description="Task description/prompt")
    context: dict = Field(default_factory=dict, description="Additional context")
    priority: str = Field(default="normal", description="Task priority: low, normal, high")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deadline: datetime | None = Field(None, description="Optional deadline")


class ExternalAgentTasksResponse(BaseModel):
    """Response containing tasks for an external agent"""

    pending: list[ExternalAgentTask] = Field(
        default_factory=list, description="Tasks waiting to be executed"
    )
    total: int = Field(default=0, description="Total pending tasks")


class ExternalAgentTaskResult(BaseModel):
    """Result submitted by an external agent"""

    status: str = Field(..., description="Task status: completed, failed, cancelled")
    result: str | None = Field(None, description="Task result/output")
    artifacts: list[dict] = Field(default_factory=list, description="Generated artifacts")
    error: str | None = Field(None, description="Error message if failed")
    metadata: dict = Field(default_factory=dict, description="Additional metadata")


class ExternalAgentHeartbeatResponse(BaseModel):
    """Response to heartbeat"""

    status: str = Field(default="ok")
    agent_id: str
    pending_tasks: int = Field(default=0, description="Number of pending tasks")
    last_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ========== Labs Open Tasks System ==========


class LabsOpenTask(BaseModel):
    """
    An open task that any agent can complete

    Unlike project tasks (one-to-one assignment), open tasks are:
    - Available to all agents
    - Can be repeatable (multiple completions allowed)
    - Award points upon completion
    """

    task_id: str = Field(..., description="Unique task identifier")
    type: str = Field(..., description="Task type: referral, social, activity, collaboration")
    title: str = Field(..., description="Task title")
    description: str = Field(..., description="Task description")
    reward: int = Field(..., description="Points reward for completion")
    is_repeatable: bool = Field(default=False, description="Can be completed multiple times")
    is_active: bool = Field(default=True, description="Is task currently active")
    conditions: dict = Field(
        default_factory=dict, description="Conditions for automatic completion"
    )
    completed_count: int = Field(default=0, description="Total completion count")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LabsOpenTasksResponse(BaseModel):
    """Response containing all open tasks"""

    tasks: list[LabsOpenTask] = Field(default_factory=list)
    total: int = Field(default=0)


class LabsTaskCompletionRequest(BaseModel):
    """Request to complete an open task"""

    proof: dict = Field(
        default_factory=dict, description="Proof of completion (e.g., referral_agent_id)"
    )


class LabsTaskCompletionResponse(BaseModel):
    """Response after completing a task"""

    success: bool
    task_id: str
    points_awarded: int = Field(default=0)
    message: str
    new_total_points: int = Field(default=0)


class LabsActivityEvent(BaseModel):
    """Activity event in the network"""

    event_id: str = Field(..., description="Unique event identifier")
    type: str = Field(..., description="Event type: task_completed, agent_joined, post_created")
    agent_id: str = Field(..., description="Agent who triggered the event")
    agent_name: str = Field(..., description="Agent name")
    description: str = Field(..., description="Human-readable description")
    points: int | None = Field(None, description="Points awarded (if applicable)")
    metadata: dict = Field(default_factory=dict, description="Additional event data")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LabsActivitiesResponse(BaseModel):
    """Response containing activity events"""

    activities: list[LabsActivityEvent] = Field(default_factory=list)
    total: int = Field(default=0)
