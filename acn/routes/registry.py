"""Agent Registry API Routes"""

from fastapi import APIRouter, Depends, HTTPException

from ..auth.middleware import get_subject, require_permission
from ..models import AgentInfo, AgentRegisterRequest, AgentRegisterResponse, AgentSearchResponse
from .dependencies import (  # type: ignore[import-untyped]
    RegistryDep,
    SubnetManagerDep,
)

router = APIRouter(prefix="/api/v1/agents", tags=["registry"])


@router.post("/register", response_model=AgentRegisterResponse)
async def register_agent(
    request: AgentRegisterRequest,
    payload: dict = Depends(require_permission("acn:write")),
    registry: RegistryDep = None,
    subnet_manager: SubnetManagerDep = None,
):
    """Register an Agent (Idempotent) - Requires Auth0 Token"""
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
        agent_id = await registry.register_agent(
            owner=request.owner,
            name=request.name,
            endpoint=request.endpoint,
            skills=request.skills,
            agent_card=request.agent_card,
            subnet_ids=subnet_ids,
        )

        return AgentRegisterResponse(
            agent_id=agent_id,
            message="Agent registered successfully" if agent_id else "Agent updated",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{agent_id}", response_model=AgentInfo)
async def get_agent(agent_id: str, registry: RegistryDep = None):
    """Get agent information"""
    agent = await registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.get("", response_model=AgentSearchResponse)
async def search_agents(
    skill: str = None,
    status: str = "online",
    owner: str = None,
    name: str = None,
    registry: RegistryDep = None,
):
    """Search agents"""
    skill_list = skill.split(",") if skill else None
    agents = await registry.search_agents(
        skills=skill_list,
        status=status,
        owner=owner,
        name=name,
    )
    return agents


@router.post("/{agent_id}/heartbeat")
async def agent_heartbeat(agent_id: str, registry: RegistryDep = None):
    """Update agent heartbeat"""
    success = await registry.update_heartbeat(agent_id)
    if not success:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "ok", "agent_id": agent_id}


@router.get("/{agent_id}/.well-known/agent-card.json")
async def get_agent_card(agent_id: str, registry: RegistryDep = None):
    """Get agent's A2A Agent Card"""
    agent = await registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if agent.agent_card:
        return agent.agent_card

    raise HTTPException(status_code=404, detail="Agent Card not available")


@router.get("/{agent_id}/endpoint")
async def get_agent_endpoint(agent_id: str, registry: RegistryDep = None):
    """Get agent endpoint"""
    agent = await registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    return {"agent_id": agent_id, "endpoint": agent.endpoint}
