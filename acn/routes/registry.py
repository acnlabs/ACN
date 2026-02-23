"""Agent Registry API Routes

Clean Architecture implementation: Route → Service → Repository

Supports two registration modes:
1. Platform Registration (managed): POST /register - requires Auth0
2. Autonomous Join: POST /join - no auth, returns API key
3. Self-service: GET /me - agent gets own info via API key
"""

import structlog  # type: ignore[import-untyped]
from a2a.types import AgentCapabilities, AgentCard, AgentSkill  # type: ignore[import-untyped]
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth.middleware import require_permission
from ..config import get_settings
from ..core.exceptions import AgentNotFoundException
from ..models import AgentInfo, AgentRegisterRequest, AgentRegisterResponse, AgentSearchResponse
from ..services.rewards_client import RewardsClient
from .dependencies import (  # type: ignore[import-untyped]
    AgentApiKeyDep,
    AgentServiceDep,
    InternalTokenDep,
    SubnetManagerDep,
    limiter,
)

router = APIRouter(prefix="/api/v1/agents", tags=["registry"])
logger = structlog.get_logger()
settings = get_settings()


# ========== Request/Response Models ==========


class AgentJoinRequest(BaseModel):
    """Request for autonomous agent to join ACN"""

    name: str = Field(..., min_length=1, max_length=100, description="Agent name")
    description: str | None = Field(None, max_length=500, description="Agent description")
    skills: list[str] = Field(default_factory=list, description="Agent skills")
    endpoint: str | None = Field(None, description="A2A endpoint (optional for pull mode)")
    referrer_id: str | None = Field(None, description="Referrer agent ID")
    agent_card: dict | None = Field(None, description="A2A Agent Card (protocol v0.3.0)")


class AgentJoinResponse(BaseModel):
    """Response after agent joins ACN"""

    agent_id: str = Field(..., description="Assigned agent ID")
    api_key: str = Field(..., description="API key for authentication - SAVE THIS!")
    status: str = Field(default="active", description="Agent status")
    claim_status: str = Field(default="unclaimed", description="Claim status")
    verification_code: str = Field(..., description="Code for human verification")

    # Helpful endpoints
    claim_url: str = Field(..., description="URL for human to claim this agent")
    tasks_endpoint: str = Field(..., description="Endpoint to fetch tasks")
    heartbeat_endpoint: str = Field(..., description="Heartbeat endpoint")
    agent_card_url: str = Field(..., description="URL to retrieve the stored Agent Card")


class AgentClaimRequest(BaseModel):
    """Request to claim an agent"""

    verification_code: str | None = Field(None, description="Verification code")


class AgentClaimResponse(BaseModel):
    """Response after claiming an agent"""

    success: bool
    agent_id: str
    owner: str | None
    message: str


class AgentTransferRequest(BaseModel):
    """Request to transfer agent ownership"""

    new_owner: str = Field(..., description="New owner identifier")


class AgentTransferResponse(BaseModel):
    """Response after transferring agent"""

    success: bool
    agent_id: str
    previous_owner: str
    new_owner: str
    message: str


class AgentReleaseResponse(BaseModel):
    """Response after releasing agent ownership"""

    success: bool
    agent_id: str
    previous_owner: str
    message: str


class AgentMeResponse(BaseModel):
    """Response for /me endpoint - agent's own information"""

    agent_id: str
    name: str
    description: str | None = None
    skills: list[str] = []
    status: str
    claim_status: str
    owner: str | None = None
    # [REMOVED] balance, total_earned, owner_share - 由 Backend Wallet API 管理
    registered_at: str | None = None
    last_heartbeat: str | None = None
    # Helpful endpoints
    tasks_endpoint: str
    heartbeat_endpoint: str


