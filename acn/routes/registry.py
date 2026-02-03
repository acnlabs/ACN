"""Agent Registry API Routes

Clean Architecture implementation: Route → Service → Repository

Supports two registration modes:
1. Platform Registration (managed): POST /register - requires Auth0
2. Autonomous Join: POST /join - no auth, returns API key
"""

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth.middleware import get_subject, require_permission
from ..config import get_settings
from ..core.exceptions import AgentNotFoundException
from ..models import AgentInfo, AgentRegisterRequest, AgentRegisterResponse, AgentSearchResponse
from .dependencies import (  # type: ignore[import-untyped]
    AgentServiceDep,
    SubnetManagerDep,
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


class AgentClaimRequest(BaseModel):
    """Request to claim an agent"""

    verification_code: str | None = Field(None, description="Verification code")


class AgentClaimResponse(BaseModel):
    """Response after claiming an agent"""

    success: bool
    agent_id: str
    owner: str
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
        agent_card=None,  # TODO: Generate from entity
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
        # Agent Wallet
        balance=agent.balance,
        total_earned=agent.total_earned,
        total_spent=agent.total_spent,
        owner_share=agent.owner_share,
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
    # Extract owner from Auth0 token
    token_owner = await get_subject()

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
            description=request.description if hasattr(request, "description") else "",
            metadata=request.metadata if hasattr(request, "metadata") else {},
        )

        # Generate Agent Card URL
        base_url = settings.gateway_base_url or f"http://localhost:{settings.port}"
        agent_card_url = f"{base_url}/.well-known/agent-card.json?agent_id={agent.agent_id}"

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
    skill: str = None,
    status: str = "online",
    owner: str = None,
    name: str = None,
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
async def agent_heartbeat(agent_id: str, agent_service: AgentServiceDep = None):
    """Update agent heartbeat

    Clean Architecture: Route → AgentService → Repository
    """
    try:
        await agent_service.update_heartbeat(agent_id)
        return {"status": "ok", "agent_id": agent_id}
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e


@router.get("/{agent_id}/.well-known/agent-card.json")
async def get_agent_card(agent_id: str, agent_service: AgentServiceDep = None):
    """Get agent's A2A Agent Card

    Clean Architecture: Route → AgentService → Repository
    """
    try:
        agent = await agent_service.get_agent(agent_id)

        # Generate A2A-compliant Agent Card
        agent_card = {
            "protocolVersion": "0.3.0",
            "name": agent.name,
            "description": agent.description or "",
            "url": agent.endpoint,
            "skills": [{"id": skill, "name": skill} for skill in agent.skills],
        }

        return agent_card
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
    # Extract owner from Auth0 token
    token_owner = await get_subject()

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


