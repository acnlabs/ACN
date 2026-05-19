"""ADR-0004 Slice 2.3 subnet-admission endpoints (allowlist / join_request / invitation).

Fourteen handlers covering the three resources the ADR introduces:

- **Allowlist** (3): pre-authorise / revoke / list owner-side trust set.
- **Join requests** (4): owner approve / reject; applicant withdraw;
  owner list.
- **Invitations** (5): owner invite (with auto-merge of pending
  join_requests) / cancel / list; invitee accept / reject.
- **Agent-side invitations list** (1): invitee's cross-subnet
  pending-invitation view.

The join-entry verb (``POST /api/v1/agents/{agent_id}/subnets/
{subnet_id}``) still lives in ``routes/agent_subnets.py`` (and the
legacy mirror in ``routes/subnets.py``); both delegate to
``_subnet_membership.do_join_subnet`` which now dispatches the
six-branch decision tree via ``JoinFlowService``.

URL precedence
--------------
This router is included BEFORE ``registry.router`` in ``api.py``
because ``registry`` has a catch-all
``/{agent_id}/{rest_path:path}`` proxy that would otherwise swallow
``GET /api/v1/agents/{agent_id}/subnet-invitations``. Same precedence
discipline as ``follows.py`` / ``manifest.py`` / ``allowlist.py``.

Response shapes
---------------
Per ADR §"HTTP status code conventions":
- 200 — request settled inline (any approve / reject / cancel /
  withdraw verb; merge-path POST /invitations; GET).
- 201 — new configuration resource (POST /allowlist).
- 202 — pending request created (normal POST /invitations).
- 204 — no body (DELETE /allowlist/{agent_id} — idempotent).

All 4xx surfaces flow through ``ACNHTTPError`` with stable
``ErrorCode`` slugs (see ``acn/core/errors.py``); the
``_subnet_admission._map_join_flow_error`` helper covers the eight
:class:`JoinFlowError` subclasses that the service layer raises.

Authorization
-------------
Per ADR §"Authorization matrix":
- Owner-only: every allowlist verb, approve / reject / list
  join_requests, send / list / cancel invitations.
- Invitee-only: accept / reject invitation.
- Self-only: applicant withdraw, agent-side invitations list.

The pre-existing ``_require_self`` (from ``_subnet_membership.py``)
covers the self-only paths; ``_require_owner`` / ``_require_invitee``
(in ``_subnet_admission.py``) cover the new gates.
"""

from __future__ import annotations

from typing import Literal

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..core.errors import ACN_DEFAULT_RESPONSES, ACNHTTPError, ErrorCode
from ..core.exceptions import (
    AgentNotFoundException,
    AlreadyMemberError,
    JoinFlowError,
)
from ._subnet_admission import (
    _map_join_flow_error,
    _require_invitee,
    _require_owner,
    invite_agent_result_to_response,
    load_subnet_or_404,
    serialize_allowlist_entry,
    serialize_join_request,
)
from ._subnet_membership import _require_self
from .dependencies import (
    AgentApiKeyDep,
    AgentIdPath,
    AgentServiceDep,
    RequestIdPath,
    SubnetIdPath,
    SubnetServiceDep,
)

logger = structlog.get_logger()

router = APIRouter(
    prefix="/api/v1",
    tags=["subnet_admission"],
    responses=ACN_DEFAULT_RESPONSES,
)


# Length cap for the optional ``note`` body on every approve / reject
# / withdraw / invite endpoint. Pinned to 500 chars to match the
# entity-layer ``SubnetJoinRequest`` invariant; validating at the
# route layer gives the caller a 422 (Pydantic) instead of a 500
# (entity-raised ValueError) for over-long notes.
_MAX_NOTE_LEN: int = 500


# ---------------------------------------------------------------------------
# Pydantic request bodies
# ---------------------------------------------------------------------------


class AllowlistAddBody(BaseModel):
    """POST /api/v1/subnets/{subnet_id}/allowlist body."""

    agent_id: str = Field(..., min_length=1, max_length=128)


class _OptionalNoteBody(BaseModel):
    """Shared body shape for verbs that accept an audit note."""

    note: str | None = Field(default=None, max_length=_MAX_NOTE_LEN)


