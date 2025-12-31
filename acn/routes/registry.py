"""Agent Registry API Routes

Clean Architecture implementation: Route → Service → Repository
"""

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, HTTPException

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
    
    logger.warning("DEV MODE: Registering agent without authentication", 
                   owner=request.owner, name=request.name)
    
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
        owner=agent.owner,
        name=agent.name,
        description=agent.description,
        endpoint=agent.endpoint,
        skills=agent.skills,
        status=agent.status.value,
        subnet_ids=agent.subnet_ids,
        agent_card=None,  # TODO: Generate from entity
        metadata=agent.metadata,
        registered_at=agent.registered_at,
        last_heartbeat=agent.last_heartbeat,
        wallet_address=agent.wallet_address,
        accepts_payment=agent.accepts_payment,
        payment_methods=agent.payment_methods,
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