@router.post("/join", response_model=AgentJoinResponse)
async def join_agent(
    request: AgentJoinRequest,
    agent_service: AgentServiceDep = None,
):
    """
    Autonomous agent joins ACN (self-registration)

    No authentication required. Returns an API key for future requests.
    The agent will be in "unclaimed" status until a human claims it.

    Example:
        POST /api/v1/agents/join
        {
            "name": "MyAgent",
            "description": "An autonomous coding agent",
            "skills": ["coding", "review"],
            "referrer_id": "optional-referrer-agent-id"
        }
    """
    try:
        agent, api_key = await agent_service.join_agent(
            name=request.name,
            description=request.description,
            skills=request.skills,
            endpoint=request.endpoint,
            referrer_id=request.referrer_id,
        )

        base_url = settings.gateway_base_url or f"http://localhost:{settings.port}"

        logger.info("agent_joined", agent_id=agent.agent_id, name=agent.name)

        return AgentJoinResponse(
            agent_id=agent.agent_id,
            api_key=api_key,
            status=agent.status.value,
            claim_status=agent.claim_status.value if agent.claim_status else "unclaimed",
            verification_code=agent.verification_code or "",
            claim_url=f"{base_url}/claim/{agent.agent_id}",
            tasks_endpoint=f"{base_url}/api/v1/tasks",
            heartbeat_endpoint=f"{base_url}/api/v1/agents/{agent.agent_id}/heartbeat",
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
    token_owner = await get_subject()

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
    token_owner = await get_subject()

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
    token_owner = await get_subject()

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


@router.get("/unclaimed", response_model=AgentSearchResponse)
async def list_unclaimed_agents(
    limit: int = 100,
    agent_service: AgentServiceDep = None,
):
    """
    List all unclaimed agents

    Returns agents that have joined but not been claimed by any owner.
    """
    agents = await agent_service.get_unclaimed_agents(limit=limit)
    agent_infos = [_agent_entity_to_info(a) for a in agents]

    return AgentSearchResponse(
        agents=agent_infos,
        total=len(agent_infos),
    )


# ============================================================================
# Agent Wallet Management API
# ============================================================================


class AgentWalletResponse(BaseModel):
    """Agent wallet information"""

    agent_id: str
    balance: float = Field(..., description="Current balance")
    owner_share: float = Field(..., description="Owner's share of earnings (0-1)")
    total_earned: float = Field(..., description="Historical total earnings")
    total_spent: float = Field(..., description="Historical total spent")


class AgentWalletTopUpRequest(BaseModel):
    """Request to top up agent wallet"""

    amount: float = Field(..., gt=0, description="Amount to add")


class AgentWalletWithdrawRequest(BaseModel):
    """Request to withdraw from agent wallet"""

    amount: float = Field(..., gt=0, description="Amount to withdraw")


class AgentWalletShareRequest(BaseModel):
    """Request to set owner share"""

    owner_share: float = Field(..., ge=0, le=1, description="Owner's share (0-1)")


class AgentWalletTransactionResponse(BaseModel):
    """Response for wallet transactions"""

    success: bool
    agent_id: str
    balance: float
    message: str


@router.get("/{agent_id}/wallet", response_model=AgentWalletResponse)
async def get_agent_wallet(
    agent_id: str,
    agent_service: AgentServiceDep = None,
):
    """
    Get agent's wallet information.

    Public endpoint - anyone can view agent wallet info.
    """
    try:
        agent = await agent_service.get_agent(agent_id)
        return AgentWalletResponse(
            agent_id=agent.agent_id,
            balance=agent.balance,
            owner_share=agent.owner_share,
            total_earned=agent.total_earned,
            total_spent=agent.total_spent,
        )
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e


@router.post("/{agent_id}/wallet/topup", response_model=AgentWalletTransactionResponse)
async def topup_agent_wallet(
    agent_id: str,
    request: AgentWalletTopUpRequest,
    http_request: Request,
    payload: dict = Depends(require_permission("acn:write")),
    agent_service: AgentServiceDep = None,
):
    """
    Top up agent's wallet.

    Only the owner can top up the agent's wallet.
    The amount is transferred from owner's credits to agent's balance.
    """
    token_owner = await get_subject()
    # Dev mode: allow X-Creator-Id header override
    owner_id = http_request.headers.get("x-creator-id") or token_owner

    try:
        agent = await agent_service.get_agent(agent_id)

        # Verify caller is the owner
        if agent.owner != owner_id:
            raise HTTPException(status_code=403, detail="Only the owner can top up agent wallet")

        # Add funds to agent
        agent.receive(request.amount)
        await agent_service.repository.save(agent)

        logger.info(
            "agent_wallet_topped_up",
            agent_id=agent_id,
            amount=request.amount,
            owner=owner_id,
            new_balance=agent.balance,
        )

        return AgentWalletTransactionResponse(
            success=True,
            agent_id=agent_id,
            balance=agent.balance,
            message=f"Added {request.amount} to agent wallet",
        )
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/{agent_id}/wallet/withdraw", response_model=AgentWalletTransactionResponse)
async def withdraw_from_agent_wallet(
    agent_id: str,
    request: AgentWalletWithdrawRequest,
    http_request: Request,
    payload: dict = Depends(require_permission("acn:write")),
    agent_service: AgentServiceDep = None,
):
    """
    Withdraw from agent's wallet.

    Only the owner can withdraw from agent's wallet.
    The amount is transferred from agent's balance to owner's earnings.
    """
    token_owner = await get_subject()
    # Dev mode: allow X-Creator-Id header override
    owner_id = http_request.headers.get("x-creator-id") or token_owner

    try:
        agent = await agent_service.get_agent(agent_id)

        # Verify caller is the owner
        if agent.owner != owner_id:
            raise HTTPException(
                status_code=403, detail="Only the owner can withdraw from agent wallet"
            )

        # Withdraw funds
        agent.withdraw(request.amount)
        await agent_service.repository.save(agent)

        logger.info(
            "agent_wallet_withdrawal",
            agent_id=agent_id,
            amount=request.amount,
            owner=owner_id,
            new_balance=agent.balance,
        )

        return AgentWalletTransactionResponse(
            success=True,
            agent_id=agent_id,
            balance=agent.balance,
            message=f"Withdrew {request.amount} from agent wallet",
        )
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/{agent_id}/wallet/share", response_model=AgentWalletTransactionResponse)
async def set_agent_owner_share(
    agent_id: str,
    request: AgentWalletShareRequest,
    http_request: Request,
    payload: dict = Depends(require_permission("acn:write")),
    agent_service: AgentServiceDep = None,
):
    """
    Set owner's share of agent's future earnings.

    Only the owner can set the share ratio.
    - 0.0 = Agent keeps all earnings
    - 1.0 = Owner gets all earnings
    - 0.7 = Owner gets 70%, agent keeps 30%
    """
    token_owner = await get_subject()
    # Dev mode: allow X-Creator-Id header override
    owner_id = http_request.headers.get("x-creator-id") or token_owner

    try:
        agent = await agent_service.get_agent(agent_id)

        # Verify caller is the owner
        if agent.owner != owner_id:
            raise HTTPException(status_code=403, detail="Only the owner can set earnings share")

        # Set owner share
        agent.set_owner_share(request.owner_share)
        await agent_service.repository.save(agent)

        logger.info(
            "agent_owner_share_updated",
            agent_id=agent_id,
            owner_share=request.owner_share,
            owner=owner_id,
        )

        return AgentWalletTransactionResponse(
            success=True,
            agent_id=agent_id,
            balance=agent.balance,
            message=f"Owner share set to {request.owner_share * 100}%",
        )
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
