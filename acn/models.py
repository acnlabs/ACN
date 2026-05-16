"""
ACN Data Models

Pydantic models for ACN service
"""

from datetime import UTC, datetime
from enum import StrEnum

from a2a.compat.v0_3.types import AgentCard as A2AAgentCard  # type: ignore[import-untyped]
from a2a.compat.v0_3.types import AgentSkill as A2AAgentSkill  # type: ignore[import-untyped]
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
    endpoint: str = Field(..., description="Agent A2A JSON-RPC endpoint URL")
    a2a_endpoint: str | None = Field(
        None,
        description=(
            "Explicit A2A JSON-RPC delivery URL. Mirrors endpoint during the "
            "field-name transition."
        ),
    )
    agent_card_url: str | None = Field(
        None,
        description="A2A Agent Card discovery URL, if provided by the registrant.",
    )
    tags: list[str] = Field(default_factory=list, description="Agent capability tags (e.g. ['coding', 'search'])")
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
    # Gateway-level access control policy. INTERNAL field — read by
    # MessageRouter / SubnetManager before delivery to enforce
    # ``communication_policy`` (Phase 1: open / closed). ``exclude=True``
    # keeps it out of every Pydantic-driven API response (FastAPI's
    # ``response_model`` honours field-level ``exclude``), so we can
    # store the policy inline on the cached agent record without leaking
    # ``reject_reason`` / future allowlist entries to public callers.
    # See docs/features/acn-communication-economic-model.md
    # "Phase 1 网关执行点决策".
    communication_policy: dict | None = Field(
        default=None,
        exclude=True,
        description=(
            "Internal-only: gateway communication policy. Excluded from "
            "API responses. ``None`` is treated as ``{'mode': 'open'}``."
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

    # Follow graph counts (see docs/features/acn-follow-proposal.md).
    # Defaults to 0 so existing clients stay happy when the follow
    # subsystem is unwired or the count lookup is intentionally skipped
    # (e.g. on hot list endpoints to avoid extra Redis round-trips).
    followers_count: int = Field(
        default=0,
        ge=0,
        description="Number of agents that follow this agent.",
    )
    follows_count: int = Field(
        default=0,
        ge=0,
        description="Number of agents this agent follows.",
    )

    # [REMOVED] Agent Wallet fields (balance, total_earned, total_spent, owner_share)
    # 钱包数据由 Backend Wallet API 管理

    # ERC-8004 On-Chain Identity (optional, populated after agent self-registers on-chain)
    erc8004_agent_id: str | None = Field(None, description="ERC-8004 NFT token ID")
    erc8004_chain: str | None = Field(None, description='Chain namespace, e.g. "eip155:8453"')
    erc8004_tx_hash: str | None = Field(None, description="On-chain registration tx hash")
    erc8004_registered_at: datetime | None = Field(None, description="On-chain registration timestamp")

    # SOCIAL.md pointer (https://agentsocial.one). URL only — body lives at
    # the URL and consumers fetch on demand. ACN deliberately does NOT cache
    # the body so each agent owner remains the single source of truth for
    # their own contact terms (see consumption-model in the spec).
    social_card_url: str | None = Field(
        None,
        max_length=2048,
        description=(
            "URL to this agent's SOCIAL.md (https://agentsocial.one spec). "
            "Body is fetched on demand by consumers; ACN stores only the URL."
        ),
    )

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
    endpoint: str | None = Field(
        None,
        max_length=512,
        description="[Deprecated] Direct A2A JSON-RPC endpoint URL. Use a2a_endpoint.",
    )
    a2a_endpoint: str | None = Field(
        None,
        max_length=512,
        description="Direct A2A JSON-RPC endpoint URL used for message delivery.",
    )
    agent_card_url: str | None = Field(
        None,
        max_length=512,
        description=(
            "A2A Agent Card discovery URL. If a2a_endpoint is omitted, ACN "
            "fetches this card and extracts the JSON-RPC endpoint."
        ),
    )
    tags: list[str] = Field(default_factory=list, max_length=50, description="Agent capability tags")
    agent_card: dict | None = Field(
        None,
        description=(
            "Optional A2A Agent Card as a plain dict (NOT a file path). "
            "Example: {'name': 'MyAgent', 'url': 'https://...', 'tags': [...]}. "
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
    # Phase 1 L410: optional inbound policy. ``None`` keeps the
    # legacy default (``open`` via ``Agent.__post_init__``).
    communication_policy: dict | None = Field(
        default=None,
        description=(
            "Inbound message policy. Phase 1 accepts "
            "{'mode': 'open' | 'closed', 'reject_reason'?: str}. "
            "Default: open."
        ),
    )
    # Optional SOCIAL.md pointer — see https://agentsocial.one. The body
    # is fetched on demand by clients; ACN stores ONLY the URL.
    social_card_url: str | None = Field(
        default=None,
        max_length=2048,
        description=(
            "URL to this agent's SOCIAL.md (https://agentsocial.one spec). "
            "Must start with https:// (or http:// in dev). Body is never "
            "stored by ACN — clients fetch from the URL on demand."
        ),
    )

    @field_validator("social_card_url")
    @classmethod
    def validate_social_card_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        # RFC 3986: URI schemes are case-insensitive. Match the entity
        # layer (Agent.__post_init__) and SocialCardUrlPatchRequest.
        scheme = v.lower()
        if not (scheme.startswith("https://") or scheme.startswith("http://")):
            raise ValueError("social_card_url must start with https:// or http://")
        return v

    @field_validator("communication_policy")
    @classmethod
    def validate_communication_policy(cls, v):
        # Single source of truth for policy schema — see
        # ``acn.services.policy_service.validate_policy_dict``.
        from .services.policy_service import validate_policy_dict

        return validate_policy_dict(v)

    @field_validator("endpoint", "a2a_endpoint", "agent_card_url")
    @classmethod
    def endpoint_must_be_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("URL must start with http:// or https://")
        # SSRF guard: forbid IP-literal endpoints in private/reserved ranges
        # at registration time. Hostname → DNS rebinding is checked at
        # request-dispatch time in _proxy_to_agent / MessageRouter.
        from .config import get_settings as _get_settings
        from .security import SSRFViolation, validate_endpoint_url

        try:
            validate_endpoint_url(v, allow_loopback=_get_settings().dev_mode)
        except SSRFViolation as e:
            raise ValueError(str(e)) from e
        return v

    @model_validator(mode="after")
    def require_delivery_or_discovery_url(self):
        if not (self.a2a_endpoint or self.endpoint or self.agent_card_url):
            raise ValueError("a2a_endpoint, endpoint, or agent_card_url is required")
        return self

    def get_direct_a2a_endpoint(self) -> str | None:
        """Return the explicit direct delivery URL, if provided."""
        return self.a2a_endpoint or self.endpoint

    @field_validator("tags", mode="before")
    @classmethod
    def tags_items_max_length(cls, v: list) -> list:
        for s in v:
            if isinstance(s, str) and len(s) > 64:
                raise ValueError(f"each tag must be ≤ 64 characters, got: {s[:70]!r}")
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

    tags: list[str] | None = Field(None, max_length=20, description="Required capability tags")
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

    # Org Harness — the external webhook target registered by the subnet owner.
    # Exposed so prospective joiners can discover which orchestration system
    # governs this subnet before deciding to join.  ``harness_secret`` is
    # intentionally omitted (write-only, never returned).
    harness_url: str | None = Field(
        None, description="Registered Org Harness webhook URL, if any"
    )
    harness_registered: bool = Field(
        False, description="Whether an Org Harness is registered for this subnet"
    )


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

    subnet_id: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        description="Unique subnet identifier. Optional — ACN auto-generates `subnet-{slug}-{rand6}` when omitted.",
    )
    name: str = Field(..., min_length=1, max_length=128, description="Subnet name")
    description: str | None = Field(None, max_length=500, description="Subnet description")
    is_private: bool = Field(False, description="Whether this is a private subnet")
    security_schemes: dict[str, dict] | None = Field(
        None, description="Security schemes (A2A format). None = public subnet"
    )
    security_config: dict | None = Field(None, description="Alias for security_schemes (deprecated)")
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
    tags: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Agent capability tags (e.g., ['coding', 'review'])",
    )
    mode: str = Field(
        default="pull",
        max_length=16,
        description="Communication mode: 'pull' (polling) or 'push' (A2A endpoint)",
    )
    endpoint: str | None = Field(
        None, max_length=500, description="A2A endpoint URL (required for push mode)"
    )
    source: str | None = Field(
        None,
        max_length=64,
        description="Where the agent came from (e.g., 'moltbook', 'openclaw')",
    )
    referrer: str | None = Field(
        None, max_length=128, description="Referrer agent ID (for invitation tracking)"
    )


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
        default="https://api.acnlabs.dev/skill.md", description="Documentation URL"
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
