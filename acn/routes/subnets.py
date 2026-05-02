"""Subnet Management API Routes

Clean Architecture implementation: Route → Service → Repository
"""

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..auth.middleware import require_internal_or_permission, require_permission, verify_token
from ..config import get_settings
from ..core.errors import ACNHTTPError, ErrorCode
from ..core.exceptions import AgentNotFoundException, SubnetNotFoundException
from ..models import SubnetCreateRequest, SubnetCreateResponse, SubnetInfo
from .dependencies import (  # type: ignore[import-untyped]
    AgentApiKeyDep,
    AgentIdPath,
    AgentServiceDep,
    SubnetIdPath,
    SubnetServiceDep,
)

_optional_bearer = HTTPBearer(auto_error=False)

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
    payload: dict = Depends(require_internal_or_permission("acn:write")),
    subnet_service: SubnetServiceDep = None,
):
    """Create a new subnet

    Clean Architecture: Route → SubnetService → Repository
    """
    # Extract owner from Auth0 token
    owner = payload.get("sub", "dev@clients")

    try:
        # Use SubnetService
        security_cfg = request.security_config or (
            dict(request.security_schemes) if request.security_schemes else {}
        )
        subnet = await subnet_service.create_subnet(
            subnet_id=request.subnet_id
            or f"subnet-{owner}-{request.name.lower().replace(' ', '-')}",
            name=request.name,
            owner=owner,
            description=request.description,
            is_private=request.is_private,
            security_config=security_cfg,
            metadata={},
        )

        # Generate gateway URLs
        base_url = settings.gateway_base_url or f"http://localhost:{settings.port}"
        gateway_a2a_url = f"{base_url}/gateway/a2a/{subnet.subnet_id}"
        gateway_ws_url = f"{base_url}/gateway/ws/{subnet.subnet_id}"

        logger.info("subnet_created", subnet_id=subnet.subnet_id, owner=owner)

        return SubnetCreateResponse(
            status="created",
            subnet_id=subnet.subnet_id,
            is_public=not subnet.is_private,
            gateway_ws_url=gateway_ws_url,
            gateway_a2a_url=gateway_a2a_url,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("subnet_creation_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create subnet") from e


@router.get("")
async def list_subnets(
    request: Request,
    owner: str = None,
    credentials: HTTPAuthorizationCredentials | None = Security(_optional_bearer),
    subnet_service: SubnetServiceDep = None,
):
    """List all subnets

    Clean Architecture: Route → SubnetService → Repository
    When ?owner= is provided, authentication is required and the caller must
    be the owner (or hold acn:admin permission).
    """
    try:
        if owner:
            # Require auth when filtering by owner to prevent private subnet enumeration
            if not credentials:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required when filtering by owner",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            payload = await verify_token(request, credentials)
            requester = payload.get("sub", "")
            permissions = payload.get("permissions", [])
            if requester != owner and "acn:admin" not in permissions:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot list subnets for another user",
                )
            subnets = await subnet_service.list_subnets(owner=owner)
        else:
            subnets = await subnet_service.list_public_subnets()

        # Convert to SubnetInfo
        subnet_infos = [_subnet_entity_to_info(s) for s in subnets]

        return {"subnets": subnet_infos, "count": len(subnet_infos)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_subnets_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list subnets") from e


@router.get("/{subnet_id}")
async def get_subnet(
    subnet_id: SubnetIdPath,
    subnet_service: SubnetServiceDep = None,
):
    """Get subnet details

    Clean Architecture: Route → SubnetService → Repository
    """
    try:
        subnet = await subnet_service.get_subnet(subnet_id)
        return _subnet_entity_to_info(subnet)
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"subnet_id": subnet_id},
        ) from e


