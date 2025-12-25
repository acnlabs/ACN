"""Subnet Management API Routes"""

from fastapi import APIRouter, HTTPException

from ...models import SubnetCreateRequest, SubnetCreateResponse
from ..dependencies import RegistryDep, SubnetManagerDep  # type: ignore[import-untyped]

router = APIRouter(prefix="/api/v1/subnets", tags=["subnets"])


@router.post("", response_model=SubnetCreateResponse)
async def create_subnet(
    request: SubnetCreateRequest,
    subnet_manager: SubnetManagerDep = None,
):
    """Create a new subnet"""
    try:
        subnet_id = subnet_manager.create_subnet(
            name=request.name,
            description=request.description,
            is_private=request.is_private,
            allowed_agents=request.allowed_agents,
        )

        return SubnetCreateResponse(
            subnet_id=subnet_id,
            name=request.name,
            gateway_url=f"{subnet_manager.gateway_base_url}/gateway/a2a/{subnet_id}",
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("")
async def list_subnets(subnet_manager: SubnetManagerDep = None):
    """List all subnets"""
    try:
        subnets = subnet_manager.list_subnets()
        return {"subnets": subnets, "count": len(subnets)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{subnet_id}")
async def get_subnet(subnet_id: str, subnet_manager: SubnetManagerDep = None):
    """Get subnet details"""
    subnet = subnet_manager.get_subnet(subnet_id)
    if not subnet:
        raise HTTPException(status_code=404, detail="Subnet not found")
    return subnet


@router.get("/{subnet_id}/agents")
async def get_subnet_agents(
    subnet_id: str,
    subnet_manager: SubnetManagerDep = None,
    registry: RegistryDep = None,
):
    """Get all agents in a subnet"""
    if not subnet_manager.subnet_exists(subnet_id):
        raise HTTPException(status_code=404, detail="Subnet not found")

    try:
        agents = await registry.get_agents_by_subnet(subnet_id)
        return {"subnet_id": subnet_id, "agents": agents, "count": len(agents)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/{agent_id}/subnets/{subnet_id}")
async def join_subnet(
    agent_id: str,
    subnet_id: str,
    subnet_manager: SubnetManagerDep = None,
    registry: RegistryDep = None,
):
    """Agent joins a subnet"""
    if not subnet_manager.subnet_exists(subnet_id):
        raise HTTPException(status_code=404, detail="Subnet not found")

    agent = await registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        success = await registry.add_agent_to_subnet(agent_id, subnet_id)
        if success:
            return {"status": "joined", "agent_id": agent_id, "subnet_id": subnet_id}
        else:
            raise HTTPException(status_code=400, detail="Failed to join subnet")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{agent_id}/subnets")
async def get_agent_subnets(agent_id: str, registry: RegistryDep = None):
    """Get subnets an agent belongs to"""
    agent = await registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    return {"agent_id": agent_id, "subnets": agent.subnet_ids}

