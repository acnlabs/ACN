"""Shared business logic for ADR-0004 Slice 2.3 subnet-admission routes.

The fourteen new admission endpoints (``POST /agents/{a}/subnets/{s}``
join entry, allowlist 3, join-request 4, invitation 5, agent
invitations 1) share three concerns:

1. **Authorization** — owner-only, invitee-only, and self-only gates
   resolve to canonical 403 surfaces with stable ``ErrorCode`` slugs.
2. **Error mapping** — the eight :class:`JoinFlowError` subclasses
   raised by the service layer translate to specific HTTP codes
   (404 / 409) per ADR §"HTTP status code conventions".
3. **Sealed-union dispatch** — :class:`JoinFlowResult` variants map to
   six distinct HTTP responses (five 200s on different "joined now"
   branches, one 202 on the pending fall-through). Owning the dispatch
   in one helper keeps every callable route layer thin.

Why a sibling of ``_subnet_membership.py`` rather than extending it
-------------------------------------------------------------------
``_subnet_membership.py`` is the pre-ADR-0004 helper for
``do_join_subnet`` / ``do_leave_subnet`` / ``do_get_agent_subnets``.
Slice 2.3 rewrites ``do_join_subnet`` to use ``JoinFlowService``, but
the leave / list helpers stay untouched. Putting the admission-
specific helpers in a new file keeps both files focused on a single
responsibility (membership vs admission policy) and avoids a 600-line
helper module that mixes two concerns.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog  # type: ignore[import-untyped]

from ..core.entities import Subnet, SubnetAllowlist, SubnetJoinRequest
from ..core.errors import ACNHTTPError, ErrorCode
from ..core.exceptions import (
    AllowlistEntryExistsError,
    AlreadyMemberError,
    InvitationAlreadyDecidedError,
    InvitationNotFoundError,
    InvitationPendingError,
    JoinFlowError,
    JoinRequestAlreadyDecidedError,
    JoinRequestNotFoundError,
    JoinRequestPendingError,
    SubnetNotFoundException,
)
from ..services._join_flow_result import (
    InviteAgentMergedToApprovedJoinRequestResult,
    InviteAgentResult,
    InviteAgentSentResult,
    JoinFlowAllowlistAutoApprovedResult,
    JoinFlowAutoAcceptedInvitationResult,
    JoinFlowJoinedAsOwnerResult,
    JoinFlowJoinedOpenResult,
    JoinFlowPendingResult,
    JoinFlowResult,
)

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Authorisation helpers
# ---------------------------------------------------------------------------
#
# All three sit alongside the existing ``_require_self`` in
# ``_subnet_membership.py`` and follow the same shape: raise
# ``ACNHTTPError`` with a stable ``ErrorCode`` slug. They do not catch
# anything — the caller is responsible for surrounding ``get_subnet`` /
# ``get_row`` calls with their own try/except chains that translate
# missing entities to canonical 404s before authz runs.


def _require_owner(agent_info: dict, subnet: Subnet) -> None:
    """Reject if the API key's agent_id is not the subnet's owner.

    ADR §"Authorization matrix" uses this gate for every endpoint
    that mutates allowlist / join_request / invitation rows from
    the owner side (approve, reject, invite, cancel, the three
    allowlist verbs). The 403 carries ``subnet_id`` + ``owner`` in
    ``details`` to help SDK clients distinguish "wrong subnet path"
    from "right subnet, wrong key".
    """
    if agent_info["agent_id"] != subnet.owner:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_OWNER,
            403,
            details={
                "slug": subnet.slug,
                "owner": subnet.owner,
                "caller": agent_info["agent_id"],
            },
        )


def _require_invitee(agent_info: dict, row: SubnetJoinRequest) -> None:
    """Reject if the API key's agent_id is not the invitation's invitee.

    ADR §"Authorization matrix" uses this on the two invitee verbs
    (accept / reject). ``row.agent_id`` is the invitee in ADR's
    ``SubnetJoinRequest`` field semantics (the agent who would, or
    did, become a member) — invariant across all three kinds.
    """
    if agent_info["agent_id"] != row.agent_id:
        raise ACNHTTPError(
            ErrorCode.NOT_INVITEE,
            403,
            details={
                "invitation_id": row.request_id,
                "invitee": row.agent_id,
                "caller": agent_info["agent_id"],
            },
        )


# ---------------------------------------------------------------------------
# Service-layer exception → HTTP error mapping
# ---------------------------------------------------------------------------
#
# Each :class:`JoinFlowError` subclass carries a stable ``reason``
# slug; we map by isinstance check (rather than by reason string) so
# the mapping is statically verifiable. Mapping table mirrors ADR
# §"HTTP status code conventions":
#
#   - ``*_NOT_FOUND`` → 404 (entity / namespace miss)
#   - ``*_PENDING``, ``*_ALREADY_DECIDED``, ``ALREADY_*`` → 409
#
# Anything not listed bubbles unchanged — callers add their own
# ``ACNHTTPError`` re-raise for ACN-specific 403 / 400 surfaces.


def _map_join_flow_error(exc: JoinFlowError) -> ACNHTTPError:
    """Translate a service-layer ``JoinFlowError`` to a typed HTTP error.

    Returns a fresh :class:`ACNHTTPError`; the caller is responsible
    for raising the result (so ``from exc`` cause chains stay
    explicit at the route boundary).
    """
    # 404s — namespace-specific NOT_FOUND for join-requests vs
    # invitations per ADR §"URL alias routing rules".
    if isinstance(exc, JoinRequestNotFoundError):
        return ACNHTTPError(
            ErrorCode.JOIN_REQUEST_NOT_FOUND,
            404,
            details={"request_id": exc.request_id},
        )
    if isinstance(exc, InvitationNotFoundError):
        return ACNHTTPError(
            ErrorCode.INVITATION_NOT_FOUND,
            404,
            details={"invitation_id": exc.invitation_id},
        )

    # 409s — every state-conflict surface gets its own slug; ADR
    # explicitly avoids a single ``CONFLICT`` because SDK clients
    # want to branch on the specific edge they hit.
    if isinstance(exc, JoinRequestPendingError):
        return ACNHTTPError(
            ErrorCode.JOIN_REQUEST_PENDING,
            409,
            details={"existing_request_id": exc.existing_request_id},
        )
    if isinstance(exc, InvitationPendingError):
        return ACNHTTPError(
            ErrorCode.INVITATION_PENDING,
            409,
            details={"existing_invitation_id": exc.existing_invitation_id},
        )
    if isinstance(exc, JoinRequestAlreadyDecidedError):
        return ACNHTTPError(
            ErrorCode.JOIN_REQUEST_ALREADY_DECIDED,
            409,
            details={
                "request_id": exc.request_id,
                "current_status": exc.current_status,
            },
        )
    if isinstance(exc, InvitationAlreadyDecidedError):
        return ACNHTTPError(
            ErrorCode.INVITATION_ALREADY_DECIDED,
            409,
            details={
                "invitation_id": exc.invitation_id,
                "current_status": exc.current_status,
            },
        )
    if isinstance(exc, AlreadyMemberError):
        return ACNHTTPError(
            ErrorCode.ALREADY_MEMBER,
            409,
            details={
                "slug": exc.slug,
                "agent_id": exc.agent_id,
            },
        )
    if isinstance(exc, AllowlistEntryExistsError):
        return ACNHTTPError(
            ErrorCode.ALREADY_ON_ALLOWLIST,
            409,
            details={
                "slug": exc.slug,
                "agent_id": exc.agent_id,
            },
        )

    # Catch-all: any future ``JoinFlowError`` subclass added to the
    # service layer without a matching branch here would silently
    # fall through as 500. We promote to 409 with ``RESOURCE_CONFLICT``
    # as a conservative default + log loud so a missing case shows
    # up in observability rather than as a confused SDK retry loop.
    logger.warning(
        "join_flow_error_unmapped",
        exception_class=type(exc).__name__,
        reason=exc.reason,
        message=str(exc),
    )
    return ACNHTTPError(
        ErrorCode.RESOURCE_CONFLICT,
        409,
        details={"reason": exc.reason},
    )


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------
#
# ``Subnet*.to_dict`` exists already but its target is Redis HASH
# storage (empty strings instead of None, datetime as ISO string).
# HTTP responses want JSON ``null`` for unset fields, which is
# semantically distinct from "empty string"; the two helpers below
# do that translation in one place so the per-endpoint response code
# stays declarative.


def _iso_or_none(value: datetime | None) -> str | None:
    """ISO 8601 string for a datetime, or ``None`` when unset."""
    return value.isoformat() if value is not None else None


def serialize_join_request(row: SubnetJoinRequest) -> dict[str, Any]:
    """JSON-ready response payload for a ``SubnetJoinRequest`` row."""
    return {
        "request_id": row.request_id,
        "slug": row.slug,
        "agent_id": row.agent_id,
        "kind": row.kind,
        "status": row.status,
        "initiated_by": row.initiated_by,
        "decided_by": row.decided_by,
        "created_at": _iso_or_none(row.created_at),
        "decided_at": _iso_or_none(row.decided_at),
        "note": row.note,
    }


def serialize_allowlist_entry(entry: SubnetAllowlist) -> dict[str, Any]:
    """JSON-ready response payload for a ``SubnetAllowlist`` entry."""
    return {
        "slug": entry.slug,
        "agent_id": entry.agent_id,
        "added_by": entry.added_by,
        "added_at": _iso_or_none(entry.added_at),
    }


# ---------------------------------------------------------------------------
# JoinFlowResult → (status_code, body) dispatch
# ---------------------------------------------------------------------------
#
# ADR §"POST /api/v1/agents/{agent_id}/subnets/{subnet_id} (join
# entry)" pins the response shape per branch. Five branches return
# 200 (caller is, now, a member) and one returns 202 (caller is not
# yet a member). Two ``200`` variants (3 and 4) share the
# ``invitation_id + via`` shape, distinguished only by ``via``
# value — owning that consolidation here lets the route handler
# stay a one-liner.


def join_flow_result_to_response(result: JoinFlowResult) -> tuple[int, dict[str, Any]]:
    """Translate a sealed-union ``JoinFlowResult`` to ``(status, body)``.

    The status code mapping is a strict function of the variant type
    (no per-call branching needed by callers). Body shape follows
    ADR §join branches 1–6 verbatim.
    """
    if isinstance(result, JoinFlowJoinedOpenResult):
        # Branch 1 — open subnet.
        return 200, {
            "status": "joined",
            "slug": result.slug,
            "agent_id": result.agent_id,
        }

    if isinstance(result, JoinFlowJoinedAsOwnerResult):
        # Branch 2 — owner self-join. Same body as branch 1; the
        # caller can tell them apart by ``subnet.owner == agent_id``
        # but doesn't need to for any downstream logic.
        return 200, {
            "status": "joined",
            "slug": result.slug,
            "agent_id": result.agent_id,
        }

    if isinstance(result, JoinFlowAutoAcceptedInvitationResult):
        # Branches 3 + 4 — pending invitation auto-accept. ``via``
        # discriminates the two: ``self_join`` for branch 3,
        # ``allowlist`` for branch 4. ADR pins the field shape so
        # SDK clients can branch on ``via`` if they care about the
        # audit trail.
        return 200, {
            "auto_resolved": True,
            "resolved_kind": "invitation",
            "slug": result.slug,
            "agent_id": result.agent_id,
            "invitation_id": result.invitation.request_id,
            "via": result.via,
        }

    if isinstance(result, JoinFlowAllowlistAutoApprovedResult):
        # Branch 5 — allowlist hit with no pending invitation. A
        # fresh ``allowlist_auto`` row was born approved; surface
        # its request_id so the caller can correlate audit log
        # readings.
        return 200, {
            "slug": result.slug,
            "agent_id": result.agent_id,
            "request_id": result.request.request_id,
            "via": "allowlist",
        }

    if isinstance(result, JoinFlowPendingResult):
        # Branch 6 — fall-through pending join_request. 202 because
        # the caller is not yet a member; the owner owes a decision.
        return 202, {
            "slug": result.slug,
            "agent_id": result.agent_id,
            "request_id": result.request.request_id,
            "status": "pending",
        }

    # Defensive default — any new variant added to the sealed
    # union without a matching branch here would otherwise return
    # an empty body. Raise instead so a missed update surfaces
    # immediately in test rather than as a silent client failure.
    raise RuntimeError(f"unhandled JoinFlowResult variant: {type(result).__name__}")


def invite_agent_result_to_response(
    result: InviteAgentResult,
) -> tuple[int, dict[str, Any]]:
    """Translate :class:`InviteAgentResult` to ``(status, body)``.

    ADR §"Invitation-side endpoints" defines two outcomes for
    ``POST /invitations``:

    - Normal path → ``202 {invitation_id}`` (the invitation row is
      pending; the invitee owes a decision).
    - Merge path → ``200 {auto_resolved: true, resolved_kind:
      "join_request", request_id}`` (target had a pending
      join_request; the invite collapses to an owner-initiated
      auto-approval).
    """
    if isinstance(result, InviteAgentSentResult):
        return 202, {
            "slug": result.slug,
            "agent_id": result.agent_id,
            "invitation_id": result.invitation.request_id,
            "status": "pending",
        }

    if isinstance(result, InviteAgentMergedToApprovedJoinRequestResult):
        return 200, {
            "auto_resolved": True,
            "resolved_kind": "join_request",
            "slug": result.slug,
            "agent_id": result.agent_id,
            "request_id": result.request.request_id,
        }

    raise RuntimeError(
        f"unhandled InviteAgentResult variant: {type(result).__name__}"
    )


# ---------------------------------------------------------------------------
# Convenience wrapper: load + 404 in one go
# ---------------------------------------------------------------------------


async def load_subnet_or_404(subnet_service: Any, subnet_id: str) -> Subnet:
    """``SubnetService.get_subnet`` wrapped in the canonical 404 conversion.

    Every admission endpoint starts with a subnet lookup; the
    exception → ``ACNHTTPError(SUBNET_NOT_FOUND)`` translation is
    boilerplate. Returns the entity so the caller can pass it to
    ``_require_owner`` without re-fetching.
    """
    try:
        return await subnet_service.get_subnet(subnet_id)
    except SubnetNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"subnet_id": subnet_id},
        ) from e
