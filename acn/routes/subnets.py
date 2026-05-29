"""Subnet Management API Routes

Clean Architecture implementation: Route → Service → Repository
"""

import re
import secrets
from typing import Literal

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from ..auth.middleware import require_internal_or_permission, verify_token
from ..config import get_settings
from ..core.errors import ACN_DEFAULT_RESPONSES, ACNHTTPError, ErrorCode
from ..core.exceptions import SubnetNotFoundException
from ..models import SubnetCreateRequest, SubnetCreateResponse, SubnetInfo, SubnetStub
from ..monitoring import AuditEventType, fire_and_forget_event, get_audit_singleton
from ..security import SSRFViolation, validate_endpoint_url
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


def _subnet_entity_to_stub(subnet, *, parent_uuid: str | None = None) -> SubnetStub:
    """Convert a private Subnet entity to a SubnetStub.

    Exposes only non-identifying fields:
    - opaque UUID (no slug / name)
    - structural metadata: parent UUID, lifecycle, linked_task_id
    - aggregate counts: member_count (headcount, not identities)
    - temporal context: created_year_month (YYYY-MM, not exact timestamp)
    - infrastructure flag: harness_registered (bool only — harness_url hidden)
    """
    created_at = getattr(subnet, "created_at", None)
    created_year_month: str | None = None
    if created_at is not None:
        try:
            created_year_month = created_at.strftime("%Y-%m")
        except AttributeError:
            # Fallback for string-typed timestamps (test fixtures, legacy rows)
            if isinstance(created_at, str) and len(created_at) >= 7:
                created_year_month = created_at[:7]
    member_count: int | None = None
    try:
        member_count = subnet.get_member_count()
    except Exception:  # noqa: BLE001
        pass
    return SubnetStub(
        id=_coerce_optional_str(getattr(subnet, "id", None)) or subnet.slug,
        is_private=True,
        parent_id=parent_uuid,
        lifecycle=_coerce_lifecycle(getattr(subnet, "lifecycle", "persistent")),
        member_count=member_count,
        created_year_month=created_year_month,
        harness_registered=bool(getattr(subnet, "harness_url", None)),
        linked_task_id=_coerce_optional_str(getattr(subnet, "linked_task_id", None)),
    )


def _subnet_entity_to_info(subnet, *, parent_uuid: str | None = None) -> SubnetInfo:
    """Convert Subnet entity to SubnetInfo model.

    ADR-0003 hierarchy fields:
    - ``parent_id`` carries the parent subnet's **opaque UUID** (ACL V6
      B6). The human-readable ``parent_slug`` slug is intentionally
      suppressed to prevent a public child subnet from leaking its
      private parent's naming convention to anonymous callers. Callers
      pass the resolved UUID via the ``parent_uuid`` keyword argument;
      the route is responsible for resolving slug → UUID before calling
      this function.
    - ``lifecycle`` / ``linked_task_id`` are surfaced as-is.
    - ``harness_secret`` stays write-only — never exposed.
    """
    return SubnetInfo(
        slug=subnet.slug,
        # Defensive ``getattr``: legacy ``MagicMock`` test stubs that
        # predate the ``id`` field would otherwise return a fresh mock
        # object instead of a string. Falls back to ``subnet.slug``
        # so the response is always well-formed; production entities
        # always carry the real UUID.
        id=_coerce_optional_str(getattr(subnet, "id", None)) or subnet.slug,
        name=subnet.name,
        owner=subnet.owner,
        description=subnet.description,
        is_private=subnet.is_private,
        security_config=subnet.security_config,
        created_at=subnet.created_at,
        metadata=subnet.metadata,
        harness_url=subnet.harness_url,
        harness_registered=subnet.harness_url is not None,
        # parent_slug (slug) is always None in API responses — ACL V6 B6.
        parent_slug=None,
        parent_id=parent_uuid,
        lifecycle=_coerce_lifecycle(getattr(subnet, "lifecycle", "persistent")),
        linked_task_id=_coerce_optional_str(
            getattr(subnet, "linked_task_id", None)
        ),
    )


