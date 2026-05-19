"""``JoinFlowService.join_subnet`` result sealed union (ADR-0004 Slice 2.2).

ADR §"POST /api/v1/agents/{agent_id}/subnets/{subnet_id} (join entry)"
defines six branches the join entry-point dispatches into. Each
branch ends in a *different* success shape — open join, owner
self-join, invitation auto-accept (two flavours, distinguished by
``via``), allowlist auto-approval, or pending request creation —
and each maps to a distinct ``200 vs 202`` + response body in
Slice 2.3's route layer.

This sealed union lets ``JoinFlowService.join_subnet`` return a
typed result that the route layer matches on once instead of
duplicating six ``if status == "joined"`` checks. Failures are
still raised as :class:`acn.core.exceptions` subclasses (409 / 404
/ 403 maps) — the union covers **success outcomes only**.

The two invitation-auto-accept branches (branch 3 self-join and
branch 4 allowlist-merge) collapse into one variant
:class:`JoinFlowAutoAcceptedInvitationResult` with a ``via`` field
discriminator, matching the response shapes ADR §join lays out
(``via="self_join"`` vs ``via="allowlist"``). Five variants for
six branches is the minimal sealed shape — anything more would
forge a distinction the response body doesn't carry.

Why frozen dataclasses, not Pydantic
------------------------------------
These types never cross the HTTP boundary — Slice 2.3's route
layer converts each variant to its own Pydantic response model.
They exist purely to give the service ↔ route handoff a typed
contract that ``match`` statements can exhaust. Pydantic would
add coercion semantics we don't need and serialization machinery
we don't use.
"""

from dataclasses import dataclass
from typing import Literal

from ..core.entities import SubnetJoinRequest


@dataclass(frozen=True)
class JoinFlowJoinedOpenResult:
    """Branch 1 — ``join_policy='open'`` → immediate ``add_member``.

    No row is created in ``subnet_join_requests``. The route layer
    returns ``200 {status: "joined", ...}``.
    """

    subnet_id: str
    agent_id: str


@dataclass(frozen=True)
class JoinFlowJoinedAsOwnerResult:
    """Branch 2 — owner self-joins their own subnet → immediate ``add_member``.

    No row in ``subnet_join_requests``. The owner is canonically
    a member of their subnet (ADR §State machine edges "Owner
    self-joins their own subnet"); this branch is the no-op
    fast path. Route layer returns ``200 {status: "joined", ...}``.
    """

    subnet_id: str
    agent_id: str


@dataclass(frozen=True)
class JoinFlowAutoAcceptedInvitationResult:
    """Branches 3 & 4 — a pending invitation was auto-accepted on self-join.

    Branch 3 fires when an agent with a pending invitation calls
    ``join`` themselves (``via='self_join'``). Branch 4 fires when
    an allowlisted agent calls ``join`` and a pending invitation
    also happens to exist (``via='allowlist'`` — the invitation
    wins to avoid a parallel ``allowlist_auto`` row).

    Route layer returns ``200 {auto_resolved: true, resolved_kind:
    "invitation", invitation_id, via}``.
    """

    subnet_id: str
    agent_id: str
    invitation: SubnetJoinRequest
    via: Literal["self_join", "allowlist"]


@dataclass(frozen=True)
class JoinFlowAllowlistAutoApprovedResult:
    """Branch 5 — allowlisted agent joins, no pending invitation present.

    A fresh row with ``kind='allowlist_auto', status='approved'`` is
    born; the agent is added as a member in the same flow. Route
    layer returns ``200 {request_id, via: "allowlist", ...}``.
    """

    subnet_id: str
    agent_id: str
    request: SubnetJoinRequest


@dataclass(frozen=True)
class JoinFlowPendingResult:
    """Branch 6 — fallback. Fresh ``kind='join_request'`` row, ``status='pending'``.

    The agent is *not* added as a member; the owner must decide.
    Route layer returns ``202 {request_id, status: "pending"}``.
    """

    subnet_id: str
    agent_id: str
    request: SubnetJoinRequest


JoinFlowResult = (
    JoinFlowJoinedOpenResult
    | JoinFlowJoinedAsOwnerResult
    | JoinFlowAutoAcceptedInvitationResult
    | JoinFlowAllowlistAutoApprovedResult
    | JoinFlowPendingResult
)
"""Sealed union of every success outcome from ``JoinFlowService.join_subnet``.

The route layer (Slice 2.3) exhaustively matches this union to
pick the right HTTP status code + response model. Failures
(409 / 404 / 403) are raised as :class:`ACNException` subclasses,
not encoded in the union — the union is success-only by design.
"""


@dataclass(frozen=True)
class InviteAgentSentResult:
    """``SubnetService.invite_agent`` normal path — fresh invitation row.

    The owner invited an agent who had no pre-existing pending row;
    a ``kind='invitation', status='pending'`` row was created and
    the invitee owes a decision. Route layer returns
    ``202 {invitation_id}``.
    """

    subnet_id: str
    agent_id: str
    invitation: SubnetJoinRequest


@dataclass(frozen=True)
class InviteAgentMergedToApprovedJoinRequestResult:
    """``SubnetService.invite_agent`` merge path — auto-approved a pending join_request.

    Symmetric partner of branch 3's "agent self-joins with pending
    invitation" path. The owner's invite arrives while the agent
    already has a ``kind='join_request', status='pending'`` row;
    the row is CAS'd to ``approved`` (``decided_by=owner_id``,
    ``trigger=auto_on_invite``) and the agent is added as a member.
    No new invitation row is created — ADR §"POST /invitations"
    "Merge path".

    Route layer returns ``200 {auto_resolved: true,
    resolved_kind: "join_request", request_id}``.
    """

    subnet_id: str
    agent_id: str
    request: SubnetJoinRequest


InviteAgentResult = InviteAgentSentResult | InviteAgentMergedToApprovedJoinRequestResult
"""Sealed union of every success outcome from ``SubnetService.invite_agent``.

Failures (target is already a member, target already has a pending
invitation, target subnet missing, etc.) raise the matching
:class:`acn.core.exceptions.JoinFlowError` subclass; the union
covers success outcomes only.
"""
