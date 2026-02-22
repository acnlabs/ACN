"""Subnet Management API Routes

Clean Architecture implementation: Route → Service → Repository
"""

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, HTTPException

from ..auth.middleware import get_subject, require_permission
from ..config import get_settings
from ..core.exceptions import AgentNotFoundException, SubnetNotFoundException
from ..models import SubnetCreateRequest, SubnetCreateResponse, SubnetInfo
from .dependencies import (  # type: ignore[import-untyped]
    AgentApiKeyDep,
    AgentServiceDep,
    SubnetServiceDep,
)

router = APIRouter(prefix="/api/v1/subnets", tags=["subnets"])
logger = structlog.get_logger()
settings = get_settings()


def _subnet_entity_to_info(subnet) -> SubnetInfo:
    """Convert Subnet entity to SubnetInfo model"""
    return SubnetInfo(
        subnet_id=subnet.subnet_id,
        name=subnet.name,
        owner=subnet.owner,
        description=subnet.description,
        is_private=subnet.is_private,
        security_config=subnet.security_config,
        created_at=subnet.created_at,
        metadata=subnet.metadata,
    )


@router.post("", response_model=SubnetCreateResponse)
async def create_subnet(
    request: SubnetCreateRequest,
    payload: dict = Depends(require_permission("acn:write")),
    subnet_service: SubnetServiceDep = None,
):
    """Create a new subnet

    Clean Architecture: Route → SubnetService → Repository
    """
    # Extract owner from Auth0 token
    owner = await get_subject()

    try:
        # Use SubnetService
        subnet = await subnet_service.create_subnet(
            subnet_id=request.subnet_id
            or f"subnet-{owner}-{request.name.lower().replace(' ', '-')}",
            name=request.name,
            owner=owner,
            description=request.description,
            is_private=request.is_private or False,
            security_config=request.security_config or {},
            metadata={},
        )

        # Generate gateway URL
        base_url = settings.gateway_base_url or f"http://localhost:{settings.port}"
        gateway_url = f"{base_url}/gateway/a2a/{subnet.subnet_id}"

        logger.info("subnet_created", subnet_id=subnet.subnet_id, owner=owner)

        return SubnetCreateResponse(
            subnet_id=subnet.subnet_id,
            name=subnet.name,
            gateway_url=gateway_url,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("subnet_creation_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("")
async def list_subnets(
    owner: str = None,
    subnet_service: SubnetServiceDep = None,
):
    """List all subnets

    Clean Architecture: Route → SubnetService → Repository
    """
    try:
        if owner:
            subnets = await subnet_service.list_subnets(owner=owner)
        else:
            subnets = await subnet_service.list_public_subnets()

        # Convert to SubnetInfo
        subnet_infos = [_subnet_entity_to_info(s) for s in subnets]

        return {"subnets": subnet_infos, "count": len(subnet_infos)}
    except Exception as e:
        logger.error("list_subnets_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{subnet_id}")
async def get_subnet(
    subnet_id: str,
    subnet_service: SubnetServiceDep = None,
):
    """Get subnet details

    Clean Architecture: Route → SubnetService → Repository
    """
    try:
        subnet = await subnet_service.get_subnet(subnet_id)
        return _subnet_entity_to_info(subnet)
    except SubnetNotFoundException as e:
        raise HTTPException(status_code=404, detail="Subnet not found") from e


@router.get("/{subnet_id}/agents")
async def get_subnet_agents(
    subnet_id: str,
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
):
    """Get all agents in a subnet

    Clean Architecture: Route → Service → Repository
    """
    # Verify subnet exists
    try:
        await subnet_service.get_subnet(subnet_id)
    except SubnetNotFoundException as e:
        raise HTTPException(status_code=404, detail="Subnet not found") from e

    try:
        agents = await agent_service.search_agents(subnet_id=subnet_id)

        # Convert to AgentInfo
        from .registry import _agent_entity_to_info

        agent_infos = [_agent_entity_to_info(a) for a in agents]

        return {"subnet_id": subnet_id, "agents": agent_infos, "count": len(agent_infos)}
    except Exception as e:
        logger.error("get_subnet_agents_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/{agent_id}/subnets/{subnet_id}")
async def join_subnet(
    agent_id: str,
    subnet_id: str,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
):
    """Agent joins a subnet (requires Agent API Key)

    The authenticated agent must match the path `agent_id`.
    Clean Architecture: Route → Service → Repository
    """
    if agent_info["agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="API key does not match agent_id")

    # Verify subnet exists
    try:
        await subnet_service.get_subnet(subnet_id)
    except SubnetNotFoundException as e:
        raise HTTPException(status_code=404, detail="Subnet not found") from e

    # Verify agent exists and join subnet
    try:
        await agent_service.join_subnet(agent_id, subnet_id)

        # Also update subnet members
        await subnet_service.add_member(subnet_id, agent_id)

        logger.info("agent_joined_subnet", agent_id=agent_id, subnet_id=subnet_id)

        return {"status": "joined", "agent_id": agent_id, "subnet_id": subnet_id}
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e
    except Exception as e:
        logger.error("join_subnet_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.delete("/{agent_id}/subnets/{subnet_id}")
async def leave_subnet(
    agent_id: str,
    subnet_id: str,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
):
    """Agent leaves a subnet (requires Agent API Key)

    The authenticated agent must match the path `agent_id`.
    Clean Architecture: Route → Service → Repository
    """
    if agent_info["agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="API key does not match agent_id")

    try:
        await agent_service.leave_subnet(agent_id, subnet_id)
        await subnet_service.remove_member(subnet_id, agent_id)

        logger.info("agent_left_subnet", agent_id=agent_id, subnet_id=subnet_id)

        return {"status": "left", "agent_id": agent_id, "subnet_id": subnet_id}
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e
    except SubnetNotFoundException as e:
        raise HTTPException(status_code=404, detail="Subnet not found") from e
    except Exception as e:
        logger.error("leave_subnet_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{agent_id}/subnets")
async def get_agent_subnets(
    agent_id: str,
    agent_info: AgentApiKeyDep,
    agent_service: AgentServiceDep = None,
):
    """Get subnets an agent belongs to (requires Agent API Key)

    An agent may only query its own subnet membership.
    Clean Architecture: Route → AgentService → Repository
    """
    if agent_info["agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="API key does not match agent_id")
    try:
        agent = await agent_service.get_agent(agent_id)
        return {"agent_id": agent_id, "subnets": agent.subnet_ids}
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e


@router.delete("/{subnet_id}")
async def delete_subnet(
    subnet_id: str,
    payload: dict = Depends(require_permission("acn:write")),
    subnet_service: SubnetServiceDep = None,
):
    """Delete a subnet

    Clean Architecture: Route → SubnetService → Repository
    """
    # Extract owner from Auth0 token
    owner = await get_subject()

    try:
        success = await subnet_service.delete_subnet(subnet_id, owner)
        if success:
            logger.info("subnet_deleted", subnet_id=subnet_id, owner=owner)
            return {"status": "deleted", "subnet_id": subnet_id}
        else:
            raise HTTPException(status_code=404, detail="Subnet not found")
    except SubnetNotFoundException as e:
        raise HTTPException(status_code=404, detail="Subnet not found") from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except Exception as e:
        logger.error("delete_subnet_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e