async def _resolve_parent_uuid(
    subnet,
    subnet_service,
) -> str | None:
    """Return the parent subnet's opaque UUID, or None if top-level / orphaned.

    Resolves the slug stored in ``subnet.parent_slug`` to the stable
    UUID needed by ``_subnet_entity_to_info`` (ACL V6 B6).  Silently
    degrades to ``None`` on lookup failure so orphaned references don't
    surface a 500 to callers.
    """
    parent_slug = _coerce_optional_str(getattr(subnet, "parent_slug", None))
    if not parent_slug:
        return None
    try:
        parent_entity = await subnet_service.get_subnet(parent_slug)
        return _coerce_optional_str(getattr(parent_entity, "id", None))
    except Exception:  # noqa: BLE001  # SubnetNotFound or any infra error
        return None


async def _resolve_caller_access(
    payload: dict,
    subnet,
    agent_service,
) -> bool:
    """Return True when the caller is entitled to see full SubnetInfo.

    Implements the V6 ownership-chain bridge (ACL V6 / issue #114 B2):

    - ``acn:admin`` always gets full access.
    - Agent API key callers (``type == "agent"``): full access when
      ``sub == subnet.owner`` OR ``sub ∈ subnet.member_agent_ids``.
    - User JWT callers (``type == "user"``): full access when they own
      the subnet's owning agent — i.e. when ``subnet.owner`` is in the
      set of agent-ids owned by ``sub`` (the ownership-chain bridge).
      Owning a *member* agent only is NOT sufficient; membership is an
      employment relationship and does not extend read trust upward to
      the agent's human holder (see V6 contract § 2 ownership edge).

    Called ONLY for private subnets; public subnets short-circuit to
    full SubnetInfo before this function is reached.
    """
    permissions = payload.get("permissions", [])
    if "acn:admin" in permissions:
        return True

    sub = payload.get("sub", "")
    caller_type = payload.get("type", "user")

    if caller_type == "agent":
        member_ids: set = getattr(subnet, "member_agent_ids", None) or set()
        return sub == subnet.owner or sub in member_ids

    # User JWT path — ownership-chain bridge.
    try:
        owned_agents = await agent_service.find_by_owner(sub)
        owned_agent_ids = {a.agent_id for a in owned_agents}
    except Exception:  # noqa: BLE001  # any infra failure → deny
        return False
    return subnet.owner in owned_agent_ids


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
    """Generate a compact, unique slug from a human-readable name.

    Format: ``subnet-{slug}-{rand6}`` where slug is a lowercased, hyphen-
    delimited form of ``name`` truncated to 32 chars. Total length is
    bounded by ``len("subnet-") + 32 + 1 + 6 = 46`` — comfortably inside
    ``SubnetCreateRequest.slug``'s ``max_length=64``.
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
        slug = body.slug or _generate_subnet_id(body.name or "subnet")
        # ``name`` is optional in SubnetCreateRequest; default to slug so
        # the entity-layer invariant (name != empty) is always satisfied.
        name = body.name or slug
        subnet = await subnet_service.create_subnet(
            slug=slug,
            name=name,
            owner=owner,
            description=body.description,
            is_private=body.is_private,
            security_config=security_cfg,
            metadata={},
            # ADR-0003 nesting fields — service-layer validates the
            # five invariant variants and raises ``SubnetInvariantError``
            # with a stable ``reason`` string.
            parent_slug=body.parent_slug,
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
            await agent_service.join_subnet(owner, subnet.slug)
        except Exception as join_error:  # noqa: BLE001 - rollback path
            logger.error(
                "subnet_owner_join_failed_rolling_back",
                slug=subnet.slug,
                owner=owner,
                error=str(join_error),
                exc_info=True,
            )
            try:
                await subnet_service.delete_subnet(subnet.slug, owner)
            except Exception as rollback_error:  # noqa: BLE001
                logger.error(
                    "subnet_creation_rollback_failed",
                    slug=subnet.slug,
                    error=str(rollback_error),
                    exc_info=True,
                )
            raise HTTPException(
                status_code=500,
                detail="Failed to create subnet",
            ) from join_error

        # Generate gateway URLs
        base_url = settings.gateway_base_url or f"http://localhost:{settings.port}"
        gateway_a2a_url = f"{base_url}/gateway/a2a/{subnet.slug}"
        gateway_ws_url = f"{base_url}/gateway/ws/{subnet.slug}"

        logger.info("subnet_created", slug=subnet.slug, owner=owner)
        fire_and_forget_event(
            get_audit_singleton(),
            event_type=AuditEventType.SUBNET_CREATED,
            actor_id=owner,
            actor_type="agent",
            target_id=subnet.slug,
            target_type="subnet",
            details={
                "is_private": subnet.is_private,
                "join_policy": subnet.join_policy,
                "public_broadcast_eligible": not subnet.is_private,
            },
        )

        return SubnetCreateResponse(
            status="created",
            slug=subnet.slug,
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
            details={"reason": "invalid_request"},
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
    owned_by_user: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000, description="Max subnets to return."),
    offset: int = Query(default=0, ge=0, description="Zero-based offset for pagination."),
    credentials: HTTPAuthorizationCredentials | None = Security(_optional_bearer),
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
):
    """List all subnets.

    Clean Architecture: Route → SubnetService → Repository.

    Filters:
    - ``?owner=`` filters by ownership (agent-id semantics). Requires
      authentication; the caller must be that owner (or hold
      ``acn:admin``).
    - ``?owned_by_user=<user_sub>`` filters to subnets owned by any
      agent that the specified user owns (ACL V6 B7). Requires
      authentication; ``payload["sub"]`` must equal the supplied
      ``user_sub`` (or caller holds ``acn:admin``). Mismatch → 403.
      All returned rows are full ``SubnetInfo`` — the ownership filter
      already implies full access via the ownership-chain bridge.
    - ``?parent=`` filters to immediate children of the given parent
      subnet (ADR-0003). Visibility is **aligned with the rest of
      this endpoint** — anonymous callers see only public children,
      and authenticated callers additionally see private children
      they own or are members of. Returns an empty list when the
      parent is unknown or has no visible children — cross-tenant
      probes get the same shape as legitimate empty results
      (no existence leak).

    Per-row caller-aware rendering (ACL V6 B5): each row in the result
    set is independently graded by the V6 privacy matrix. Public subnets
    return full ``SubnetInfo``; private subnets return ``SubnetStub``
    unless the caller is authorised (owner agent, member agent, user JWT
    holding ownership-chain access via the ownership-chain bridge, or
    admin).
    """
    try:
        # Resolve caller payload once for per-row ACL.
        caller_payload: dict | None = None
        if credentials:
            try:
                caller_payload = await verify_token(request, credentials)
            except Exception:  # noqa: BLE001  # invalid token → treat as anon
                caller_payload = None

        if parent is not None:
            # ``?parent=`` filter takes precedence and gates on the
            # same identity primitive used by ``list_children``.
            requester_id: str | None = (
                caller_payload.get("sub") or None if caller_payload else None
            )
            subnets = await subnet_service.list_children(
                parent_slug=parent,
                requester_id=requester_id,
            )
        elif owned_by_user is not None:
            # ACL V6 B7 — user-centric ownership filter.
            # Requires auth; caller's sub must match the supplied user_sub
            # (or caller holds acn:admin) to prevent cross-tenant probing.
            if not credentials or caller_payload is None:
                raise ACNHTTPError(
                    ErrorCode.AUTHENTICATION_REQUIRED,
                    401,
                    message="Authentication required when filtering by owned_by_user.",
                    details={"reason": "owned_by_user_requires_auth"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            caller_sub = caller_payload.get("sub", "")
            permissions = caller_payload.get("permissions", [])
            if caller_sub != owned_by_user and "acn:admin" not in permissions:
                raise ACNHTTPError(
                    ErrorCode.OWNERSHIP_MISMATCH,
                    403,
                    message="Cannot list subnets owned by another user.",
                    details={"reason": "ownership_mismatch"},
                )
            # Resolve the user's owned agents, then query subnets by that
            # bounded owner set — avoids the O(N) full-table scan that
            # ``list_subnets(owner=None)`` + in-memory filter would impose.
            owned_agents = await agent_service.find_by_owner(owned_by_user)
            owned_agent_ids = {a.agent_id for a in owned_agents}
            subnets = await subnet_service.list_subnets_by_owners(owned_agent_ids)
        elif owner:
            # Require auth when filtering by owner to prevent private subnet enumeration
            if not credentials or caller_payload is None:
                raise ACNHTTPError(
                    ErrorCode.AUTHENTICATION_REQUIRED,
                    401,
                    message="Authentication required when filtering by owner.",
                    details={"reason": "owner_filter_requires_auth"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            requester = caller_payload.get("sub", "")
            permissions = caller_payload.get("permissions", [])
            if requester != owner and "acn:admin" not in permissions:
                raise ACNHTTPError(
                    ErrorCode.OWNERSHIP_MISMATCH,
                    403,
                    message="Cannot list subnets for another user.",
                    details={"requested_owner": owner, "token_owner": requester},
                )
            subnets = await subnet_service.list_subnets(owner=owner)
        else:
            # Return all subnets; private ones are downgraded to SubnetStub
            # per-row below (ACL V6 B5 caller-aware rendering).
            subnets = await subnet_service.list_subnets()

        # Apply pagination before the per-row rendering pass (bounds peak work).
        total = len(subnets)
        subnets_page = subnets[offset : offset + limit]

        # Batch-resolve all parent UUIDs in a single pass to avoid N+1 queries.
        parent_ids_needed = {
            getattr(s, "parent_slug", None)
            for s in subnets_page
            if getattr(s, "parent_slug", None)
        }
        parent_uuid_map: dict[str, str | None] = {}
        for pid in parent_ids_needed:
            try:
                parent_obj = await subnet_service.get_subnet(pid)
                parent_uuid_map[pid] = _coerce_optional_str(getattr(parent_obj, "id", None))
            except Exception:  # noqa: BLE001
                parent_uuid_map[pid] = None

        def _get_parent_uuid(s) -> str | None:
            pid = getattr(s, "parent_slug", None)
            if not pid:
                return None
            return parent_uuid_map.get(pid)

        # Per-row caller-aware rendering (ACL V6 B5).
        # Exception: when ?owned_by_user= is set, all rows are already
        # confirmed as owned-by-user-via-agent (ownership-chain bridge)
        # so they always receive full SubnetInfo regardless of is_private.
        full_access_all_rows = owned_by_user is not None
        subnet_infos: list = []
        for s in subnets_page:
            parent_uuid = _get_parent_uuid(s)
            if full_access_all_rows or not getattr(s, "is_private", False):
                subnet_infos.append(_subnet_entity_to_info(s, parent_uuid=parent_uuid))
            elif caller_payload and await _resolve_caller_access(
                caller_payload, s, agent_service
            ):
                subnet_infos.append(_subnet_entity_to_info(s, parent_uuid=parent_uuid))
            else:
                # Private subnet the caller cannot see in full → SubnetStub.
                subnet_infos.append(_subnet_entity_to_stub(s, parent_uuid=parent_uuid))

        return {"subnets": subnet_infos, "count": len(subnet_infos), "total": total, "has_more": offset + limit < total}
    except ACNHTTPError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_subnets_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list subnets") from e


@router.get("/{slug}")
async def get_subnet(
    slug: SubnetIdPath,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_optional_bearer),
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
):
    """Get subnet details.

    Clean Architecture: Route → SubnetService → Repository.

    Privacy contract (ACL V6 / issue #114)
        Public subnets are fully visible to anyone.

        Private subnets use a two-tier response:

        * **Authorised callers** — owner agent (API key), member agent
          (API key), the owner agent's human holder via ownership-chain
          bridge (user JWT), or ``acn:admin`` — receive the full
          ``SubnetInfo`` payload.

        * **Everyone else** (anonymous, or authenticated but unrelated)
          receives a ``SubnetStub`` — opaque UUID + structural metadata
          only.  The human-readable slug, ``name``, ``owner``,
          ``description``, ``harness_url``, and ``security_schemes`` are
          omitted.

        Rationale: a private subnet's ``slug`` is already
        discoverable through public agent listings, so existence-hiding
        provides no real security.  Surfacing hierarchy metadata lets
        graph clients draw correct topology without leaking sensitive
        details.

        Ownership-chain bridge (ACL V6 B11): a user JWT caller that owns
        the subnet's **owner agent** gets full access (A2 principle —
        humans need read-only knowledge of their agents' activities).
        Owning only a *member* agent is **not** sufficient — membership
        is a collaboration relationship that does not extend read trust
        upward to the member agent's human holder (V6 contract § 2,
        ownership edge vs. membership edge distinction). Humans wishing
        to see subnets their agents are members of — but do not own —
        must use their agent's own API key (``GET /agents/me`` pattern).

        Genuinely missing subnets still return ``SUBNET_NOT_FOUND``
        (404).
    """
    try:
        subnet = await subnet_service.get_subnet(slug)
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"slug": slug},
        ) from e

    # Resolve parent UUID once — used by both SubnetStub and SubnetInfo
    # (ACL V6 B6: slug is never exposed in API responses).
    parent_uuid = await _resolve_parent_uuid(subnet, subnet_service)

    if not getattr(subnet, "is_private", False):
        return _subnet_entity_to_info(subnet, parent_uuid=parent_uuid)

    # Private subnet — check authorisation.
    # Unauthenticated or unauthorised callers receive a SubnetStub
    # carrying only the **opaque UUID** plus structural metadata.
    # The human-readable slug slug is intentionally NOT exposed:
    # naming patterns like ``acnlabs-core`` would otherwise leak
    # organisational structure to anyone who happens to know an
    # agent_id that's a member.
    def _stub() -> SubnetStub:
        return _subnet_entity_to_stub(subnet, parent_uuid=parent_uuid)

    if not credentials:
        return _stub()

    # Align with list_subnets: invalid token → treat as anonymous → Stub.
    try:
        payload = await verify_token(request, credentials)
    except Exception:  # noqa: BLE001
        return _stub()

    if not await _resolve_caller_access(payload, subnet, agent_service):
        return _stub()

    return _subnet_entity_to_info(subnet, parent_uuid=parent_uuid)


@router.get("/{slug}/children")
async def get_subnet_children(
    slug: SubnetIdPath,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_optional_bearer),
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
):
    """List immediate children of a subnet (ADR-0003).

    Visibility matches ``GET /api/v1/subnets?parent=<id>``: anonymous
    callers see public children in full; private children are returned
    as ``SubnetStub`` unless the caller is authorised (V6 B5 per-row
    caller-aware rendering, same logic as ``list_subnets``).

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
        await subnet_service.get_subnet(slug)
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"slug": slug},
        ) from e

    try:
        caller_payload: dict | None = None
        if credentials:
            try:
                caller_payload = await verify_token(request, credentials)
            except Exception:  # noqa: BLE001  # invalid token → treat as anon
                caller_payload = None

        requester_id: str | None = (
            caller_payload.get("sub") or None if caller_payload else None
        )
        children = await subnet_service.list_children(
            parent_slug=slug,
            requester_id=requester_id,
        )

        # Per-row caller-aware rendering (ACL V6 B5) — same logic as list_subnets.
        subnet_infos: list = []
        for s in children:
            if not getattr(s, "is_private", False):
                parent_uuid = await _resolve_parent_uuid(s, subnet_service)
                subnet_infos.append(_subnet_entity_to_info(s, parent_uuid=parent_uuid))
            elif caller_payload and await _resolve_caller_access(
                caller_payload, s, agent_service
            ):
                parent_uuid = await _resolve_parent_uuid(s, subnet_service)
                subnet_infos.append(_subnet_entity_to_info(s, parent_uuid=parent_uuid))
            else:
                parent_uuid = await _resolve_parent_uuid(s, subnet_service)
                subnet_infos.append(_subnet_entity_to_stub(s, parent_uuid=parent_uuid))

        return {"count": len(subnet_infos), "subnets": subnet_infos}
    except ACNHTTPError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "get_subnet_children_failed",
            slug=slug,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail="Failed to list subnet children"
        ) from e


