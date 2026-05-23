"""Foundation contract tests — ADR-0004 Phase 2 Slice 2.2.

Pins the shapes of the four building blocks Slice 2.2's domain
layer rests on:

1. :class:`acn.core.interfaces.JoinFlowEventType` — the canonical
   ADR §"Webhook event catalogue" string values (the Slice-2.4
   mapping into ``WebhookEventType`` is a verbatim string lookup,
   so a typo here would silently desync the two enums).

2. :class:`acn.core.interfaces.IJoinFlowEventPublisher` — abstract
   port + the no-op implementation Slice 2.2 binds in
   ``api.py``. Verifies the port is genuinely abstract (you can't
   call ``publish`` on the base) and the no-op short-circuits to
   debug logging without raising.

3. :mod:`acn.services._join_flow_result` — the five
   sealed-union variants ``JoinFlowService.join_subnet`` returns,
   and the matching ``via`` discriminator on the
   invitation-merge variant.

4. ADR-0004 join-flow exception hierarchy in
   :mod:`acn.core.exceptions` — each subclass surfaces a stable
   ``reason`` slug and the right attribute fields for the route
   layer to echo in 4xx responses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from acn.core.entities import Subnet, SubnetJoinRequest
from acn.core.exceptions import (
    ACNException,
    AllowlistEntryExistsError,
    AlreadyMemberError,
    InvitationAlreadyDecidedError,
    InvitationNotFoundError,
    InvitationPendingError,
    JoinFlowError,
    JoinRequestAlreadyDecidedError,
    JoinRequestNotFoundError,
    JoinRequestPendingError,
)
from acn.core.interfaces import (
    IJoinFlowEventPublisher,
    JoinFlowEventType,
)
from acn.services._join_flow_result import (
    JoinFlowAllowlistAutoApprovedResult,
    JoinFlowAutoAcceptedInvitationResult,
    JoinFlowJoinedAsOwnerResult,
    JoinFlowJoinedOpenResult,
    JoinFlowPendingResult,
)
from acn.services._no_op_join_flow_event_publisher import (
    NoOpJoinFlowEventPublisher,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _subnet(slug: str = "s-1", owner: str = "alice") -> Subnet:
    return Subnet(
        slug=slug,
        name=slug,
        owner=owner,
        created_at=datetime.now(UTC),
    )


def _pending_request(
    request_id: str = "rq-1",
    slug: str = "s-1",
    agent_id: str = "bob",
    kind: str = "join_request",
) -> SubnetJoinRequest:
    return SubnetJoinRequest(
        request_id=request_id,
        slug=slug,
        agent_id=agent_id,
        kind=kind,
        status="pending",
        initiated_by=agent_id if kind == "join_request" else "alice",
    )


# ---------------------------------------------------------------------------
# JoinFlowEventType — canonical string values
# ---------------------------------------------------------------------------


class TestJoinFlowEventType:
    """The eight event-type values MUST match ADR §"Webhook event
    catalogue" verbatim — Slice 2.4's webhook publisher does a
    string-equality lookup against the larger
    :class:`WebhookEventType` enum, so any drift would cause silent
    no-ops in production rather than a loud test failure."""

    def test_eight_canonical_event_values(self) -> None:
        # Order doesn't matter; values do. Pin every one.
        actual = {member.value for member in JoinFlowEventType}
        expected = {
            "subnet.join_requested",
            "subnet.join_approved",
            "subnet.join_rejected",
            "subnet.join_withdrawn",
            "subnet.invitation_sent",
            "subnet.invitation_accepted",
            "subnet.invitation_rejected",
            "subnet.invitation_canceled",
        }
        assert actual == expected

    def test_str_enum_yields_string_at_call_sites(self) -> None:
        # StrEnum members ARE strings — webhook send_to expects a
        # WebhookEventType which is also StrEnum, so the Slice-2.4
        # bridge will compare member.value across enums.
        assert JoinFlowEventType.JOIN_APPROVED == "subnet.join_approved"
        assert str(JoinFlowEventType.JOIN_APPROVED) == "subnet.join_approved"


# ---------------------------------------------------------------------------
# IJoinFlowEventPublisher + NoOpJoinFlowEventPublisher
# ---------------------------------------------------------------------------


class TestIJoinFlowEventPublisher:
    def test_abstract_base_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            IJoinFlowEventPublisher()  # type: ignore[abstract]


class TestNoOpJoinFlowEventPublisher:
    """The Slice-2.2 no-op stub is the production publisher until
    Slice 2.4 lands the real webhook adapter. It must satisfy the
    interface trivially and never raise — both ``join_subnet`` and
    the eight ``SubnetService`` transition methods call it without
    a try/except guard."""

    def test_satisfies_interface(self) -> None:
        publisher = NoOpJoinFlowEventPublisher()
        assert isinstance(publisher, IJoinFlowEventPublisher)

    @pytest.mark.asyncio
    async def test_publish_returns_none_and_does_not_raise(self) -> None:
        publisher = NoOpJoinFlowEventPublisher()
        result = await publisher.publish(
            JoinFlowEventType.JOIN_APPROVED,
            subnet=_subnet(),
            request=_pending_request(),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_publish_logs_at_debug_level_with_canonical_fields(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # structlog routes through stdlib logging at the root; the
        # debug breadcrumb is what operators rely on for "Slice 2.2
        # deployment, no webhook fired" audit forensics — pin its
        # presence (without overspecifying the formatter).
        publisher = NoOpJoinFlowEventPublisher()
        publisher_logger = MagicMock()
        from acn.services import _no_op_join_flow_event_publisher as mod

        original = mod.logger
        mod.logger = publisher_logger
        try:
            await publisher.publish(
                JoinFlowEventType.INVITATION_ACCEPTED,
                subnet=_subnet("s-7"),
                request=_pending_request(
                    "rq-9", slug="s-7", agent_id="bob", kind="invitation"
                ),
                trigger="auto_on_join",
                via="self_join",
            )
        finally:
            mod.logger = original

        publisher_logger.debug.assert_called_once()
        kwargs = publisher_logger.debug.call_args.kwargs
        # ``join_flow_event`` rather than ``event`` — structlog
        # reserves the ``event`` keyword for the log message.
        assert kwargs["join_flow_event"] == "subnet.invitation_accepted"
        assert kwargs["slug"] == "s-7"
        assert kwargs["request_id"] == "rq-9"
        assert kwargs["agent_id"] == "bob"
        assert kwargs["trigger"] == "auto_on_join"
        assert kwargs["via"] == "self_join"


# ---------------------------------------------------------------------------
# JoinFlowResult sealed union
# ---------------------------------------------------------------------------


class TestJoinFlowResultVariants:
    """Five variants cover the six §join branches (branches 3 and 4
    share the invitation-auto-accept variant, distinguished by the
    ``via`` discriminator). The route layer matches on these types
    exhaustively in Slice 2.3, so any rename / reshape here breaks
    a downstream pattern-match."""

    def test_joined_open_branch_1(self) -> None:
        result = JoinFlowJoinedOpenResult(slug="s-1", agent_id="bob")
        assert result.slug == "s-1"
        assert result.agent_id == "bob"

    def test_joined_as_owner_branch_2(self) -> None:
        result = JoinFlowJoinedAsOwnerResult(slug="s-1", agent_id="alice")
        assert result.slug == "s-1"
        assert result.agent_id == "alice"

    def test_auto_accepted_invitation_via_self_join_branch_3(self) -> None:
        invite = _pending_request(kind="invitation")
        result = JoinFlowAutoAcceptedInvitationResult(
            slug="s-1",
            agent_id="bob",
            invitation=invite,
            via="self_join",
        )
        assert result.via == "self_join"
        assert result.invitation is invite

    def test_auto_accepted_invitation_via_allowlist_branch_4(self) -> None:
        invite = _pending_request(kind="invitation")
        result = JoinFlowAutoAcceptedInvitationResult(
            slug="s-1",
            agent_id="bob",
            invitation=invite,
            via="allowlist",
        )
        assert result.via == "allowlist"

    def test_allowlist_auto_approved_branch_5(self) -> None:
        req = SubnetJoinRequest(
            request_id="rq-1",
            slug="s-1",
            agent_id="bob",
            kind="allowlist_auto",
            status="approved",
            initiated_by="system:allowlist",
            decided_by="system:allowlist",
            decided_at=datetime.now(UTC),
        )
        result = JoinFlowAllowlistAutoApprovedResult(slug="s-1", agent_id="bob", request=req)
        assert result.request.kind == "allowlist_auto"
        assert result.request.status == "approved"

    def test_pending_branch_6(self) -> None:
        req = _pending_request()
        result = JoinFlowPendingResult(slug="s-1", agent_id="bob", request=req)
        assert result.request.status == "pending"

    def test_variants_are_frozen(self) -> None:
        # Frozen dataclass — protects against accidental mutation
        # at the route ↔ service boundary.
        result = JoinFlowJoinedOpenResult(slug="s-1", agent_id="bob")
        with pytest.raises((AttributeError, Exception)):
            result.slug="s-2"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Exception hierarchy (8 join-flow rejections)
# ---------------------------------------------------------------------------


class TestJoinFlowExceptions:
    """Every join-flow rejection inherits :class:`JoinFlowError` so
    the route layer's ``_join_flow_error_to_acn`` mapper can
    isinstance-dispatch on the base; the per-subclass attribute
    fields (``existing_request_id`` / ``current_status`` /
    ``slug`` / ``agent_id``) are what the route echoes into
    the 4xx response body."""

    def test_join_flow_error_inherits_acn_exception(self) -> None:
        # Base hierarchy keeps the catch-all handler in ``api.py``
        # working (any unhandled ACNException → 500 with a
        # canonical envelope).
        assert issubclass(JoinFlowError, ACNException)

    @pytest.mark.parametrize(
        "exc_cls",
        [
            JoinRequestPendingError,
            InvitationPendingError,
            JoinRequestAlreadyDecidedError,
            InvitationAlreadyDecidedError,
            AlreadyMemberError,
            AllowlistEntryExistsError,
            JoinRequestNotFoundError,
            InvitationNotFoundError,
        ],
    )
    def test_each_subclass_inherits_join_flow_error(self, exc_cls: type[JoinFlowError]) -> None:
        assert issubclass(exc_cls, JoinFlowError)

    def test_join_request_pending_carries_existing_id_and_stable_reason(self) -> None:
        err = JoinRequestPendingError("rq-1")
        assert err.existing_request_id == "rq-1"
        assert err.reason == "join_request_pending"

    def test_invitation_pending_carries_existing_id_and_stable_reason(self) -> None:
        err = InvitationPendingError("inv-1")
        assert err.existing_invitation_id == "inv-1"
        assert err.reason == "invitation_pending"

    def test_join_request_already_decided_carries_status(self) -> None:
        err = JoinRequestAlreadyDecidedError("rq-1", "approved")
        assert err.request_id == "rq-1"
        assert err.current_status == "approved"
        assert err.reason == "join_request_already_decided"

    def test_invitation_already_decided_carries_status(self) -> None:
        err = InvitationAlreadyDecidedError("inv-1", "rejected")
        assert err.invitation_id == "inv-1"
        assert err.current_status == "rejected"
        assert err.reason == "invitation_already_decided"

    def test_already_member_carries_subnet_and_agent(self) -> None:
        err = AlreadyMemberError("s-1", "bob")
        assert err.slug == "s-1"
        assert err.agent_id == "bob"
        assert err.reason == "already_member"

    def test_allowlist_entry_exists_carries_subnet_and_agent(self) -> None:
        err = AllowlistEntryExistsError("s-1", "bob")
        assert err.slug == "s-1"
        assert err.agent_id == "bob"
        assert err.reason == "already_on_allowlist"

    def test_join_request_not_found_carries_request_id(self) -> None:
        err = JoinRequestNotFoundError("rq-1")
        assert err.request_id == "rq-1"
        assert err.reason == "join_request_not_found"

    def test_invitation_not_found_carries_invitation_id(self) -> None:
        err = InvitationNotFoundError("inv-1")
        assert err.invitation_id == "inv-1"
        assert err.reason == "invitation_not_found"
