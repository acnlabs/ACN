"""Subnet Management API Routes

Clean Architecture implementation: Route → Service → Repository
"""

import re
import secrets

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from ..auth.middleware import require_internal_or_permission, verify_token
from ..config import get_settings
from ..core.errors import ACN_DEFAULT_RESPONSES, ACNHTTPError, ErrorCode
from ..core.exceptions import SubnetNotFoundException
from ..models import SubnetCreateRequest, SubnetCreateResponse, SubnetInfo
from ._subnet_membership import (
    do_get_agent_subnets,
    do_join_subnet,
    do_leave_subnet,
)
from .dependencies import (  # type: ignore[import-untyped]
    AgentApiKeyDep,
    AgentIdPath,
    AgentServiceDep,
    SubnetIdPath,
    SubnetServiceDep,
    WebhookServiceDep,
    limiter,
)

_optional_bearer = HTTPBearer(auto_error=False)

router = APIRouter(
    prefix="/api/v1/subnets",
    tags=["subnets"],
    responses=ACN_DEFAULT_RESPONSES,
)
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
        harness_url=subnet.harness_url,
        harness_registered=subnet.harness_url is not None,
    )


def _generate_subnet_id(name: str) -> str:
    """Generate a compact, unique subnet_id from a human-readable name.

    Format: ``subnet-{slug}-{rand6}`` where slug is a lowercased, hyphen-
    delimited form of ``name`` truncated to 32 chars. Total length is
    bounded by ``len("subnet-") + 32 + 1 + 6 = 46`` — comfortably inside
    ``SubnetCreateRequest.subnet_id``'s ``max_length=64``.
    """
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:32] or "subnet"
    return f"subnet-{slug}-{secrets.token_hex(3)}"