@router.post("/{slug}/promote")
@limiter.limit("10/minute")
async def promote_subnet(
    request: Request,
    slug: SubnetIdPath,
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
            slug=slug,
            owner=owner,
        )
        return _subnet_entity_to_info(subnet)
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"slug": slug},
        ) from e
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"slug": slug, "reason": "owner_mismatch"},
        ) from e
    except ACNHTTPError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "promote_subnet_failed",
            slug=slug,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Failed to promote subnet") from e


@router.get("/{slug}/agents")
async def get_subnet_agents(
    slug: SubnetIdPath,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_optional_bearer),
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
):
    """Get all agents in a subnet.

    Clean Architecture: Route → Service → Repository.

    Privacy contract (ACL V6 / issue #114 B12):

    * **Public subnets**: full member list for all callers.
    * **Private subnets** — authorised callers (owner agent, member
      agent, user JWT with ownership-chain access to the owning agent,
      or ``acn:admin``) receive the full member list.
    * **Everyone else** on a private subnet: ``200`` with an empty
      ``agents`` list — identical in shape to a legitimate empty subnet
      so that unauthorised callers cannot determine whether the subnet
      has any members (existence-hiding contract, same approach as
      ``list_children`` stubs).  No ``401`` / ``403`` is returned for
      private-subnet membership queries.
    """
    try:
        subnet = await subnet_service.get_subnet(slug)
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"slug": slug},
        ) from e

    # For private subnets, check caller access.  For public subnets this
    # block is skipped entirely — the member list is openly visible.
    _empty_response = {"slug": slug, "agents": [], "count": 0}
    if getattr(subnet, "is_private", False):
        if not credentials:
            return _empty_response
        try:
            payload = await verify_token(request, credentials)
        except Exception:  # noqa: BLE001  # invalid token → empty list
            return _empty_response
        if not await _resolve_caller_access(payload, subnet, agent_service):
            return _empty_response

    try:
        agents = await agent_service.search_agents(slug=slug)

        from .registry import _agent_entities_to_infos

        agent_infos = await _agent_entities_to_infos(
            agents, agent_service=agent_service, strip_sensitive=True
        )

        return {"slug": slug, "agents": agent_infos, "count": len(agent_infos)}
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
    "/{agent_id}/subnets/{slug}",
    deprecated=True,
    summary="[Deprecated] Agent joins a subnet",
    description=(
        "**Deprecated.** Use `POST /api/v1/agents/{agent_id}/subnets/{slug}` "
        "instead. This path will be removed after telemetry shows zero traffic "
        "for one full release cycle. Behaviour is identical."
    ),
)
@limiter.limit("30/minute")
async def join_subnet(
    request: Request,
    agent_id: AgentIdPath,
    slug: SubnetIdPath,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
    webhook_service: WebhookServiceDep = None,
    join_flow_service: JoinFlowServiceDep = None,
):
    """[Deprecated] Use POST /api/v1/agents/{agent_id}/subnets/{slug}."""
    logger.warning(
        "deprecated_route_called",
        path="/api/v1/subnets/{agent_id}/subnets/{slug}",
        canonical="/api/v1/agents/{agent_id}/subnets/{slug}",
        agent_id=agent_id,
    )
    return await do_join_subnet(
        agent_id=agent_id,
        slug=slug,
        agent_info=agent_info,
        subnet_service=subnet_service,
        agent_service=agent_service,
        webhook_service=webhook_service,
        join_flow_service=join_flow_service,
    )