# ============================================================================
# 🔧 DEV MODE: Register without Auth (for local development only)
# ============================================================================
@router.post("/dev/register", response_model=AgentRegisterResponse)
async def dev_register_agent(
    request: AgentRegisterRequest,
    agent_service: AgentServiceDep = None,
    subnet_manager: SubnetManagerDep = None,
):
    """DEV MODE: Register an Agent without Auth0 (local development only)

    ⚠️ WARNING: This endpoint should be disabled in production!
    """
    if not settings.dev_mode:
        raise HTTPException(
            status_code=403,
            detail="Dev mode registration is disabled. Use /register with Auth0 token.",
        )

    logger.warning(
        "DEV MODE: Registering agent without authentication", owner=request.owner, name=request.name
    )

    # Get subnet IDs
    subnet_ids = request.get_subnet_ids()

    # Validate subnets
    for subnet_id in subnet_ids:
        if subnet_id != "public" and not subnet_manager.subnet_exists(subnet_id):
            raise HTTPException(
                status_code=400,
                detail=f"Subnet not found: {subnet_id}",
            )

    try:
        # Use AgentService (Clean Architecture)
        agent = await agent_service.register_agent(
            owner=request.owner,
            name=request.name,
            endpoint=request.endpoint,
            skills=request.skills,
            subnet_ids=subnet_ids,
            agent_card=request.agent_card,
        )

        # Return response
        return AgentRegisterResponse(
            agent_id=agent.agent_id,
            name=agent.name,
            status=agent.status.value,
            registered_at=agent.registered_at,
            message=f"DEV MODE: Agent registered successfully (owner: {request.owner})",
        )

    except Exception as e:
        logger.error("Dev registration failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


def _agent_entity_to_info(agent) -> AgentInfo:
    """Convert Agent entity to AgentInfo model"""
    return AgentInfo(
        agent_id=agent.agent_id,
        owner=agent.owner or "unowned",  # Handle None owner
        name=agent.name,
        description=agent.description,
        endpoint=agent.endpoint or "",  # Handle None endpoint
        skills=agent.skills,
        status=agent.status.value,
        subnet_ids=agent.subnet_ids,
        agent_card=agent.agent_card,
        metadata={
            **agent.metadata,
            # Add claim info to metadata for API consumers
            "claim_status": agent.claim_status.value if agent.claim_status else None,
            "verification_code": agent.verification_code,
            "referrer_id": agent.referrer_id,
        },
        registered_at=agent.registered_at,
        last_heartbeat=agent.last_heartbeat,
        wallet_address=agent.wallet_address,
        accepts_payment=agent.accepts_payment,
        payment_methods=agent.payment_methods,
        # [REMOVED] Agent Wallet fields - 由 Backend 管理
    )


@router.post("/register", response_model=AgentRegisterResponse)
async def register_agent(
    request: AgentRegisterRequest,
    payload: dict = Depends(require_permission("acn:write")),
    agent_service: AgentServiceDep = None,
    subnet_manager: SubnetManagerDep = None,
):
    """Register an Agent (Idempotent) - Requires Auth0 Token

    Clean Architecture: Route → AgentService → Repository
    """
    token_owner: str = payload.get("sub", "")

    # Validate owner
    if request.owner != token_owner:
        permissions = payload.get("permissions", []) or payload.get("scope", "").split()
        if "acn:admin" not in permissions:
            raise HTTPException(
                status_code=403,
                detail=f"Cannot register agent for owner '{request.owner}'. Token owner is '{token_owner}'.",
            )

    # Get subnet IDs
    subnet_ids = request.get_subnet_ids()

    # Validate subnets
    for subnet_id in subnet_ids:
        if subnet_id != "public" and not subnet_manager.subnet_exists(subnet_id):
            raise HTTPException(
                status_code=400,
                detail=f"Subnet not found: {subnet_id}",
            )

    try:
        # Use AgentService (Clean Architecture)
        agent = await agent_service.register_agent(
            owner=request.owner,
            name=request.name,
            endpoint=request.endpoint,
            skills=request.skills,
            subnet_ids=subnet_ids,
            description=getattr(request, "description", None),
            metadata=getattr(request, "metadata", {}),
            agent_card=request.agent_card,
        )

        # Generate Agent Card URL
        base_url = settings.gateway_base_url or f"http://localhost:{settings.port}"
        agent_card_url = f"{base_url}/api/v1/agents/{agent.agent_id}/.well-known/agent-card.json"

        logger.info("agent_registered", agent_id=agent.agent_id, owner=agent.owner)

        return AgentRegisterResponse(
            status="registered",
            agent_id=agent.agent_id,
            name=agent.name,
            agent_card_url=agent_card_url,
        )
    except Exception as e:
        logger.error("agent_registration_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/me", response_model=AgentMeResponse)
async def get_my_agent(
    authorization: str = Header(..., description="Bearer API_KEY"),
    agent_service: AgentServiceDep = None,
):
    """
    Get current agent's own information via API key

    This endpoint allows agents to retrieve their own information
    without knowing their agent_id. Useful for self-service operations.

    Example:
        GET /api/v1/agents/me
        Authorization: Bearer acn_xxxxx
    """
    # Parse API key from Authorization header
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    api_key = authorization[7:]  # Remove "Bearer " prefix

    # Find agent by API key
    agent = await agent_service.get_agent_by_api_key(api_key)
    if not agent:
        raise HTTPException(status_code=401, detail="Invalid API key")

    base_url = settings.gateway_base_url or f"http://localhost:{settings.port}"

    return AgentMeResponse(
        agent_id=agent.agent_id,
        name=agent.name,
        description=agent.description,
        skills=agent.skills or [],
        status=agent.status.value,
        claim_status=agent.claim_status.value if agent.claim_status else "unclaimed",
        owner=agent.owner,
        registered_at=agent.registered_at.isoformat() if agent.registered_at else None,
        last_heartbeat=agent.last_heartbeat.isoformat() if agent.last_heartbeat else None,
        tasks_endpoint=f"{base_url}/api/v1/tasks",
        heartbeat_endpoint=f"{base_url}/api/v1/agents/{agent.agent_id}/heartbeat",
    )


@router.get("/unclaimed", response_model=AgentSearchResponse)
async def list_unclaimed_agents(
    _: InternalTokenDep,
    limit: int = 100,
    agent_service: AgentServiceDep = None,
):
    """
    List all unclaimed agents (requires X-Internal-Token)

    Returns agents that have joined but not been claimed by any owner.
    Restricted to ACN operators to prevent enumeration attacks.
    """
    agents = await agent_service.get_unclaimed_agents(limit=limit)
    agent_infos = [_agent_entity_to_info(a) for a in agents]

    return AgentSearchResponse(
        agents=agent_infos,
        total=len(agent_infos),
    )


@router.get("/{agent_id}", response_model=AgentInfo)
async def get_agent(agent_id: str, agent_service: AgentServiceDep = None):
    """Get agent information

    Clean Architecture: Route → AgentService → Repository
    """
    try:
        agent = await agent_service.get_agent(agent_id)
        return _agent_entity_to_info(agent)
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e


@router.get("", response_model=AgentSearchResponse)
async def search_agents(
    skill: str | None = None,
    status: str = "online",
    owner: str | None = None,
    name: str | None = None,
    agent_service: AgentServiceDep = None,
):
    """Search agents

    Clean Architecture: Route → AgentService → Repository
    """
    skill_list = skill.split(",") if skill else None

    # Search using AgentService
    agents = await agent_service.search_agents(
        skills=skill_list,
        status=status,
    )

    # Apply additional filters (owner, name)
    if owner:
        agents = [a for a in agents if a.owner == owner]
    if name:
        agents = [a for a in agents if name.lower() in a.name.lower()]

    # Convert to AgentInfo
    agent_infos = [_agent_entity_to_info(a) for a in agents]

    return AgentSearchResponse(
        agents=agent_infos,
        total=len(agent_infos),
    )


@router.post("/{agent_id}/heartbeat")
async def agent_heartbeat(
    agent_id: str,
    agent_info: AgentApiKeyDep,
    agent_service: AgentServiceDep = None,
):
    """Update agent heartbeat (requires Agent API Key)

    The authenticated agent must match the path `agent_id` to prevent
    falsely keeping other agents alive.
    Clean Architecture: Route → AgentService → Repository
    """
    if agent_info["agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="API key does not match agent_id")
    try:
        await agent_service.update_heartbeat(agent_id)
        return {"status": "ok", "agent_id": agent_id}
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e


@router.get("/{agent_id}/.well-known/agent-card.json")
async def get_agent_card(agent_id: str, agent_service: AgentServiceDep = None):
    """Get agent's A2A Agent Card (v0.3.0 compliant)

    Returns the card submitted at registration time if available.
    Falls back to auto-generating a minimal card from stored fields.
    """
    try:
        agent = await agent_service.get_agent(agent_id)

        # Return the complete card submitted at registration (e.g. OpenPersona-generated)
        if agent.agent_card:
            return agent.agent_card

        # Fallback: auto-generate a minimal card from stored fields
        card = AgentCard(
            name=agent.name,
            version="0.1.0",
            description=agent.description or f"{agent.name} on ACN",
            url=agent.endpoint or "",
            capabilities=AgentCapabilities(streaming=False),
            default_input_modes=["text", "application/json"],
            default_output_modes=["text", "application/json"],
            skills=[
                AgentSkill(
                    id=skill,
                    name=skill.replace("-", " ").replace("_", " ").title(),
                    description=f"Capability: {skill}",
                    tags=[skill],
                )
                for skill in agent.skills
            ],
        )

        return card.model_dump(exclude_none=True)
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e


@router.get("/{agent_id}/endpoint")
async def get_agent_endpoint(agent_id: str, agent_service: AgentServiceDep = None):
    """Get agent endpoint

    Clean Architecture: Route → AgentService → Repository
    """
    try:
        agent = await agent_service.get_agent(agent_id)
        return {"agent_id": agent_id, "endpoint": agent.endpoint}
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e


@router.delete("/{agent_id}")
async def unregister_agent(
    agent_id: str,
    payload: dict = Depends(require_permission("acn:write")),
    agent_service: AgentServiceDep = None,
):
    """Unregister an agent

    Clean Architecture: Route → AgentService → Repository
    """
    token_owner: str = payload.get("sub", "")

    try:
        # AgentService handles authorization check
        success = await agent_service.unregister_agent(agent_id, token_owner)

        if success:
            logger.info("agent_unregistered", agent_id=agent_id)
            return {"status": "unregistered", "agent_id": agent_id}
        else:
            raise HTTPException(status_code=404, detail="Agent not found")
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


# ============================================================================
# Autonomous Agent Endpoints (No Auth0 required)
# ============================================================================


async def _grant_referral_reward(referrer_id: str, new_agent_id: str) -> None:
    """Background task to grant referral reward"""
    try:
        rewards_client = RewardsClient(backend_url=settings.backend_url)
        result = await rewards_client.grant_referral_bonus(
            referrer_id=referrer_id,
            new_agent_id=new_agent_id,
        )
        if result.success:
            logger.info(
                "referral_reward_granted",
                referrer_id=referrer_id,
                new_agent_id=new_agent_id,
                amount=result.amount,
            )
        else:
            logger.warning(
                "referral_reward_failed",
                referrer_id=referrer_id,
                new_agent_id=new_agent_id,
                error=result.error,
            )
    except Exception as e:
        logger.error(
            "referral_reward_error",
            referrer_id=referrer_id,
            new_agent_id=new_agent_id,
            error=str(e),
        )


@router.post("/join", response_model=AgentJoinResponse)
@limiter.limit("10/minute")
async def join_agent(
    request: Request,
    body: AgentJoinRequest,
    background_tasks: BackgroundTasks,
    agent_service: AgentServiceDep = None,
):
    """
    Autonomous agent joins ACN (self-registration)

    No authentication required. Returns an API key for future requests.
    The agent will be in "unclaimed" status until a human claims it.

    If a referrer_id is provided and valid, the referrer will receive
    a referral bonus (managed by Backend's Rewards API).

    Example:
        POST /api/v1/agents/join
        {
            "name": "MyAgent",
            "description": "An autonomous coding agent",
            "skills": ["coding", "review"],
            "endpoint": "https://my-agent.example.com/a2a",
            "agent_card": { "name": "MyAgent", "version": "1.0.0", ... },
            "referrer_id": "optional-referrer-agent-id"
        }
    """
    try:
        agent, api_key = await agent_service.join_agent(
            name=body.name,
            description=body.description,
            skills=body.skills,
            endpoint=body.endpoint,
            referrer_id=body.referrer_id,
            agent_card=body.agent_card,
        )

        base_url = settings.gateway_base_url or f"http://localhost:{settings.port}"

        logger.info("agent_joined", agent_id=agent.agent_id, name=agent.name)

        # Grant referral reward in background (if referrer provided)
        if body.referrer_id:
            background_tasks.add_task(
                _grant_referral_reward,
                referrer_id=body.referrer_id,
                new_agent_id=agent.agent_id,
            )

        return AgentJoinResponse(
            agent_id=agent.agent_id,
            api_key=api_key,
            status=agent.status.value,
            claim_status=agent.claim_status.value if agent.claim_status else "unclaimed",
            verification_code=agent.verification_code or "",
            claim_url=f"{base_url}/claim/{agent.agent_id}",
            tasks_endpoint=f"{base_url}/api/v1/tasks",
            heartbeat_endpoint=f"{base_url}/api/v1/agents/{agent.agent_id}/heartbeat",
            agent_card_url=f"{base_url}/api/v1/agents/{agent.agent_id}/.well-known/agent-card.json",
        )
    except Exception as e:
        logger.error("agent_join_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/{agent_id}/claim", response_model=AgentClaimResponse)
async def claim_agent(
    agent_id: str,
    request: AgentClaimRequest,
    payload: dict = Depends(require_permission("acn:write")),
    agent_service: AgentServiceDep = None,
):
    """
    Claim ownership of an unclaimed agent

    Requires Auth0 authentication. The authenticated user becomes the owner.
    """
    token_owner: str = payload.get("sub", "")

    try:
        agent = await agent_service.claim_agent(
            agent_id=agent_id,
            owner=token_owner,
            verification_code=request.verification_code,
        )

        logger.info("agent_claimed", agent_id=agent_id, owner=token_owner)

        return AgentClaimResponse(
            success=True,
            agent_id=agent.agent_id,
            owner=agent.owner,
            message=f"Agent '{agent.name}' successfully claimed",
        )
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/{agent_id}/transfer", response_model=AgentTransferResponse)
async def transfer_agent(
    agent_id: str,
    request: AgentTransferRequest,
    payload: dict = Depends(require_permission("acn:write")),
    agent_service: AgentServiceDep = None,
):
    """
    Transfer agent ownership to another user

    Only the current owner can transfer the agent.
    """
    token_owner: str = payload.get("sub", "")

    try:
        agent = await agent_service.transfer_agent(
            agent_id=agent_id,
            current_owner=token_owner,
            new_owner=request.new_owner,
        )

        logger.info(
            "agent_transferred",
            agent_id=agent_id,
            from_owner=token_owner,
            to_owner=request.new_owner,
        )

        return AgentTransferResponse(
            success=True,
            agent_id=agent.agent_id,
            previous_owner=token_owner,
            new_owner=agent.owner,
            message=f"Agent '{agent.name}' transferred to {request.new_owner}",
        )
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


@router.post("/{agent_id}/release", response_model=AgentReleaseResponse)
async def release_agent(
    agent_id: str,
    payload: dict = Depends(require_permission("acn:write")),
    agent_service: AgentServiceDep = None,
):
    """
    Release ownership of an agent (make it unowned/unclaimed)

    Only the current owner can release the agent.
    After release, anyone can claim the agent again.
    """
    token_owner: str = payload.get("sub", "")

    try:
        agent = await agent_service.release_agent(
            agent_id=agent_id,
            owner=token_owner,
        )

        logger.info("agent_released", agent_id=agent_id, previous_owner=token_owner)

        return AgentReleaseResponse(
            success=True,
            agent_id=agent.agent_id,
            previous_owner=token_owner,
            message=f"Agent '{agent.name}' released. It can now be claimed by anyone.",
        )
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


# ============================================================================
# Agent Wallet Management API
# ============================================================================


# [REMOVED] Agent Wallet endpoints - 前端直接调 Backend API:
#   GET  /api/agent-wallets/{agent_id}           获取钱包
#   POST /api/agent-wallets/{agent_id}/topup     充值
#   POST /api/agent-wallets/{agent_id}/withdraw  提取


# [DELETED] set_agent_owner_share endpoint - 不再支持 owner_share 分成机制