class ApproveBody(_OptionalNoteBody):
    """POST /api/v1/subnets/{s}/join-requests/{rid}/approve body."""


class RejectBody(_OptionalNoteBody):
    """POST /api/v1/subnets/{s}/join-requests/{rid}/reject body."""


class WithdrawBody(_OptionalNoteBody):
    """DELETE /api/v1/subnets/{s}/join-requests/{rid} body (optional)."""


class InviteBody(_OptionalNoteBody):
    """POST /api/v1/subnets/{s}/invitations body."""

    agent_id: str = Field(..., min_length=1, max_length=128)


class AcceptInvitationBody(_OptionalNoteBody):
    """POST /api/v1/subnets/{s}/invitations/{iid}/accept body (optional)."""


class RejectInvitationBody(_OptionalNoteBody):
    """POST /api/v1/subnets/{s}/invitations/{iid}/reject body (optional)."""


class CancelInvitationBody(_OptionalNoteBody):
    """DELETE /api/v1/subnets/{s}/invitations/{iid} body (optional)."""


# ---------------------------------------------------------------------------
# Allowlist endpoints (3)
# ---------------------------------------------------------------------------
#
# All three are owner-only. POST returns 201 on first add (matching
# ADR §"Allowlist endpoints") and 409 ALREADY_ON_ALLOWLIST on
# duplicate. DELETE is idempotent (returns 204 even when the entry
# wasn't present — service layer raises AllowlistEntryNotFoundError
# which we silently swallow). GET is owner-only by design (allowlist
# leaks relationship metadata if exposed publicly).


@router.post("/subnets/{subnet_id}/allowlist", status_code=201)
async def add_to_allowlist(
    subnet_id: SubnetIdPath,
    body: AllowlistAddBody,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
):
    """Pre-authorise ``body.agent_id`` for ``subnet_id``'s allowlist.

    Owner-only. Idempotent failure (existing entry) returns 409
    ``ALREADY_ON_ALLOWLIST`` per ADR §"Allowlist endpoints" — the
    legacy "silently no-op on duplicate" shape is rejected so a
    misconfigured UI surfaces the bug instead of silently
    succeeding.

    The target agent must exist; missing agent → 404
    ``AGENT_NOT_FOUND``. (We don't validate at the entity layer
    because the route is the only caller — pushing the existence
    check up here keeps the service-layer shape minimal.)
    """
    subnet = await load_subnet_or_404(subnet_service, subnet_id)
    _require_owner(agent_info, subnet)

    try:
        await agent_service.get_agent(body.agent_id)
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": body.agent_id},
        ) from e

    try:
        entry = await subnet_service.add_allowlist(
            subnet_id, body.agent_id, added_by=agent_info["agent_id"]
        )
    except JoinFlowError as e:
        raise _map_join_flow_error(e) from e

    logger.info(
        "subnet_allowlist_added_via_http",
        subnet_id=subnet_id,
        agent_id=body.agent_id,
        added_by=agent_info["agent_id"],
    )
    return serialize_allowlist_entry(entry)


@router.delete(
    "/subnets/{subnet_id}/allowlist/{agent_id}",
    status_code=204,
    responses={**ACN_DEFAULT_RESPONSES, 204: {"description": "Entry removed"}},
)
async def remove_from_allowlist(
    subnet_id: SubnetIdPath,
    agent_id: AgentIdPath,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
):
    """Remove ``agent_id`` from ``subnet_id``'s allowlist (owner-only).

    Idempotent per ADR §"Allowlist endpoints" — removing an entry
    that doesn't exist is a 204 (not a 404). This matches the
    semantics SDK clients expect from declarative state-sync calls
    (``ensure_not_on_allowlist``).

    NB: ADR §"Allowlist mutation does not affect agents who already
    joined" — an agent removed from the allowlist remains a subnet
    member if they had already joined. The route layer does not
    revoke membership.
    """
    subnet = await load_subnet_or_404(subnet_service, subnet_id)
    _require_owner(agent_info, subnet)

    # Service-layer ``remove_allowlist`` is already idempotent (returns
    # False when the pair wasn't present); the 204 response is the
    # same either way per ADR §"Allowlist endpoints".
    try:
        await subnet_service.remove_allowlist(
            subnet_id, agent_id, remover=agent_info["agent_id"]
        )
    except JoinFlowError as e:
        raise _map_join_flow_error(e) from e

    logger.info(
        "subnet_allowlist_removed_via_http",
        subnet_id=subnet_id,
        agent_id=agent_id,
        removed_by=agent_info["agent_id"],
    )
    return Response(status_code=204)


