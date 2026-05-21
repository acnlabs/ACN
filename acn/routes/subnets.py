"""Subnet Management API Routes

Clean Architecture implementation: Route → Service → Repository
"""

import re
import secrets
from typing import Literal

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from ..auth.middleware import require_internal_or_permission, verify_token
from ..config import get_settings
from ..core.errors import ACN_DEFAULT_RESPONSES, ACNHTTPError, ErrorCode
from ..core.exceptions import SubnetNotFoundException
from ..models import SubnetCreateRequest, SubnetCreateResponse, SubnetInfo, SubnetStub
from ..services.subnet_service import SubnetInvariantError
from ._subnet_membership import (
    do_get_agent_subnets,
    do_join_subnet,
    do_leave_subnet,
)
from .dependencies import (  # type: ignore[import-untyped]
    AgentApiKeyDep,
    AgentIdPath,
    AgentServiceDep,
    JoinFlowServiceDep,
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


def _coerce_optional_str(value: object) -> str | None:
    """Return ``value`` if it's a ``str``, else ``None``.

    Boundary-layer coercion: lets ``_subnet_entity_to_info`` accept
    both real ``Subnet`` entities (where the field types are guaranteed)
    and legacy ``MagicMock``-based stub subnets used by older route
    tests (where any auto-generated attribute returns a new
    ``MagicMock`` rather than ``None``). The real entity dataclass
    defaults to ``None`` so this coercion is a no-op in production.
    """
    return value if isinstance(value, str) else None


def _coerce_lifecycle(value: object) -> Literal["persistent", "task_scoped"]:
    """Same intent as ``_coerce_optional_str`` for the lifecycle
    Literal. Defaults to ``"persistent"`` on any unexpected value.

    A real ``Subnet`` entity always carries one of the two valid
    strings (entity-layer ``__post_init__`` enforces it), so the
    only "unexpected" path in practice is a legacy ``MagicMock``
    stub returning a non-string auto-attribute. We silently degrade
    those, but emit a warning for unexpected real-string values
    so a future invalid persisted value surfaces in logs instead
    of disappearing silently.
    """
    if value == "task_scoped":
        return "task_scoped"
    if value == "persistent":
        return "persistent"
    if isinstance(value, str):
        logger.warning(
            "subnet_info_unexpected_lifecycle",
            lifecycle_value=value,
        )
    return "persistent"


def _subnet_entity_to_info(subnet) -> SubnetInfo:
    """Convert Subnet entity to SubnetInfo model.

    ADR-0003 surfaces ``parent_subnet_id`` / ``lifecycle`` /
    ``linked_task_id`` so consumers can render hierarchy / lifecycle
    UI hints. ``harness_secret`` stays write-only — never exposed
    through this conversion.
    """
    return SubnetInfo(
        subnet_id=subnet.subnet_id,
        # Defensive ``getattr``: legacy ``MagicMock`` test stubs that
        # predate the ``id`` field would otherwise return a fresh mock
        # object instead of a string. Falls back to ``subnet.subnet_id``
        # so the response is always well-formed; production entities
        # always carry the real UUID.
        id=_coerce_optional_str(getattr(subnet, "id", None)) or subnet.subnet_id,
        name=subnet.name,
        owner=subnet.owner,
        description=subnet.description,
        is_private=subnet.is_private,
        security_config=subnet.security_config,
        created_at=subnet.created_at,
        metadata=subnet.metadata,
        harness_url=subnet.harness_url,
        harness_registered=subnet.harness_url is not None,
        parent_subnet_id=_coerce_optional_str(
            getattr(subnet, "parent_subnet_id", None)
        ),
        lifecycle=_coerce_lifecycle(getattr(subnet, "lifecycle", "persistent")),
        linked_task_id=_coerce_optional_str(
            getattr(subnet, "linked_task_id", None)
        ),
    )


def _invariant_error_to_acn(
    exc: SubnetInvariantError,
    extra_details: dict | None = None,
) -> ACNHTTPError:
    """Map a service-layer ``SubnetInvariantError`` to the wire-format
    ``INVALID_REQUEST`` ACN error with the stable ``details.reason``
    string the route contract tests pin.

    Membership-subset rejection (``not_parent_member``) is the one
    case we surface as ``NOT_SUBNET_MEMBER`` instead — it's a
    membership rather than a request-shape problem. Routes that
    might raise it should call this helper with that variant set.
    """
    from ..services.subnet_service import REASON_NOT_PARENT_MEMBER

    details = {"reason": exc.reason}
    if extra_details:
        details.update(extra_details)
    if exc.reason == REASON_NOT_PARENT_MEMBER:
        return ACNHTTPError(
            ErrorCode.NOT_SUBNET_MEMBER, 403, details=details
        )
    return ACNHTTPError(ErrorCode.INVALID_REQUEST, 400, details=details)


# Deprecated alias kept for one release cycle. Out-of-tree callers
# importing ``_nesting_error_to_acn`` keep working; new code should
# use ``_invariant_error_to_acn`` directly.
_nesting_error_to_acn = _invariant_error_to_acn


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
            # ADR-0003 nesting fields — service-layer validates the
            # five invariant variants and raises ``SubnetInvariantError``
            # with a stable ``reason`` string.
            parent_subnet_id=body.parent_subnet_id,
            lifecycle=body.lifecycle,
            linked_task_id=body.linked_task_id,
            # ADR-0004 admission policy. ``None`` (the default) lets
            # the service infer from ``is_private``; explicit values
            # go straight through and the
            # ``visibility_policy_conflict`` rejection surfaces via
            # ``SubnetInvariantError`` → ``_invariant_error_to_acn``.
            join_policy=body.join_policy,
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
            # Echo back the effective policy so callers that omitted
            # ``join_policy`` in the request can see what the service
            # inferred (ADR-0004).
            join_policy=subnet.join_policy,
        )
    except SubnetInvariantError as e:
        # ADR-0003 invariant rejection — surface with the stable
        # ``details.reason`` token clients (route contract tests,
        # CLI / SDK error parsers) pin against.
        raise _invariant_error_to_acn(e) from e
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
    parent: str | None = None,
    credentials: HTTPAuthorizationCredentials | None = Security(_optional_bearer),
    subnet_service: SubnetServiceDep = None,
):
    """List all subnets.

    Clean Architecture: Route → SubnetService → Repository.

    Filters:
    - ``?owner=`` filters by ownership. Requires authentication; the
      caller must be that owner (or hold ``acn:admin``).
    - ``?parent=`` filters to immediate children of the given parent
      subnet (ADR-0003). Visibility is **aligned with the rest of
      this endpoint** — anonymous callers see only public children,
      and authenticated callers additionally see private children
      they own or are members of. Returns an empty list when the
      parent is unknown or has no visible children — cross-tenant
      probes get the same shape as legitimate empty results
      (no existence leak).
    """
    try:
        if parent is not None:
            # ``?parent=`` filter takes precedence and gates on the
            # same identity primitive used by ``list_children``.
            requester_id: str | None = None
            if credentials:
                payload = await verify_token(request, credentials)
                requester_id = payload.get("sub") or None
            subnets = await subnet_service.list_children(
                parent_subnet_id=parent,
                requester_id=requester_id,
            )
        elif owner:
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
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_optional_bearer),
    subnet_service: SubnetServiceDep = None,
):
    """Get subnet details.

    Clean Architecture: Route → SubnetService → Repository.

    Privacy contract
        Public subnets are fully visible to anyone.

        Private subnets use a two-tier response:

        * **Authorised callers** (owner, current member, ``acn:admin``)
          receive the full ``SubnetInfo`` payload including ``harness_url``
          and ``metadata``.

        * **Everyone else** (anonymous or authenticated non-member)
          receives a ``SubnetStub`` — structural metadata only
          (``subnet_id``, ``name``, ``is_private``, ``parent_subnet_id``,
          ``lifecycle``).  Sensitive fields (``owner``, ``description``,
          ``harness_url``, ``security_schemes``, ``metadata``) are omitted.

        Rationale: a private subnet's ``subnet_id`` is already discoverable
        through any public agent's ``subnet_ids`` field, so existence-hiding
        provides no real security.  Surfacing hierarchy metadata lets graph
        clients draw correct topology without leaking sensitive details.

        Genuinely missing subnets still return ``SUBNET_NOT_FOUND`` (404).
    """
    try:
        subnet = await subnet_service.get_subnet(subnet_id)
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"subnet_id": subnet_id},
        ) from e

    if not getattr(subnet, "is_private", False):
        return _subnet_entity_to_info(subnet)

    # Private subnet — check authorisation.
    # Unauthenticated or unauthorised callers receive a SubnetStub
    # carrying only the **opaque UUID** plus structural metadata.
    # The human-readable subnet_id slug is intentionally NOT exposed:
    # naming patterns like ``acnlabs-core`` would otherwise leak
    # organisational structure to anyone who happens to know an
    # agent_id that's a member.
    #
    # ``parent_id`` is the parent subnet's UUID (resolved from
    # ``subnet.parent_subnet_id`` slug). Frontend graph clients
    # join hierarchy edges on the UUID across SubnetInfo / SubnetStub.
    parent_slug = _coerce_optional_str(
        getattr(subnet, "parent_subnet_id", None)
    )
    parent_uuid: str | None = None
    if parent_slug:
        try:
            parent_entity = await subnet_service.get_subnet(parent_slug)
            parent_uuid = _coerce_optional_str(getattr(parent_entity, "id", None))
        except SubnetNotFoundException:
            # Orphaned reference — surface as if top-level rather than
            # leaking a 500 to anonymous callers.
            parent_uuid = None

    def _stub() -> SubnetStub:
        return SubnetStub(
            id=_coerce_optional_str(getattr(subnet, "id", None)) or subnet.subnet_id,
            is_private=True,
            parent_id=parent_uuid,
            lifecycle=_coerce_lifecycle(getattr(subnet, "lifecycle", "persistent")),
        )

    if not credentials:
        return _stub()

    payload = await verify_token(request, credentials)
    requester = payload.get("sub", "")
    permissions = payload.get("permissions", [])

    is_owner = requester == subnet.owner
    is_admin = "acn:admin" in permissions
    is_member = requester in (getattr(subnet, "member_agent_ids", None) or set())

    if not (is_owner or is_admin or is_member):
        return _stub()

    return _subnet_entity_to_info(subnet)