@router.delete(
    "/{agent_id}/subnets/{slug}",
    deprecated=True,
    summary="[Deprecated] Agent leaves a subnet",
    description=(
        "**Deprecated.** Use `DELETE /api/v1/agents/{agent_id}/subnets/{slug}` "
        "instead. Behaviour is identical."
    ),
)
@limiter.limit("30/minute")
async def leave_subnet(
    request: Request,
    agent_id: AgentIdPath,
    slug: SubnetIdPath,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
    webhook_service: WebhookServiceDep = None,
):
    """[Deprecated] Use DELETE /api/v1/agents/{agent_id}/subnets/{slug}."""
    logger.warning(
        "deprecated_route_called",
        path="/api/v1/subnets/{agent_id}/subnets/{slug}",
        canonical="/api/v1/agents/{agent_id}/subnets/{slug}",
        agent_id=agent_id,
    )
    return await do_leave_subnet(
        agent_id=agent_id,
        slug=slug,
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

    @field_validator("harness_url")
    @classmethod
    def _validate_harness_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            validate_endpoint_url(v, allow_loopback=get_settings().dev_mode)
        except SSRFViolation as exc:
            raise ValueError("The provided harness URL is not allowed.") from exc
        return v


@router.patch("/{slug}/harness")
@limiter.limit("20/minute")
async def update_subnet_harness(
    request: Request,
    slug: SubnetIdPath,
    body: UpdateHarnessRequest,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
):
    """Register or clear the Org Harness webhook for this subnet.

    Only the subnet owner may call this endpoint.
    """
    try:
        subnet = await subnet_service.update_harness(
            slug=slug,
            owner=agent_info["agent_id"],
            harness_url=body.harness_url,
            harness_secret=body.harness_secret,
        )
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"slug": slug},
        ) from e
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"slug": slug, "reason": "owner_mismatch"},
        ) from e

    return {
        "status": "updated",
        "slug": subnet.slug,
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


class TransferOwnerRequest(BaseModel):
    new_owner: str = Field(..., min_length=1, description="Agent ID of the new subnet owner")


@router.post("/{slug}/transfer")
@limiter.limit("5/minute")
async def transfer_subnet_owner(
    request: Request,
    slug: str,
    body: TransferOwnerRequest,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep,
) -> dict:
    """Transfer ownership of a subnet to another agent.

    The caller must be the current owner. The new owner will be added to
    the subnet's member set automatically. See ADR-0005.
    """
    try:
        transferred = await subnet_service.transfer_owner(
            slug=slug,
            current_owner=agent_info["agent_id"],
            new_owner=body.new_owner,
        )
    except SubnetNotFoundException as exc:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            status_code=404,
            details={"slug": slug},
        ) from exc
    except PermissionError as exc:
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            status_code=403,
            details={"reason": str(exc)},
        ) from exc
    except ValueError as exc:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            status_code=400,
            details={"reason": str(exc)},
        ) from exc
    return {"slug": transferred.slug, "owner": transferred.owner}