@router.get("/{subnet_id}/agents")
async def get_subnet_agents(
    subnet_id: SubnetIdPath,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_optional_bearer),
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
):
    """Get all agents in a subnet

    Clean Architecture: Route → Service → Repository
    Private subnets require authentication; the caller must be the owner
    or hold acn:admin permission.
    """
    # Verify subnet exists and check privacy
    try:
        subnet = await subnet_service.get_subnet(subnet_id)
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"subnet_id": subnet_id},
        ) from e

    # Enforce auth for private subnets
    if getattr(subnet, "is_private", False):
        if not credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required to view private subnet members",
                headers={"WWW-Authenticate": "Bearer"},
            )
        payload = await verify_token(request, credentials)
        requester = payload.get("sub", "")
        permissions = payload.get("permissions", [])
        if requester != subnet.owner and "acn:admin" not in permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: private subnet",
            )

    try:
        agents = await agent_service.search_agents(subnet_id=subnet_id)

        # Convert to AgentInfo
        from .registry import _agent_entity_to_info

        agent_infos = [_agent_entity_to_info(a, strip_sensitive=True) for a in agents]

        return {"subnet_id": subnet_id, "agents": agent_infos, "count": len(agent_infos)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_subnet_agents_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve subnet agents") from e


@router.post("/{agent_id}/subnets/{subnet_id}")
async def join_subnet(
    agent_id: AgentIdPath,
    subnet_id: SubnetIdPath,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
):
    """Agent joins a subnet (requires Agent API Key)

    The authenticated agent must match the path `agent_id`.
    Clean Architecture: Route → Service → Repository
    """
    if agent_info["agent_id"] != agent_id:
        raise ACNHTTPError(
            ErrorCode.API_KEY_AGENT_MISMATCH,
            403,
            details={
                "path_agent": agent_id,
                "key_agent": agent_info["agent_id"],
            },
        )

    # Verify subnet exists
    try:
        await subnet_service.get_subnet(subnet_id)
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"subnet_id": subnet_id},
        ) from e

    # Verify agent exists and join subnet
    try:
        await agent_service.join_subnet(agent_id, subnet_id)

        # Also update subnet members
        await subnet_service.add_member(subnet_id, agent_id)

        logger.info("agent_joined_subnet", agent_id=agent_id, subnet_id=subnet_id)

        return {"status": "joined", "agent_id": agent_id, "subnet_id": subnet_id}
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e
    except Exception as e:
        logger.error("join_subnet_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to join subnet") from e


@router.delete("/{agent_id}/subnets/{subnet_id}")
async def leave_subnet(
    agent_id: AgentIdPath,
    subnet_id: SubnetIdPath,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
):
    """Agent leaves a subnet (requires Agent API Key)

    The authenticated agent must match the path `agent_id`.
    Clean Architecture: Route → Service → Repository
    """
    if agent_info["agent_id"] != agent_id:
        raise ACNHTTPError(
            ErrorCode.API_KEY_AGENT_MISMATCH,
            403,
            details={
                "path_agent": agent_id,
                "key_agent": agent_info["agent_id"],
            },
        )

    try:
        await agent_service.leave_subnet(agent_id, subnet_id)
        await subnet_service.remove_member(subnet_id, agent_id)

        logger.info("agent_left_subnet", agent_id=agent_id, subnet_id=subnet_id)

        return {"status": "left", "agent_id": agent_id, "subnet_id": subnet_id}
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"subnet_id": subnet_id},
        ) from e
    except Exception as e:
        logger.error("leave_subnet_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to leave subnet") from e


@router.get("/{agent_id}/subnets")
async def get_agent_subnets(
    agent_id: AgentIdPath,
    agent_info: AgentApiKeyDep,
    agent_service: AgentServiceDep = None,
):
    """Get subnets an agent belongs to (requires Agent API Key)

    An agent may only query its own subnet membership.
    Clean Architecture: Route → AgentService → Repository
    """
    if agent_info["agent_id"] != agent_id:
        raise ACNHTTPError(
            ErrorCode.API_KEY_AGENT_MISMATCH,
            403,
            details={
                "path_agent": agent_id,
                "key_agent": agent_info["agent_id"],
            },
        )
    try:
        agent = await agent_service.get_agent(agent_id)
        return {"agent_id": agent_id, "subnets": agent.subnet_ids}
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_id},
        ) from e


