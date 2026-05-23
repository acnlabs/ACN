"""Business Exceptions

Domain-specific exceptions.
"""


class ACNException(Exception):
    """Base exception for ACN"""

    pass


class AgentNotFoundException(ACNException):
    """Agent not found"""

    pass


class SubnetNotFoundException(ACNException):
    """Subnet not found"""

    pass


class PolicyRejected(ACNException):
    """Inbound message was rejected by the recipient's communication_policy.

    Carries a short ``reason`` code (used for HTTP error code mapping and
    ``message_rejected_by_policy_total{reason}`` metric labels) and an
    optional human-readable ``reject_reason`` provided by the recipient
    in their policy. ``recipient_id`` is preserved so audit / log
    handlers can attribute the rejection without re-querying the
    registry.

    Raised by ``PolicyCheckService.check_inbound_or_raise``; callers in
    ``MessageRouter.route`` and ``SubnetManager.forward_request``
    short-circuit on this exception (no inbox write, no DLQ).

    See docs/features/acn-communication-economic-model.md
    "Phase 1 网关执行点决策".
    """

    def __init__(
        self,
        reason: str,
        reject_reason: str | None = None,
        recipient_id: str | None = None,
    ) -> None:
        self.reason = reason
        self.reject_reason = reject_reason
        self.recipient_id = recipient_id
        message = f"{reason}: {reject_reason}" if reject_reason else reason
        super().__init__(message)


# ---------------------------------------------------------------------------
# Allowlist domain exceptions (Phase 2 PR #2)
# ---------------------------------------------------------------------------
#
# Hoisted from ``services/allowlist_service.py`` to ``core/exceptions`` so
# the Postgres repository can raise ``AllowlistCapacityExceededError``
# directly when the database-side capacity trigger fires (PR #2 v3 review
# P1-A1 fix — TOCTOU race resolved by a per-owner advisory lock + check
# inside a ``BEFORE INSERT`` trigger). Keeping the type in core/ avoids a
# repo→service import cycle while letting all three layers (repo, service,
# routes) reference the same canonical exception class.
class SelfAllowlistError(ACNException):
    """Owner attempted to add itself to its own allowlist.

    Mirrors the self-follow rule: the operation has no semantic meaning
    (sender == recipient already passes via the ``open`` short-circuit
    in ``PolicyCheckService.check_inbound``) and would clutter audit
    surfaces. Surfaced as 400 by the route layer.
    """


class AllowlistCapacityExceededError(ACNException):
    """Owner's allowlist already at ``MAX_ALLOWLIST_SIZE`` (=500).

    Raised by either the service-layer pre-flight check (cheap path)
    or the Postgres ``trg_agent_allowlist_capacity`` trigger (race-
    safe last line of defence). The trigger uses a per-owner
    ``pg_advisory_xact_lock`` to serialise concurrent INSERTs for the
    same owner — see migration ``f6a7b8c9d0e1`` for the full SQL.
    The route layer surfaces this as 429 with a "remove some entries
    first" hint.
    """