@router.post("", response_model=SubnetCreateResponse)
@limiter.limit("5/minute")
async def create_subnet(
    request: Request,
    body: SubnetCreateRequest,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
):
    """Create a new subnet (requires Agent API Key — agent becomes the owner).

    Clean Architecture: Route → SubnetService → Repository

    Membership invariant (ADR-0001): ACN stores subnet membership as a
    bidirectional pair (``subnet.member_agent_ids`` + ``agent.subnet_ids``).
    ``SubnetService.create_subnet`` only writes the subnet side. The
    agent-side write is mirrored here via ``agent_service.join_subnet``,
    matching ``do_join_subnet`` in ``_subnet_membership.py``. Without
    this, freshly created subnets show ``member_count=0`` in any consumer
    that derives the count from ``agent.subnet_ids`` (the common path,
    e.g. ``agentplanet/frontend::buildSubnetHalos``).
    """
    owner = agent_info["agent_id"]

    try:
        security_cfg = body.security_config or (
            dict(body.security_schemes) if body.security_schemes else {}
        )
        subnet_id = body.subnet_id or _generate_subnet_id(body.name)
        subnet = await subnet_service.create_subnet(
            subnet_id=subnet_id,
            name=body.name,
            owner=owner,
            description=body.description,
            is_private=body.is_private,
            security_config=security_cfg,
            metadata={},
        )

        # Mirror the subnet-side owner add into the agent store. Wrapped in
        # try/except so we can roll back the half-created subnet if the
        # agent-side write fails (preserves the "create is atomic" contract
        # callers expect).
        try:
            await agent_service.join_subnet(owner, subnet.subnet_id)
        except Exception as join_error:  # noqa: BLE001 - rollback path
            logger.error(
                "subnet_owner_join_failed_rolling_back",
                subnet_id=subnet.subnet_id,
                owner=owner,
                error=str(join_error),
                exc_info=True,
            )
            try:
                await subnet_service.delete_subnet(subnet.subnet_id, owner)
            except Exception as rollback_error:  # noqa: BLE001
                logger.error(
                    "subnet_creation_rollback_failed",
                    subnet_id=subnet.subnet_id,
                    error=str(rollback_error),
                    exc_info=True,
                )
            raise HTTPException(
                status_code=500,
                detail="Failed to create subnet",
            ) from join_error

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
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"reason": str(e)},
        ) from e
    except ACNHTTPError:
        # P3 cross-module catch-all defence: ``ACNHTTPError`` is
        # ``Exception``-typed (not ``HTTPException``-typed); without
        # this re-raise, any caller-actionable 4xx raised inside the
        # try body would be silently rewritten as a sanitised 500.
        raise
    except HTTPException:
        # Mirror defence for legacy ``HTTPException`` raises — same
        # swallow risk via the catch-all below.
        raise
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
                raise ACNHTTPError(
                    ErrorCode.AUTHENTICATION_REQUIRED,
                    401,
                    message="Authentication required when filtering by owner.",
                    details={"reason": "owner_filter_requires_auth"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            payload = await verify_token(request, credentials)
            requester = payload.get("sub", "")
            permissions = payload.get("permissions", [])
            if requester != owner and "acn:admin" not in permissions:
                raise ACNHTTPError(
                    ErrorCode.OWNERSHIP_MISMATCH,
                    403,
                    message="Cannot list subnets for another user.",
                    details={"requested_owner": owner, "token_owner": requester},
                )
            subnets = await subnet_service.list_subnets(owner=owner)
        else:
            subnets = await subnet_service.list_public_subnets()

        # Convert to SubnetInfo
        subnet_infos = [_subnet_entity_to_info(s) for s in subnets]

        return {"subnets": subnet_infos, "count": len(subnet_infos)}
    except ACNHTTPError:
        raise
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
            raise ACNHTTPError(
                ErrorCode.AUTHENTICATION_REQUIRED,
                401,
                message="Authentication required to view private subnet members.",
                details={"subnet_id": subnet_id, "reason": "private_subnet"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        payload = await verify_token(request, credentials)
        requester = payload.get("sub", "")
        permissions = payload.get("permissions", [])
        if requester != subnet.owner and "acn:admin" not in permissions:
            raise ACNHTTPError(
                ErrorCode.NOT_SUBNET_MEMBER,
                403,
                message="Access denied: private subnet.",
                details={"subnet_id": subnet_id, "agent_id": requester},
            )

    try:
        agents = await agent_service.search_agents(subnet_id=subnet_id)

        # Convert to AgentInfo
        from .registry import _agent_entity_to_info

        agent_infos = [_agent_entity_to_info(a, strip_sensitive=True) for a in agents]

        return {"subnet_id": subnet_id, "agents": agent_infos, "count": len(agent_infos)}
    except ACNHTTPError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_subnet_agents_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve subnet agents") from e


# ---------------------------------------------------------------------------
# Legacy agent-side membership endpoints.
#
# These three endpoints are *deprecated*: the canonical paths now live under
# `/api/v1/agents/{agent_id}/subnets/…` (see `routes/agent_subnets.py`). The
# old paths sit on the `subnets` router for accidental historical reasons —
# the `{agent_id}` segment is awkwardly nested under `/api/v1/subnets/…`,
# which produces the surprising shape `…/subnets/{agent_id}/subnets/{id}`.
#
# The handlers stay here to keep all existing callers working byte-for-byte,
# but every request is logged at warn level and OpenAPI marks them
# `deprecated`. Once telemetry shows zero traffic for ≥ one full release
# cycle, this entire block can be deleted.
#
# Behaviour is implemented once in `_subnet_membership.py`; both the legacy
# and canonical routes call the same helpers, so they cannot drift.
# ---------------------------------------------------------------------------


@router.post(
    "/{agent_id}/subnets/{subnet_id}",
    deprecated=True,
    summary="[Deprecated] Agent joins a subnet",
    description=(
        "**Deprecated.** Use `POST /api/v1/agents/{agent_id}/subnets/{subnet_id}` "
        "instead. This path will be removed after telemetry shows zero traffic "
        "for one full release cycle. Behaviour is identical."
    ),
)
async def join_subnet(
    agent_id: AgentIdPath,
    subnet_id: SubnetIdPath,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
    webhook_service: WebhookServiceDep = None,
):
    """[Deprecated] Use POST /api/v1/agents/{agent_id}/subnets/{subnet_id}."""
    logger.warning(
        "deprecated_route_called",
        path="/api/v1/subnets/{agent_id}/subnets/{subnet_id}",
        canonical="/api/v1/agents/{agent_id}/subnets/{subnet_id}",
        agent_id=agent_id,
    )
    return await do_join_subnet(
        agent_id=agent_id,
        subnet_id=subnet_id,
        agent_info=agent_info,
        subnet_service=subnet_service,
        agent_service=agent_service,
        webhook_service=webhook_service,
    )


@router.delete(
    "/{agent_id}/subnets/{subnet_id}",
    deprecated=True,
    summary="[Deprecated] Agent leaves a subnet",
    description=(
        "**Deprecated.** Use `DELETE /api/v1/agents/{agent_id}/subnets/{subnet_id}` "
        "instead. Behaviour is identical."
    ),
)
async def leave_subnet(
    agent_id: AgentIdPath,
    subnet_id: SubnetIdPath,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
    webhook_service: WebhookServiceDep = None,
):
    """[Deprecated] Use DELETE /api/v1/agents/{agent_id}/subnets/{subnet_id}."""
    logger.warning(
        "deprecated_route_called",
        path="/api/v1/subnets/{agent_id}/subnets/{subnet_id}",
        canonical="/api/v1/agents/{agent_id}/subnets/{subnet_id}",
        agent_id=agent_id,
    )
    return await do_leave_subnet(
        agent_id=agent_id,
        subnet_id=subnet_id,
        agent_info=agent_info,
        subnet_service=subnet_service,
        agent_service=agent_service,
        webhook_service=webhook_service,
    )


# ---------------------------------------------------------------------------
# Org Harness registration (pluggable webhook target per subnet)
# ---------------------------------------------------------------------------


class UpdateHarnessRequest(BaseModel):
    """Register or clear the Org Harness webhook for a subnet.

    Pass ``harness_url=null`` to unregister (delivery stops). When a non-null
    URL is registered, ACN POSTs lifecycle events (`agent.joined_subnet`,
    `agent.left_subnet`, `task.created`, `task.accepted`, `task.submitted`,
    `task.completed`, `task.cancelled`) to that URL, HMAC-SHA256 signed with
    ``harness_secret`` (same scheme as payment webhooks). The secret is
    write-only — it is never returned by any GET endpoint.
    """

    harness_url: str | None = Field(
        default=None,
        max_length=500,
        description="External Org Harness webhook URL. Null clears registration.",
    )
    harness_secret: str | None = Field(
        default=None,
        max_length=256,
        description="HMAC-SHA256 secret. Null disables signing on outbound webhooks.",
    )


@router.patch("/{subnet_id}/harness")
async def update_subnet_harness(
    subnet_id: SubnetIdPath,
    body: UpdateHarnessRequest,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
):
    """Register or clear the Org Harness webhook for this subnet.

    Only the subnet owner may call this endpoint.
    """
    try:
        subnet = await subnet_service.update_harness(
            subnet_id=subnet_id,
            owner=agent_info["agent_id"],
            harness_url=body.harness_url,
            harness_secret=body.harness_secret,
        )
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"subnet_id": subnet_id},
        ) from e
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"subnet_id": subnet_id, "reason": str(e)},
        ) from e

    return {
        "status": "updated",
        "subnet_id": subnet.subnet_id,
        "harness_url": subnet.harness_url,
        "harness_registered": subnet.harness_url is not None,
    }