@router.get("/{subnet_id}/children")
async def get_subnet_children(
    subnet_id: SubnetIdPath,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_optional_bearer),
    subnet_service: SubnetServiceDep = None,
):
    """List immediate children of a subnet (ADR-0003).

    Visibility matches ``GET /api/v1/subnets?parent=<id>``: anonymous
    callers see only public children; authenticated callers
    additionally see private children they own or are members of.

    Returns ``SUBNET_NOT_FOUND`` if the parent itself does not exist.
    Cross-tenant probes against an existing-but-not-visible parent
    surface ``SUBNET_NOT_FOUND`` only when the parent itself is
    missing — visible parents with zero authorised children return
    ``{"count": 0, "subnets": []}`` (no enumeration of who-has-children
    that the caller isn't entitled to see).
    """
    try:
        # Verify the parent itself exists so callers don't silently
        # paper over typos. Service ACL still filters the result set.
        await subnet_service.get_subnet(subnet_id)
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"subnet_id": subnet_id},
        ) from e

    try:
        requester_id: str | None = None
        if credentials:
            payload = await verify_token(request, credentials)
            requester_id = payload.get("sub") or None
        children = await subnet_service.list_children(
            parent_subnet_id=subnet_id,
            requester_id=requester_id,
        )
        subnet_infos = [_subnet_entity_to_info(s) for s in children]
        return {"count": len(subnet_infos), "subnets": subnet_infos}
    except ACNHTTPError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "get_subnet_children_failed",
            subnet_id=subnet_id,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail="Failed to list subnet children"
        ) from e