# ---------------------------------------------------------------------------
# ADR-0004 join-flow domain exceptions (Phase 2 Slice 2.2)
# ---------------------------------------------------------------------------
#
# All eight surface as 4xx HTTP statuses through Slice 2.3's route layer.
# Each carries a stable ``reason`` string so the route layer's
# ``_join_flow_error_to_acn`` mapper can pin the ``ErrorCode`` /
# ``details.reason`` payload without depending on (English) message text,
# mirroring the ``SubnetInvariantError`` pattern from ADR-0003 / ADR-0004
# Phase 1.
class JoinFlowError(ACNException):
    """Base class for ADR-0004 join-flow rejections.

    Carries a stable ``reason`` slug (one of the ``REASON_*``
    constants exposed by the subclasses' module). The route layer
    catches the base and dispatches on ``isinstance`` to pick the
    right HTTP status, so out-of-tree subclasses participate in
    the same exception flow without re-listing themselves anywhere.
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


class JoinRequestPendingError(JoinFlowError):
    """409 ``JOIN_REQUEST_PENDING`` — agent already has a pending join_request.

    Carries the existing ``request_id`` so the route layer can echo
    it back in the response per ADR §State machine edges
    "Duplicate join request".
    """

    def __init__(self, existing_request_id: str) -> None:
        self.existing_request_id = existing_request_id
        super().__init__(
            "join_request_pending",
            f"pending join_request {existing_request_id} already exists",
        )


class InvitationPendingError(JoinFlowError):
    """409 ``INVITATION_PENDING`` — owner already invited this agent.

    Symmetric partner of :class:`JoinRequestPendingError` for the
    push direction (ADR §State machine edges "Duplicate invitation").
    """

    def __init__(self, existing_invitation_id: str) -> None:
        self.existing_invitation_id = existing_invitation_id
        super().__init__(
            "invitation_pending",
            f"pending invitation {existing_invitation_id} already exists",
        )


class JoinRequestAlreadyDecidedError(JoinFlowError):
    """409 ``JOIN_REQUEST_ALREADY_DECIDED`` — CAS race lost on transition.

    Both owner-side approve / reject and applicant-side withdraw
    raise this when the row has already moved off ``status='pending'``
    by the time the second caller arrives. See ADR §State machine
    edges "Concurrent decision".
    """

    def __init__(self, request_id: str, current_status: str) -> None:
        self.request_id = request_id
        self.current_status = current_status
        super().__init__(
            "join_request_already_decided",
            f"join_request {request_id} is already {current_status}",
        )


class InvitationAlreadyDecidedError(JoinFlowError):
    """409 ``INVITATION_ALREADY_DECIDED`` — symmetric partner for invitations.

    Raised by accept / reject / cancel when the invitation row has
    already moved off ``status='pending'``.
    """

    def __init__(self, invitation_id: str, current_status: str) -> None:
        self.invitation_id = invitation_id
        self.current_status = current_status
        super().__init__(
            "invitation_already_decided",
            f"invitation {invitation_id} is already {current_status}",
        )


class AlreadyMemberError(JoinFlowError):
    """409 ``ALREADY_MEMBER`` — target agent is already a subnet member.

    Raised on the invite path ("Invite an existing member") and the
    self-join path ("Agent self-join a subnet they are already in").
    See ADR §State machine edges for both cases.
    """

    def __init__(self, subnet_id: str, agent_id: str) -> None:
        self.slug = subnet_id
        self.agent_id = agent_id
        super().__init__(
            "already_member",
            f"agent {agent_id} is already a member of subnet {subnet_id}",
        )


class AllowlistEntryExistsError(JoinFlowError):
    """409 ``ALREADY_ON_ALLOWLIST`` — duplicate ``POST /allowlist``.

    Idempotent removal stays a 200 / 204 (no exception). Only the
    add path raises; ADR §"Allowlist endpoints" pins the 409 here.
    """

    def __init__(self, subnet_id: str, agent_id: str) -> None:
        self.slug = subnet_id
        self.agent_id = agent_id
        super().__init__(
            "already_on_allowlist",
            f"agent {agent_id} is already on subnet {subnet_id}'s allowlist",
        )


class JoinRequestNotFoundError(JoinFlowError):
    """404 ``JOIN_REQUEST_NOT_FOUND`` — id missing, or wrong namespace.

    Raised both when ``request_id`` does not exist and when it
    exists but ``row.kind != 'join_request'`` (e.g. the caller hit
    ``/join-requests/{id}`` with an invitation id). ADR §URL alias
    routing rules pins the latter to the same 404 so the two-name
    space split doesn't leak existence of cross-kind ids.
    """

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(
            "join_request_not_found",
            f"join_request {request_id} not found",
        )


class InvitationNotFoundError(JoinFlowError):
    """404 ``INVITATION_NOT_FOUND`` — symmetric partner for invitations."""

    def __init__(self, invitation_id: str) -> None:
        self.invitation_id = invitation_id
        super().__init__(
            "invitation_not_found",
            f"invitation {invitation_id} not found",
        )