@router.get(
    "/{agent_id}/subnets",
    deprecated=True,
    summary="[Deprecated] Get subnets an agent belongs to",
    description=(
        "**Deprecated.** Use `GET /api/v1/agents/{agent_id}/subnets` instead. "
        "Behaviour is identical."
    ),
)
async def get_agent_subnets(
    agent_id: AgentIdPath,
    agent_info: AgentApiKeyDep,
    agent_service: AgentServiceDep = None,
):
    """[Deprecated] Use GET /api/v1/agents/{agent_id}/subnets."""
    logger.warning(
        "deprecated_route_called",
        path="/api/v1/subnets/{agent_id}/subnets",
        canonical="/api/v1/agents/{agent_id}/subnets",
        agent_id=agent_id,
    )
    return await do_get_agent_subnets(
        agent_id=agent_id,
        agent_info=agent_info,
        agent_service=agent_service,
    )


@router.delete("/{subnet_id}")
@limiter.limit("10/minute")
async def delete_subnet(
    request: Request,
    subnet_id: SubnetIdPath,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
):
    """Delete a subnet (requires Agent API Key — only the owning agent can delete).

    Clean Architecture: Route → SubnetService → Repository
    """
    owner = agent_info["agent_id"]

    try:
        success = await subnet_service.delete_subnet(subnet_id, owner)
        if success:
            logger.info("subnet_deleted", subnet_id=subnet_id, owner=owner)
            return {"status": "deleted", "subnet_id": subnet_id}
        else:
            # This in-try raise is now correctly propagated thanks to the
            # ``except HTTPException: raise`` defence below (added by the
            # P3 cross-module catch-all defence sweep). Pre-defence, the
            # 404 was silently rewritten to 500 by the catch-all — that
            # latent bug is now fixed. Future migration of this site to
            # ``ACNHTTPError`` is also safe (the matching
            # ``except ACNHTTPError: raise`` line is in place).
            raise HTTPException(status_code=404, detail="Subnet not found")
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"subnet_id": subnet_id},
        ) from e
    except PermissionError as e:
        logger.warning("delete_subnet_permission_denied", subnet_id=subnet_id, error=str(e))
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"subnet_id": subnet_id, "reason": str(e)},
        ) from e
    except ACNHTTPError:
        raise
    except HTTPException:
        # P3 cross-module catch-all defence ALSO repairs the pre-existing
        # latent bug at the ``else: raise HTTPException(404, "Subnet not
        # found")`` branch above (line ≈398): without this re-raise the
        # 404 was silently rewritten to 500 by the catch-all below. The
        # 404 raise path is now correctly propagated.
        raise
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
    except ACNHTTPError:
        raise
    except HTTPException:
        raise
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
    except ACNHTTPError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error("admin_remove_subnet_member_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to remove subnet member") from e