@router.post("/{subnet_id}/promote")
@limiter.limit("10/minute")
async def promote_subnet(
    request: Request,
    subnet_id: SubnetIdPath,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
):
    """Promote a ``task_scoped`` subnet to ``persistent`` (ADR-0003).

    Owner-only. Idempotent — promoting an already-persistent subnet
    returns its current state without modification. Side effects on
    a task-scoped → persistent flip:

    - ``lifecycle`` ← ``"persistent"``
    - ``linked_task_id`` ← ``None`` (subnet outlives its origin task)

    Per ADR-0003 semantic decision #4 the owner is *not* required
    to currently be a member of the parent subnet; promote is a
    pure field flip gated only by owner ACL.
    """
    owner = agent_info["agent_id"]
    try:
        subnet = await subnet_service.promote_to_persistent(
            subnet_id=subnet_id,
            owner=owner,
        )
        return _subnet_entity_to_info(subnet)
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
    except ACNHTTPError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "promote_subnet_failed",
            subnet_id=subnet_id,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Failed to promote subnet") from e


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

        from .registry import _agent_entities_to_infos

        agent_infos = await _agent_entities_to_infos(
            agents, agent_service=agent_service, strip_sensitive=True
        )

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
    join_flow_service: JoinFlowServiceDep = None,
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
        join_flow_service=join_flow_service,
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
    except SubnetInvariantError as e:
        # ADR-0003 child-subnet membership-subset rejection
        # propagated even through the internal admin path — keeps
        # the invariant uniformly enforced regardless of caller.
        raise _invariant_error_to_acn(
            e, extra_details={"subnet_id": subnet_id, "agent_id": agent_id}
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