@router.delete("/{subnet_id}")
async def delete_subnet(
    subnet_id: SubnetIdPath,
    payload: dict = Depends(require_permission("acn:write")),
    subnet_service: SubnetServiceDep = None,
):
    """Delete a subnet

    Clean Architecture: Route → SubnetService → Repository
    """
    # Extract owner from Auth0 token
    owner = payload.get("sub", "dev@clients")

    try:
        success = await subnet_service.delete_subnet(subnet_id, owner)
        if success:
            logger.info("subnet_deleted", subnet_id=subnet_id, owner=owner)
            return {"status": "deleted", "subnet_id": subnet_id}
        else:
            # NOTE (sprint #3): this in-try raise is intentionally NOT migrated
            # to ``ACNHTTPError`` — it would fall through to the catch-all
            # ``except Exception`` below and be silently rewritten as 500.
            # The same fragility exists today for the legacy ``HTTPException``
            # form (also ``Exception``-typed). Tracked as a P3 ticket in
            # ``docs/BACKLOG.md`` ("Add ``except ACNHTTPError: raise``
            # defence on registry's catch-all 5xx blocks") — to be fixed
            # holistically alongside sprint row #2b.
            raise HTTPException(status_code=404, detail="Subnet not found")
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"subnet_id": subnet_id},
        ) from e
    except PermissionError as e:
        logger.warning("delete_subnet_permission_denied", subnet_id=subnet_id, error=str(e))
        raise HTTPException(status_code=403, detail="Permission denied") from e
    except Exception as e:
        logger.error("delete_subnet_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete subnet") from e


# =============================================================================
# Internal Admin Endpoints (Backend service → ACN, requires X-Internal-Token)
# =============================================================================


@router.post("/{subnet_id}/members/{agent_id}", tags=["subnets-internal"])
async def admin_add_subnet_member(
    subnet_id: SubnetIdPath,
    agent_id: AgentIdPath,
    payload: dict = Depends(require_internal_or_permission("acn:admin")),
    subnet_service: SubnetServiceDep = None,
):
    """Add an agent to a subnet's member list (internal service call).

    Called by the Platform Backend when a WorkspaceMember is added.
    Requires X-Internal-Token header or acn:admin JWT permission.
    Returns 404 silently if the subnet does not exist (best-effort).
    """
    try:
        await subnet_service.add_member(subnet_id, agent_id)
        logger.info("admin_subnet_member_added", subnet_id=subnet_id, agent_id=agent_id)
        return {"status": "added", "subnet_id": subnet_id, "agent_id": agent_id}
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"subnet_id": subnet_id},
        ) from e
    except Exception as e:
        logger.error("admin_add_subnet_member_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to add subnet member") from e


@router.delete("/{subnet_id}/members/{agent_id}", tags=["subnets-internal"])
async def admin_remove_subnet_member(
    subnet_id: SubnetIdPath,
    agent_id: AgentIdPath,
    payload: dict = Depends(require_internal_or_permission("acn:admin")),
    subnet_service: SubnetServiceDep = None,
):
    """Remove an agent from a subnet's member list (internal service call).

    Called by the Platform Backend when a WorkspaceMember is removed.
    Requires X-Internal-Token header or acn:admin JWT permission.
    Returns 404 silently if the subnet does not exist (best-effort).
    """
    try:
        await subnet_service.remove_member(subnet_id, agent_id)
        logger.info("admin_subnet_member_removed", subnet_id=subnet_id, agent_id=agent_id)
        return {"status": "removed", "subnet_id": subnet_id, "agent_id": agent_id}
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"subnet_id": subnet_id},
        ) from e
    except Exception as e:
        logger.error("admin_remove_subnet_member_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to remove subnet member") from e
