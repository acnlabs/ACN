"""JoinFlowService — ADR-0004 §join 6-branch dispatcher (Phase 2 Slice 2.2).

The single entry point for ``POST /api/v1/agents/{agent_id}/subnets/
{subnet_id}``. ADR §"POST /api/v1/agents/{agent_id}/subnets/
{subnet_id} (join entry)" specifies a normative six-branch
decision tree on the caller's relationship to the subnet:

1. ``join_policy='open'``                                                 → join immediately
2. ``join_policy='approval'`` AND caller is the subnet owner              → join immediately
3. ``approval`` AND pending invitation for ``(subnet, agent)`` exists     → auto-accept the invitation (``via='self_join'``)
4. ``approval`` AND agent on allowlist AND pending invitation exists      → prefer the invitation (``via='allowlist'``)
5. ``approval`` AND agent on allowlist AND no pending invitation          → auto-create a ``kind='allowlist_auto', status='approved'`` row
6. otherwise                                                              → fall back to ``kind='join_request', status='pending'``

Branches 1–5 are 200s (the caller is, now, a member). Branch 6 is
a 202 (caller is not yet a member, owner owes a decision).

Why a separate service rather than ``SubnetService.join_subnet``
----------------------------------------------------------------
Two reasons:

1. **Composition.** The flow needs to call
   :meth:`SubnetService.add_member` and four other thin CAS
   methods (accept_invitation, etc.) plus
   :meth:`AgentService.join_subnet` — orchestrating them in a
   sibling service keeps :class:`SubnetService` itself focussed
   on single-table writes.
2. **Substitution.** Slice 2.4's CLI and Slice 2.3's HTTP routes
   both call ``JoinFlowService.join_subnet``; subnet-internal
   callers (e.g. the cascade hook that synthetically creates
   ``allowlist_auto`` rows during admin replay) want
   :meth:`SubnetService.add_member` directly, without the
   policy-branch logic. Splitting the two services makes the
   intent at the call site unambiguous.

Branches 3 and 4 are subtly different — both auto-accept a
pending invitation, but branch 4 fires when the agent is ALSO on
the allowlist; ADR §"State machine edges" "Allowlist hit AND
pending invitation" pins the resolution to invitation acceptance
(NOT a parallel ``allowlist_auto`` row) with
``decided_by='system:allowlist'`` so the audit trail says
"approved because pre-authorised by the allowlist", not "approved
because the invitee accepted". The :class:`JoinFlowResult`
sealed union carries the discriminator forward via the
``via='self_join'`` vs ``via='allowlist'`` field on
:class:`JoinFlowAutoAcceptedInvitationResult`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog  # type: ignore[import-untyped]

from ..core.entities import SubnetJoinRequest
from ..core.entities.subnet_join_request import SYSTEM_ALLOWLIST_ACTOR
from ..core.exceptions import (
    AlreadyMemberError,
    JoinRequestPendingError,
)
from ..core.interfaces import (
    IJoinFlowEventPublisher,
    ISubnetAllowlistRepository,
    ISubnetJoinRequestRepository,
    JoinFlowEventType,
)
from ._join_flow_result import (
    JoinFlowAllowlistAutoApprovedResult,
    JoinFlowAutoAcceptedInvitationResult,
    JoinFlowJoinedAsOwnerResult,
    JoinFlowJoinedOpenResult,
    JoinFlowPendingResult,
    JoinFlowResult,
)
from ._no_op_join_flow_event_publisher import NoOpJoinFlowEventPublisher
from .subnet_service import SubnetService

logger = structlog.get_logger()


class JoinFlowService:
    """Implements the ADR-0004 §join six-branch decision tree.

    Composed over :class:`SubnetService` (for the thin CAS methods +
    add_member) and the two new repositories (for the membership /
    pending / allowlist checks). The agent-side back-reference
    (``agent.subnet_ids``) is intentionally NOT touched here — Slice
    2.3's route layer continues to call ``agent_service.join_subnet``
    around the service call, matching the pre-existing
    ``do_join_subnet`` flow in ``acn/routes/_subnet_membership.py``.
    """

    def __init__(
        self,
        subnet_service: SubnetService,
        join_request_repository: ISubnetJoinRequestRepository,
        allowlist_repository: ISubnetAllowlistRepository,
        event_publisher: IJoinFlowEventPublisher | None = None,
    ) -> None:
        """Wire dependencies.

        Args:
            subnet_service: For ``get_subnet``, ``add_member``, and
                the four thin CAS verbs the merge / pending paths
                delegate to (``accept_invitation``,
                ``approve_join_request`` — used internally by
                ``invite_agent`` not here).
            join_request_repository: For ``find_pending_for`` and
                ``save`` of fresh ``allowlist_auto`` / ``join_request``
                rows.
            allowlist_repository: For the ``is_member`` check on
                branches 4 and 5.
            event_publisher: Defaults to the no-op stub for parity
                with :class:`SubnetService`'s constructor.
        """
        self._subnet_service = subnet_service
        self._join_request_repository = join_request_repository
        self._allowlist_repository = allowlist_repository
        self._event_publisher: IJoinFlowEventPublisher = (
            event_publisher or NoOpJoinFlowEventPublisher()
        )

    async def join_subnet(self, slug: str, agent_id: str) -> JoinFlowResult:
        """Dispatch the six branches.

        Note on branch ordering: ADR §join states "the branch order
        matters and is normative". The branches are checked top to
        bottom; the first one to match wins. In particular, branch 2
        (owner self-join) checks the owner ID **after** the open
        check, so an open subnet with the owner calling it still
        takes branch 1 (open) — semantically equivalent (owner is a
        member either way) but the response shape is the "open" 200.
        """
        subnet = await self._subnet_service.get_subnet(slug)

        # ADR §State machine edges "Agent self-join a subnet they
        # are already in" → 409 ALREADY_MEMBER. Check applies to
        # every branch including open, so we hoist it above the
        # branch dispatch.
        if agent_id in subnet.member_agent_ids:
            raise AlreadyMemberError(slug, agent_id)

        # Branch 1 — open subnet, immediate add_member.
        if subnet.join_policy == "open":
            await self._subnet_service.add_member(slug, agent_id)
            logger.info(
                "join_flow_branch_open",
                slug=slug,
                agent_id=agent_id,
                branch=1,
            )
            return JoinFlowJoinedOpenResult(slug=slug, agent_id=agent_id)

        # From here on ``join_policy == 'approval'``.

        # Branch 2 — owner self-joins. The owner is canonically a
        # member of their subnet (ADR §State machine edges), so the
        # request table is bypassed entirely.
        if agent_id == subnet.owner:
            await self._subnet_service.add_member(slug, agent_id)
            logger.info(
                "join_flow_branch_owner_self_join",
                slug=slug,
                agent_id=agent_id,
                branch=2,
            )
            return JoinFlowJoinedAsOwnerResult(slug=slug, agent_id=agent_id)

        # The three remaining branches all hinge on the pending row
        # / allowlist presence. Compute both up front so the
        # decision tree is a straight ``if/elif`` chain.
        pending = await self._join_request_repository.find_pending_for(slug, agent_id)
        is_allowlisted = await self._allowlist_repository.is_member(slug, agent_id)

        # Branches 3 + 4 — pending invitation auto-accept.
        if pending is not None and pending.kind == "invitation":
            via = "allowlist" if is_allowlisted else "self_join"
            accepted = await self._subnet_service.accept_invitation(
                slug,
                pending.request_id,
                # On the allowlist merge path ADR §"Merge-path event
                # mapping" pins decided_by='system:allowlist'; on
                # plain self-join it stays the invitee_id. We pass
                # the invitee_id either way and let accept_invitation
                # rewrite to ``system:allowlist`` when via='allowlist'.
                invitee_id=agent_id,
                trigger="auto_on_join",
                via=via,
            )
            logger.info(
                "join_flow_branch_invitation_auto_accept",
                slug=slug,
                agent_id=agent_id,
                invitation_id=accepted.request_id,
                via=via,
                branch=4 if is_allowlisted else 3,
            )
            return JoinFlowAutoAcceptedInvitationResult(
                slug=slug,
                agent_id=agent_id,
                invitation=accepted,
                via=via,
            )

        # An ``allowlist_auto`` row in ``pending`` is structurally
        # impossible (born approved), but if a pending join_request
        # somehow co-exists with an invite-driven re-entry the
        # entity-level uniqueness invariant guarantees only one
        # pending row per (subnet, agent). We reject the
        # join_request collision with the 409 ADR §State machine
        # edges "Duplicate join request" pins.
        if pending is not None and pending.kind == "join_request":
            raise JoinRequestPendingError(pending.request_id)

        # Branch 5 — allowlisted (no pending invitation, no pending
        # join_request). Create an ``allowlist_auto`` row born
        # approved and add_member.
        if is_allowlisted:
            request = SubnetJoinRequest(
                request_id=str(uuid.uuid4()),
                slug=slug,
                agent_id=agent_id,
                kind="allowlist_auto",
                status="approved",
                initiated_by=SYSTEM_ALLOWLIST_ACTOR,
                decided_by=SYSTEM_ALLOWLIST_ACTOR,
                decided_at=datetime.now(UTC),
            )
            await self._join_request_repository.save(request)
            await self._subnet_service.add_member(slug, agent_id)
            # Slice 2.2 emits ``JOIN_APPROVED`` for the auto-approval —
            # branch 5 doesn't go through join_request → approved, it
            # is born approved, so the canonical "approval happened"
            # signal is what fires. ADR §Webhook event catalogue
            # explicitly lists this event family with
            # ``decided_by="system:allowlist"`` for the auto path.
            await self._event_publisher.publish(
                JoinFlowEventType.JOIN_APPROVED,
                subnet=subnet,
                request=request,
                trigger="auto_on_join",
                via="allowlist",
            )
            logger.info(
                "join_flow_branch_allowlist_auto_approved",
                slug=slug,
                agent_id=agent_id,
                request_id=request.request_id,
                branch=5,
            )
            return JoinFlowAllowlistAutoApprovedResult(
                slug=slug,
                agent_id=agent_id,
                request=request,
            )

        # Branch 6 — fall-through. Create a pending join_request.
        # NOTE: NO ``add_member`` here — the caller is not yet a
        # member; the owner owes a decision.
        request = SubnetJoinRequest(
            request_id=str(uuid.uuid4()),
            slug=slug,
            agent_id=agent_id,
            kind="join_request",
            status="pending",
            initiated_by=agent_id,
        )
        await self._join_request_repository.save(request)
        await self._event_publisher.publish(
            JoinFlowEventType.JOIN_REQUESTED,
            subnet=subnet,
            request=request,
        )
        logger.info(
            "join_flow_branch_pending_join_request",
            slug=slug,
            agent_id=agent_id,
            request_id=request.request_id,
            branch=6,
        )
        return JoinFlowPendingResult(
            slug=slug,
            agent_id=agent_id,
            request=request,
        )