@router.delete("/{slug}")
@limiter.limit("10/minute")
async def delete_subnet(
    request: Request,
    slug: SubnetIdPath,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
    confirm: bool = Query(
        default=False,
        description=(
            "Safety guard (ACL V6 B8): must be `true` to execute this "
            "destructive operation. Omitting it or passing `false` returns "
            "a 400 so accidental calls cannot silently delete subnets."
        ),
    ),
):
    """Delete a subnet (requires Agent API Key — only the owning agent can delete).

    **Destructive operation** — requires ``?confirm=true``.

    Clean Architecture: Route → SubnetService → Repository
    """
    if not confirm:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={
                "slug": slug,
                "hint": "Add ?confirm=true to confirm this destructive operation.",
            },
        )

    owner = agent_info["agent_id"]

    try:
        success = await subnet_service.delete_subnet(slug, owner)
        if success:
            logger.info("subnet_deleted", slug=slug, owner=owner)
            fire_and_forget_event(
                get_audit_singleton(),
                event_type=AuditEventType.SUBNET_DELETED,
                actor_id=owner,
                actor_type="agent",
                target_id=slug,
                target_type="subnet",
                details={"confirmed": True},
            )
            return {"status": "deleted", "slug": slug}
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
            details={"slug": slug},
        ) from e
    except PermissionError as e:
        logger.warning("delete_subnet_permission_denied", slug=slug, error=str(e))
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"slug": slug, "reason": "owner_mismatch"},
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


