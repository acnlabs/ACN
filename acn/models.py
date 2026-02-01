"""
ACN Data Models

Pydantic models for ACN service
"""

from datetime import datetime

from pydantic import BaseModel, Field


class Skill(BaseModel):
    """Agent Skill"""

    id: str = Field(..., description="Skill identifier (e.g., 'task-planning')")
    name: str = Field(..., description="Human-readable skill name")
    description: str | None = Field(None, description="Skill description")


class AgentCard(BaseModel):
    """
    A2A-compliant Agent Card

    Follows the Agent-to-Agent (A2A) protocol specification
    """

    protocol_version: str = Field(default="0.3.0", alias="protocolVersion")
    name: str = Field(..., description="Agent name")
    description: str | None = Field(None, description="Agent description")
    url: str = Field(..., description="Agent A2A endpoint URL")
    skills: list[Skill] = Field(default_factory=list, description="Agent skills")
    authentication: dict | None = Field(None, description="Authentication config")

    class Config:
        populate_by_name = True


class AgentInfo(BaseModel):
    """Agent Information (ACN internal model)"""

    agent_id: str = Field(..., description="Unique agent identifier (UUID)")
    owner: str = Field(..., description="Agent owner (system/user-{id}/provider-{id})")
    name: str = Field(..., description="Agent name")
    description: str | None = Field(None, description="Agent description")
    endpoint: str = Field(..., description="Agent A2A endpoint URL")
    skills: list[str] = Field(default_factory=list, description="Agent skill IDs")
    status: str = Field(default="online", description="Agent status (online/offline/busy)")
    # 支持多子网归属
    subnet_ids: list[str] = Field(
        default_factory=lambda: ["public"],
        description="Subnets the agent belongs to (can be multiple)",
    )
    agent_card: AgentCard | None = Field(None, description="A2A Agent Card")
    metadata: dict = Field(default_factory=dict, description="Additional metadata")
    registered_at: datetime = Field(default_factory=datetime.now)
    last_heartbeat: datetime | None = Field(None)

    # Payment capability (AP2 Protocol integration)
    wallet_address: str | None = Field(None, description="Wallet address for crypto payments (AP2)")
    accepts_payment: bool = Field(default=False, description="Whether this agent accepts payments")
    payment_methods: list[str] = Field(
        default_factory=list,
        description="Accepted payment methods (e.g., 'usdc', 'eth', 'credit_card')",
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

    owner: str = Field(..., description="Agent owner (system/user-{id}/provider-{id})")
    name: str = Field(..., description="Agent name")
    endpoint: str = Field(..., description="Agent A2A endpoint URL")
    skills: list[str] = Field(default_factory=list, description="Agent skill IDs")
    agent_card: dict | None = Field(
        None, description="Optional Agent Card (auto-generated if not provided)"
    )
    # 支持多子网归属
    subnet_ids: list[str] | None = Field(
        None, description="Subnets to join (default: ['public']). Can be multiple."
    )
    # 向后兼容：单子网参数
    subnet_id: str | None = Field(
        None, description="[Deprecated] Single subnet to join. Use subnet_ids instead."
    )

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

    skills: list[str] | None = Field(None, description="Required skills")
    status: str = Field(default="online", description="Agent status filter")


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

    class Config:
        populate_by_name = True


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

    created_at: datetime = Field(default_factory=datetime.now)
    metadata: dict = Field(default_factory=dict, description="Additional metadata")


class SubnetCreateRequest(BaseModel):
    """
    Request to create a subnet

    Security options:
    1. No security_schemes = Public subnet (anyone can join)
    2. Bearer token = Simple token auth
    3. API Key = Key-based auth
    4. OpenID Connect = Enterprise OAuth/SSO

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

        # Enterprise OAuth
        {
            "subnet_id": "enterprise",
            "name": "Enterprise",
            "security_schemes": {
                "oauth": {
                    "type": "openIdConnect",
                    "openIdConnectUrl": "https://auth.company.com/.well-known/openid"
                }
            }
        }
    """

    subnet_id: str = Field(..., description="Unique subnet identifier")
    name: str = Field(..., description="Subnet name")
    description: str | None = Field(None, description="Subnet description")
    security_schemes: dict[str, dict] | None = Field(
        None, description="Security schemes (A2A format). None = public subnet"
    )
    default_security: list[str] | None = Field(
        None, description="Required security schemes. None = use first available"
    )
    metadata: dict = Field(default_factory=dict, description="Additional metadata")


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
    skills: list[str] = Field(default_factory=list, description="Agent skills (e.g., ['coding', 'review'])")
    mode: str = Field(default="pull", description="Communication mode: 'pull' (polling) or 'push' (A2A endpoint)")
    endpoint: str | None = Field(None, description="A2A endpoint URL (required for push mode)")
    source: str | None = Field(None, description="Where the agent came from (e.g., 'moltbook', 'openclaw')")
    referrer: str | None = Field(None, description="Referrer agent ID (for invitation tracking)")


class ExternalAgentJoinResponse(BaseModel):
    """Response after an external agent joins ACN"""
    
    agent_id: str = Field(..., description="Assigned agent ID (format: ext-{uuid})")
    api_key: str = Field(..., description="API key for authentication - SAVE THIS!")
    status: str = Field(default="active", description="Agent status")
    message: str = Field(default="Welcome to ACN!", description="Welcome message")
    
    # Helpful info
    tasks_endpoint: str = Field(..., description="Endpoint to pull tasks from")
    heartbeat_endpoint: str = Field(..., description="Endpoint for heartbeat")
    docs_url: str = Field(default="https://acn.agenticplanet.space/skill.md", description="Documentation URL")


class ExternalAgentTask(BaseModel):
    """A task for an external agent to execute"""
    
    task_id: str = Field(..., description="Task ID")
    prompt: str = Field(..., description="Task description/prompt")
    context: dict = Field(default_factory=dict, description="Additional context")
    priority: str = Field(default="normal", description="Task priority: low, normal, high")
    created_at: datetime = Field(default_factory=datetime.now)
    deadline: datetime | None = Field(None, description="Optional deadline")


class ExternalAgentTasksResponse(BaseModel):
    """Response containing tasks for an external agent"""
    
    pending: list[ExternalAgentTask] = Field(default_factory=list, description="Tasks waiting to be executed")
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
    last_seen: datetime = Field(default_factory=datetime.now)