@router.get("/subnets/{subnet_id}/allowlist")
async def list_allowlist(
    subnet_id: SubnetIdPath,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """List allowlist entries for ``subnet_id`` (owner-only).

    Owner-only by design per ADR §"GET /subnets/{s}/allowlist is
    owner-only deliberately" — the allowlist is a privacy-sensitive
    trust signal and exposing it publicly would leak relationship
    metadata for both the operator and the listed agents.

    Pagination matches ``follows`` / ``allowlist`` conventions: 100
    default page, 500 max page. Allowlists are bounded (no
    universal cap, but practical sizes are well under 500).
    """
    subnet = await load_subnet_or_404(subnet_service, subnet_id)
    _require_owner(agent_info, subnet)

    entries = await subnet_service.list_allowlist(
        subnet_id, limit=limit, offset=offset
    )
    return {
        "subnet_id": subnet_id,
        "entries": [serialize_allowlist_entry(e) for e in entries],
    }


# ---------------------------------------------------------------------------
# Join-request endpoints (4)
# ---------------------------------------------------------------------------
#
# Owner approves / rejects; applicant withdraws; owner lists.
#
# ADR §"URL alias routing rules": ``/join-requests/{id}`` paths reject
# rows where ``row.kind != "join_request"`` with 404
# JOIN_REQUEST_NOT_FOUND — the service-layer helper
# ``_load_join_request_or_404`` enforces this and raises
# ``JoinRequestNotFoundError`` on mismatch. We pass it through the
# mapper unchanged so namespace-cross attempts (using a join-request
# verb against an invitation id) emit JOIN_REQUEST_NOT_FOUND, never
# leaking the existence of a row in the other namespace.


@router.post("/subnets/{subnet_id}/join-requests/{request_id}/approve")
async def approve_join_request(
    subnet_id: SubnetIdPath,
    request_id: RequestIdPath,
    agent_info: AgentApiKeyDep,
    body: ApproveBody | None = None,
    subnet_service: SubnetServiceDep = None,
):
    """Owner approves a pending ``join_request`` (CAS pending → approved).

    Side effects:
    - Row CAS'd to ``approved``, ``decided_by=owner_agent_id``,
      ``decided_at=now()``.
    - Applicant added to ``subnet.member_agent_ids`` (no agent-side
      back-reference write — the applicant is expected to re-join
      via ``POST /agents/{a}/subnets/{s}`` to register the
      back-ref; ADR §"State machine edges" pins this).
    - ``subnet.join_approved`` webhook fires (logged-only stub
      pending Slice 2.4).
    """
    subnet = await load_subnet_or_404(subnet_service, subnet_id)
    _require_owner(agent_info, subnet)
    note = body.note if body is not None else None

    try:
        row = await subnet_service.approve_join_request(
            subnet_id,
            request_id,
            owner_id=agent_info["agent_id"],
            note=note,
        )
    except JoinFlowError as e:
        raise _map_join_flow_error(e) from e

    return serialize_join_request(row)


@router.post("/subnets/{subnet_id}/join-requests/{request_id}/reject")
async def reject_join_request(
    subnet_id: SubnetIdPath,
    request_id: RequestIdPath,
    agent_info: AgentApiKeyDep,
    body: RejectBody | None = None,
    subnet_service: SubnetServiceDep = None,
):
    """Owner rejects a pending ``join_request`` (CAS pending → rejected).

    No membership change. ``subnet.join_rejected`` webhook fires.
    Optional ``note`` (≤500 chars) per ADR §"Application-side
    endpoints" lets the owner record a human-readable reason in the
    audit trail.
    """
    subnet = await load_subnet_or_404(subnet_service, subnet_id)
    _require_owner(agent_info, subnet)
    note = body.note if body is not None else None

    try:
        row = await subnet_service.reject_join_request(
            subnet_id,
            request_id,
            owner_id=agent_info["agent_id"],
            note=note,
        )
    except JoinFlowError as e:
        raise _map_join_flow_error(e) from e

    return serialize_join_request(row)


@router.delete("/subnets/{subnet_id}/join-requests/{request_id}")
async def withdraw_join_request(
    subnet_id: SubnetIdPath,
    request_id: RequestIdPath,
    agent_info: AgentApiKeyDep,
    body: WithdrawBody | None = None,
    subnet_service: SubnetServiceDep = None,
):
    """Applicant withdraws their own pending ``join_request``.

    ADR §"Authorization matrix" pins this to ``_require_self
    against row.initiated_by``: the caller must be the agent who
    originally created the request (NOT the subnet owner — owner
    rejection is a separate verb). The check happens *after* the
    row load so we can validate ``initiated_by`` without a separate
    repo call.

    No membership change. ``subnet.join_withdrawn`` webhook fires.
    """
    await load_subnet_or_404(subnet_service, subnet_id)

    # Load + namespace-check the row up front so we can authz
    # against initiated_by. Wrong namespace → 404 surfaces here.
    try:
        row = await subnet_service._load_join_request_or_404(
            request_id,
            expected_kind="join_request",
            expected_subnet_id=subnet_id,
        )
    except JoinFlowError as e:
        raise _map_join_flow_error(e) from e

    # Applicant-only: must match the row's initiated_by.
    if agent_info["agent_id"] != row.initiated_by:
        raise ACNHTTPError(
            ErrorCode.API_KEY_AGENT_MISMATCH,
            403,
            details={
                "request_id": request_id,
                "initiated_by": row.initiated_by,
                "caller": agent_info["agent_id"],
            },
        )

    note = body.note if body is not None else None

    try:
        withdrawn = await subnet_service.withdraw_join_request(
            subnet_id,
            request_id,
            applicant_id=agent_info["agent_id"],
            note=note,
        )
    except JoinFlowError as e:
        raise _map_join_flow_error(e) from e

    return serialize_join_request(withdrawn)


@router.get("/subnets/{subnet_id}/join-requests")
async def list_join_requests(
    subnet_id: SubnetIdPath,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
    status: Literal["pending", "approved", "rejected", "withdrawn"] | None = Query(
        default=None
    ),
    kind: Literal["join_request", "allowlist_auto", "invitation"] = Query(
        default="join_request",
        description=(
            "kind filter — defaults to join_request. Supplying "
            "kind=invitation returns 400 INVALID_KIND_FILTER per "
            "ADR §'Application-side endpoints' (invitations are "
            "queryable through /invitations only)."
        ),
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Owner lists join_request / allowlist_auto rows for ``subnet_id``.

    ADR §"Application-side endpoints" forbids ``kind=invitation`` on
    this path; rejecting at the request boundary with 400
    ``INVALID_KIND_FILTER`` prevents SDK clients from accidentally
    surfacing invitation rows through the join-request channel.
    """
    subnet = await load_subnet_or_404(subnet_service, subnet_id)
    _require_owner(agent_info, subnet)

    if kind == "invitation":
        raise ACNHTTPError(
            ErrorCode.INVALID_KIND_FILTER,
            400,
            details={"kind": kind, "allowed": ["join_request", "allowlist_auto"]},
        )

    rows = await subnet_service.list_join_requests(
        subnet_id,
        kind=kind,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {
        "subnet_id": subnet_id,
        "items": [serialize_join_request(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# Invitation endpoints (5 on the subnet path + 1 on the agent path)
# ---------------------------------------------------------------------------
#
# Owner sends / cancels / lists; invitee accepts / rejects. The send
# verb has a merge path (target has pending join_request → owner
# invite collapses to auto-approval of that request); the response
# discriminates 202 (normal send) vs 200 (merge).


@router.post("/subnets/{subnet_id}/invitations")
async def send_invitation(
    subnet_id: SubnetIdPath,
    body: InviteBody,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
):
    """Owner sends an invitation, or merges into a pending join_request.

    See :meth:`SubnetService.invite_agent` for the merge path —
    return shape is the sealed ``InviteAgentResult`` translated to
    HTTP by :func:`invite_agent_result_to_response`:

    - **Normal path** — 202 ``{invitation_id, status: "pending"}``.
    - **Merge path** — 200 ``{auto_resolved: true, resolved_kind:
      "join_request", request_id}``.

    Pre-checks:
    - Target agent must exist → 404 ``AGENT_NOT_FOUND``.
    - Target already a member → 409 ``ALREADY_MEMBER``.
    - Target already has a pending invitation → 409
      ``INVITATION_PENDING`` with the existing id echoed.
    """
    subnet = await load_subnet_or_404(subnet_service, subnet_id)
    _require_owner(agent_info, subnet)

    try:
        await agent_service.get_agent(body.agent_id)
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": body.agent_id},
        ) from e

    try:
        result = await subnet_service.invite_agent(
            subnet_id,
            body.agent_id,
            owner_id=agent_info["agent_id"],
            note=body.note,
        )
    except AlreadyMemberError as e:
        raise _map_join_flow_error(e) from e
    except JoinFlowError as e:
        raise _map_join_flow_error(e) from e

    status_code, payload = invite_agent_result_to_response(result)
    return JSONResponse(status_code=status_code, content=payload)


@router.post("/subnets/{subnet_id}/invitations/{request_id}/accept")
async def accept_invitation(
    subnet_id: SubnetIdPath,
    request_id: RequestIdPath,
    agent_info: AgentApiKeyDep,
    body: AcceptInvitationBody | None = None,
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
):
    """Invitee accepts a pending invitation.

    Owner-side cancel of the same row is a different verb (DELETE
    /invitations/{iid}). ``_require_invitee`` gates this against
    ``row.agent_id`` (the invitee, per the entity field semantics).

    Side effects on success:
    - Row CAS'd to ``approved``, ``decided_by=invitee_id``.
    - Invitee added to ``subnet.member_agent_ids``.
    - Invitee's ``agent.subnet_ids`` gains the back-reference
      (written here in the route layer for parity with the join
      path).
    - ``subnet.invitation_accepted`` webhook fires.
    """
    await load_subnet_or_404(subnet_service, subnet_id)

    # Load + namespace-check up front so authz uses the loaded row.
    try:
        row = await subnet_service._load_join_request_or_404(
            request_id,
            expected_kind="invitation",
            expected_subnet_id=subnet_id,
        )
    except JoinFlowError as e:
        raise _map_join_flow_error(e) from e

    _require_invitee(agent_info, row)

    try:
        accepted = await subnet_service.accept_invitation(
            subnet_id,
            request_id,
            invitee_id=agent_info["agent_id"],
        )
    except JoinFlowError as e:
        raise _map_join_flow_error(e) from e

    # Agent-side back-reference. Same pattern + rollback semantics
    # as ``do_join_subnet``: the service layer has already added
    # the member; failure to write the back-ref leaves a
    # half-joined state we best-effort recover.
    try:
        await agent_service.join_subnet(agent_info["agent_id"], subnet_id)
    except AgentNotFoundException as e:
        logger.warning(
            "accept_invitation_back_ref_failed_agent_not_found",
            agent_id=agent_info["agent_id"],
            subnet_id=subnet_id,
        )
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"agent_id": agent_info["agent_id"]},
        ) from e
    except Exception as e:  # noqa: BLE001
        logger.error(
            "accept_invitation_back_ref_failed",
            agent_id=agent_info["agent_id"],
            subnet_id=subnet_id,
            error=str(e),
            exc_info=True,
        )
        # Unlike join, we do NOT roll back ``add_member`` here —
        # the invitation row has already been CAS'd to approved,
        # and reversing that CAS would leak a partial-success
        # signal to Harness consumers. Surface 500 and let the SDK
        # retry (idempotent CAS will then succeed at the
        # already-decided check, surfacing 409
        # INVITATION_ALREADY_DECIDED — SDK can treat that as
        # success).
        raise HTTPException(
            status_code=500,
            detail="Failed to write agent back-reference",
        ) from e

    return serialize_join_request(accepted)


@router.post("/subnets/{subnet_id}/invitations/{request_id}/reject")
async def reject_invitation(
    subnet_id: SubnetIdPath,
    request_id: RequestIdPath,
    agent_info: AgentApiKeyDep,
    body: RejectInvitationBody | None = None,
    subnet_service: SubnetServiceDep = None,
):
    """Invitee rejects a pending invitation (CAS pending → rejected).

    No membership change. ``subnet.invitation_rejected`` webhook
    fires. Optional ``note`` lets the invitee record a reason.
    """
    await load_subnet_or_404(subnet_service, subnet_id)

    try:
        row = await subnet_service._load_join_request_or_404(
            request_id,
            expected_kind="invitation",
            expected_subnet_id=subnet_id,
        )
    except JoinFlowError as e:
        raise _map_join_flow_error(e) from e

    _require_invitee(agent_info, row)
    note = body.note if body is not None else None

    try:
        rejected = await subnet_service.reject_invitation(
            subnet_id,
            request_id,
            invitee_id=agent_info["agent_id"],
            note=note,
        )
    except JoinFlowError as e:
        raise _map_join_flow_error(e) from e

    return serialize_join_request(rejected)


@router.delete("/subnets/{subnet_id}/invitations/{request_id}")
async def cancel_invitation(
    subnet_id: SubnetIdPath,
    request_id: RequestIdPath,
    agent_info: AgentApiKeyDep,
    body: CancelInvitationBody | None = None,
    subnet_service: SubnetServiceDep = None,
):
    """Owner cancels a pending invitation (CAS pending → withdrawn).

    Owner-only counterpart to applicant withdraw. Per ADR §"State
    transition table", ``invitation`` rows transition to
    ``withdrawn`` (not ``rejected``) on owner cancel — distinct
    audit token so consumers can tell "owner gave up" from "invitee
    said no".

    ``subnet.invitation_canceled`` webhook fires.
    """
    subnet = await load_subnet_or_404(subnet_service, subnet_id)
    _require_owner(agent_info, subnet)
    note = body.note if body is not None else None

    try:
        canceled = await subnet_service.cancel_invitation(
            subnet_id,
            request_id,
            owner_id=agent_info["agent_id"],
            note=note,
        )
    except JoinFlowError as e:
        raise _map_join_flow_error(e) from e

    return serialize_join_request(canceled)


@router.get("/subnets/{subnet_id}/invitations")
async def list_invitations(
    subnet_id: SubnetIdPath,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
    status: Literal["pending", "approved", "rejected", "withdrawn"] | None = Query(
        default=None
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Owner lists invitation rows for ``subnet_id``.

    Owner-only — invitees use the per-agent endpoint
    ``GET /agents/{a}/subnet-invitations`` for their own view.
    """
    subnet = await load_subnet_or_404(subnet_service, subnet_id)
    _require_owner(agent_info, subnet)

    rows = await subnet_service.list_invitations(
        subnet_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {
        "subnet_id": subnet_id,
        "items": [serialize_join_request(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# Agent-side invitations list
# ---------------------------------------------------------------------------


@router.get("/agents/{agent_id}/subnet-invitations")
async def list_agent_pending_invitations(
    agent_id: AgentIdPath,
    agent_info: AgentApiKeyDep,
    subnet_service: SubnetServiceDep = None,
):
    """Invitee's cross-subnet pending-invitation list.

    Self-only per ADR §"Authorization matrix". Returns only
    ``status=pending`` rows — the assumption is that an invitee
    cares about "what's waiting on me to decide", not historical
    decisions. (Historical view is available per-subnet through
    the owner-only ``GET /subnets/{s}/invitations``.)
    """
    _require_self(agent_info, agent_id)

    rows = await subnet_service.list_pending_invitations_for_agent(agent_id)
    return {
        "agent_id": agent_id,
        "items": [serialize_join_request(r) for r in rows],
    }