@router.post("/{slug}/members/{agent_id}", tags=["subnets-internal"])
async def admin_add_subnet_member(
    slug: SubnetIdPath,
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
        await subnet_service.add_member(slug, agent_id)
        logger.info("admin_subnet_member_added", slug=slug, agent_id=agent_id)
        return {"status": "added", "slug": slug, "agent_id": agent_id}
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"slug": slug},
        ) from e
    except SubnetInvariantError as e:
        # ADR-0003 child-subnet membership-subset rejection
        # propagated even through the internal admin path — keeps
        # the invariant uniformly enforced regardless of caller.
        raise _invariant_error_to_acn(
            e, extra_details={"slug": slug, "agent_id": agent_id}
        ) from e
    except ACNHTTPError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error("admin_add_subnet_member_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to add subnet member") from e


@router.delete("/{slug}/members/{agent_id}", tags=["subnets-internal"])
async def admin_remove_subnet_member(
    slug: SubnetIdPath,
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
        await subnet_service.remove_member(slug, agent_id)
        logger.info("admin_subnet_member_removed", slug=slug, agent_id=agent_id)
        return {"status": "removed", "slug": slug, "agent_id": agent_id}
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"slug": slug},
        ) from e
    except ACNHTTPError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error("admin_remove_subnet_member_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to remove subnet member") from e
