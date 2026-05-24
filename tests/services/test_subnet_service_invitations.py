"""SubnetService — invitation lifecycle + merge path (ADR-0004 Slice 2.2).

Covers the four invitation methods on ``SubnetService``:

- ``invite_agent``      — owner pushes. May take the normal path
  (fresh invitation row + ``INVITATION_SENT``) OR the merge path
  (target has a pending join_request → auto-approve + ``JOIN_APPROVED``
  with ``trigger='auto_on_invite'``).
- ``accept_invitation`` — invitee says yes; agent joins. The
  ``trigger`` / ``via`` kwargs are the hooks ``JoinFlowService``
  uses for branches 3 (``via='self_join'``) and 4
  (``via='allowlist'``).
- ``reject_invitation`` — invitee says no.
- ``cancel_invitation`` — owner-side withdraw.

State-machine edges from ADR §"State machine edges" that show up
here: "Duplicate invitation", "Invite an existing member",
"Join request pending when owner invites" (the merge path),
"Concurrent decision" (CAS race on accept / reject / cancel).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.entities import Subnet, SubnetJoinRequest
from acn.core.entities.subnet_join_request import SYSTEM_ALLOWLIST_ACTOR
from acn.core.exceptions import (
    AlreadyMemberError,
    InvitationAlreadyDecidedError,
    InvitationNotFoundError,
    InvitationPendingError,
)
from acn.core.interfaces import (
    IJoinFlowEventPublisher,
    ISubnetJoinRequestRepository,
    ISubnetRepository,
    JoinFlowEventType,
)
from acn.services._join_flow_result import (
    InviteAgentMergedToApprovedJoinRequestResult,
    InviteAgentSentResult,
)
from acn.services.subnet_service import SubnetService


def _subnet(
    slug: str = "s-1",
    owner: str = "alice",
    members: set[str] | None = None,
) -> Subnet:
    return Subnet(
        slug=slug,
        name=slug,
        owner=owner,
        created_at=datetime.now(UTC),
        member_agent_ids=members if members is not None else {owner},
    )


def _pending_invitation(
    request_id: str = "inv-1",
    slug: str = "s-1",
    agent_id: str = "bob",
    initiated_by: str = "alice",
) -> SubnetJoinRequest:
    return SubnetJoinRequest(
        request_id=request_id,
        slug=slug,
        agent_id=agent_id,
        kind="invitation",
        status="pending",
        initiated_by=initiated_by,
    )


def _pending_join_request(
    request_id: str = "rq-1",
    slug: str = "s-1",
    agent_id: str = "bob",
) -> SubnetJoinRequest:
    return SubnetJoinRequest(
        request_id=request_id,
        slug=slug,
        agent_id=agent_id,
        kind="join_request",
        status="pending",
        initiated_by=agent_id,
    )


@pytest.fixture
def mock_subnet_repo() -> AsyncMock:
    repo = AsyncMock(spec=ISubnetRepository)
    repo.find_by_id.return_value = _subnet()
    return repo


@pytest.fixture
def mock_join_repo() -> AsyncMock:
    repo = AsyncMock(spec=ISubnetJoinRequestRepository)
    repo.find_pending_for.return_value = None  # default: no collision
    return repo


@pytest.fixture
def mock_publisher() -> MagicMock:
    publisher = MagicMock(spec=IJoinFlowEventPublisher)
    publisher.publish = AsyncMock()
    return publisher


@pytest.fixture
def service(
    mock_subnet_repo: AsyncMock,
    mock_join_repo: AsyncMock,
    mock_publisher: MagicMock,
) -> SubnetService:
    return SubnetService(
        subnet_repository=mock_subnet_repo,
        subnet_join_request_repository=mock_join_repo,
        join_flow_event_publisher=mock_publisher,
    )


class TestInviteAgentNormalPath:
    @pytest.mark.asyncio
    async def test_creates_pending_invitation_row(
        self,
        service: SubnetService,
        mock_join_repo: AsyncMock,
        mock_publisher: MagicMock,
    ) -> None:
        result = await service.invite_agent("s-1", "bob", owner_id="alice", note="join us")

        assert isinstance(result, InviteAgentSentResult)
        assert result.invitation.kind == "invitation"
        assert result.invitation.status == "pending"
        assert result.invitation.initiated_by == "alice"
        assert result.invitation.agent_id == "bob"
        assert result.invitation.note == "join us"

        mock_join_repo.save.assert_awaited_once()
        mock_publisher.publish.assert_awaited_once()
        assert mock_publisher.publish.await_args.args[0] == JoinFlowEventType.INVITATION_SENT

    @pytest.mark.asyncio
    async def test_already_member_raises_409(
        self, service: SubnetService, mock_subnet_repo: AsyncMock
    ) -> None:
        # ADR §State machine edges "Invite an existing member"
        mock_subnet_repo.find_by_id.return_value = _subnet(members={"alice", "bob"})

        with pytest.raises(AlreadyMemberError) as exc_info:
            await service.invite_agent("s-1", "bob", owner_id="alice")
        assert exc_info.value.slug == "s-1"
        assert exc_info.value.agent_id == "bob"

    @pytest.mark.asyncio
    async def test_duplicate_pending_invitation_raises_409(
        self, service: SubnetService, mock_join_repo: AsyncMock
    ) -> None:
        # ADR §State machine edges "Duplicate invitation" — owner
        # re-invites while a pending invitation exists.
        existing = _pending_invitation(request_id="inv-OLD")
        mock_join_repo.find_pending_for.return_value = existing

        with pytest.raises(InvitationPendingError) as exc_info:
            await service.invite_agent("s-1", "bob", owner_id="alice")
        assert exc_info.value.existing_invitation_id == "inv-OLD"


class TestInviteAgentMergePath:
    """ADR §"POST /invitations" "Merge path" — owner invites an
    agent who already has a pending ``join_request``. The pending
    row CAS's to ``approved`` with ``trigger=auto_on_invite``; no
    invitation row is created; ``JOIN_APPROVED`` fires (NOT
    ``INVITATION_SENT``)."""

    @pytest.mark.asyncio
    async def test_pending_join_request_merges_to_approved(
        self,
        service: SubnetService,
        mock_join_repo: AsyncMock,
        mock_subnet_repo: AsyncMock,
        mock_publisher: MagicMock,
    ) -> None:
        pending = _pending_join_request(request_id="rq-EXISTING")
        mock_join_repo.find_pending_for.return_value = pending
        # The merge path calls approve_join_request which re-loads
        # the row via find_by_id.
        mock_join_repo.find_by_id.return_value = pending

        result = await service.invite_agent("s-1", "bob", owner_id="alice")

        assert isinstance(result, InviteAgentMergedToApprovedJoinRequestResult)
        assert result.request.request_id == "rq-EXISTING"
        assert result.request.status == "approved"
        assert result.request.decided_by == "alice"

        # Exactly one webhook fires — JOIN_APPROVED with
        # trigger=auto_on_invite — and NO INVITATION_SENT.
        mock_publisher.publish.assert_awaited_once()
        call = mock_publisher.publish.await_args
        assert call.args[0] == JoinFlowEventType.JOIN_APPROVED
        assert call.kwargs["trigger"] == "auto_on_invite"

        # add_member ran — verified by the subnet repo save call.
        mock_subnet_repo.save.assert_awaited()


class TestAcceptInvitation:
    @pytest.mark.asyncio
    async def test_happy_path_explicit_invitee_accept(
        self,
        service: SubnetService,
        mock_join_repo: AsyncMock,
        mock_subnet_repo: AsyncMock,
        mock_publisher: MagicMock,
    ) -> None:
        mock_join_repo.find_by_id.return_value = _pending_invitation()

        result = await service.accept_invitation("s-1", "inv-1", invitee_id="bob")

        assert result.status == "approved"
        assert result.decided_by == "bob"

        mock_subnet_repo.save.assert_awaited()  # add_member ran

        call = mock_publisher.publish.await_args
        assert call.args[0] == JoinFlowEventType.INVITATION_ACCEPTED
        assert call.kwargs["trigger"] == "explicit"
        assert call.kwargs["via"] is None

    @pytest.mark.asyncio
    async def test_self_join_merge_via_self_join(
        self,
        service: SubnetService,
        mock_join_repo: AsyncMock,
        mock_publisher: MagicMock,
    ) -> None:
        # Branch 3 in §join — agent self-joins with pending invitation.
        # JoinFlowService passes trigger=auto_on_join, via=self_join;
        # decided_by remains the invitee_id (the agent self-joining).
        mock_join_repo.find_by_id.return_value = _pending_invitation()

        result = await service.accept_invitation(
            "s-1",
            "inv-1",
            invitee_id="bob",
            trigger="auto_on_join",
            via="self_join",
        )

        assert result.decided_by == "bob"
        call = mock_publisher.publish.await_args
        assert call.kwargs["trigger"] == "auto_on_join"
        assert call.kwargs["via"] == "self_join"

    @pytest.mark.asyncio
    async def test_allowlist_merge_decides_by_system_allowlist(
        self,
        service: SubnetService,
        mock_join_repo: AsyncMock,
        mock_publisher: MagicMock,
    ) -> None:
        # Branch 4 — allowlist hit AND pending invitation. ADR
        # §"Merge-path event mapping" pins
        # decided_by='system:allowlist' on this specific path,
        # making the audit trail say "this approval came from the
        # allowlist preauth", not "the invitee chose this".
        mock_join_repo.find_by_id.return_value = _pending_invitation()

        result = await service.accept_invitation(
            "s-1",
            "inv-1",
            invitee_id="bob",
            trigger="auto_on_join",
            via="allowlist",
        )

        assert result.decided_by == SYSTEM_ALLOWLIST_ACTOR
        call = mock_publisher.publish.await_args
        assert call.kwargs["via"] == "allowlist"

    @pytest.mark.asyncio
    async def test_already_decided_raises_409(
        self, service: SubnetService, mock_join_repo: AsyncMock
    ) -> None:
        decided = SubnetJoinRequest(
            request_id="inv-1",
            slug="s-1",
            agent_id="bob",
            kind="invitation",
            status="approved",
            initiated_by="alice",
            decided_by="bob",
            decided_at=datetime.now(UTC),
        )
        mock_join_repo.find_by_id.return_value = decided

        with pytest.raises(InvitationAlreadyDecidedError) as exc_info:
            await service.accept_invitation("s-1", "inv-1", invitee_id="bob")
        assert exc_info.value.invitation_id == "inv-1"
        assert exc_info.value.current_status == "approved"

    @pytest.mark.asyncio
    async def test_wrong_kind_returns_invitation_not_found(
        self, service: SubnetService, mock_join_repo: AsyncMock
    ) -> None:
        # join_request id used against accept_invitation → 404
        # INVITATION_NOT_FOUND (ADR §URL alias routing rules).
        mock_join_repo.find_by_id.return_value = _pending_join_request()
        with pytest.raises(InvitationNotFoundError):
            await service.accept_invitation("s-1", "rq-1", invitee_id="bob")

    @pytest.mark.asyncio
    async def test_missing_returns_invitation_not_found(
        self, service: SubnetService, mock_join_repo: AsyncMock
    ) -> None:
        mock_join_repo.find_by_id.return_value = None
        with pytest.raises(InvitationNotFoundError):
            await service.accept_invitation("s-1", "inv-1", invitee_id="bob")


class TestRejectInvitation:
    @pytest.mark.asyncio
    async def test_happy_path(
        self,
        service: SubnetService,
        mock_join_repo: AsyncMock,
        mock_publisher: MagicMock,
    ) -> None:
        mock_join_repo.find_by_id.return_value = _pending_invitation()

        result = await service.reject_invitation("s-1", "inv-1", invitee_id="bob", note="not now")

        assert result.status == "rejected"
        assert result.decided_by == "bob"
        assert result.note == "not now"

        call = mock_publisher.publish.await_args
        assert call.args[0] == JoinFlowEventType.INVITATION_REJECTED


class TestCancelInvitation:
    @pytest.mark.asyncio
    async def test_happy_path(
        self,
        service: SubnetService,
        mock_join_repo: AsyncMock,
        mock_publisher: MagicMock,
    ) -> None:
        mock_join_repo.find_by_id.return_value = _pending_invitation()

        result = await service.cancel_invitation("s-1", "inv-1", owner_id="alice")

        assert result.status == "withdrawn"
        assert result.decided_by == "alice"

        call = mock_publisher.publish.await_args
        assert call.args[0] == JoinFlowEventType.INVITATION_CANCELED

    @pytest.mark.asyncio
    async def test_already_decided_raises_409(
        self, service: SubnetService, mock_join_repo: AsyncMock
    ) -> None:
        decided = SubnetJoinRequest(
            request_id="inv-1",
            slug="s-1",
            agent_id="bob",
            kind="invitation",
            status="rejected",
            initiated_by="alice",
            decided_by="bob",
            decided_at=datetime.now(UTC),
        )
        mock_join_repo.find_by_id.return_value = decided

        with pytest.raises(InvitationAlreadyDecidedError):
            await service.cancel_invitation("s-1", "inv-1", owner_id="alice")


class TestReadPathsInvitations:
    @pytest.mark.asyncio
    async def test_list_invitations_pins_kind_invitation(
        self, service: SubnetService, mock_join_repo: AsyncMock
    ) -> None:
        mock_join_repo.list_by_subnet.return_value = []
        await service.list_invitations("s-1", status="pending")
        mock_join_repo.list_by_subnet.assert_awaited_once_with(
            "s-1", kind="invitation", status="pending", limit=100, offset=0
        )

    @pytest.mark.asyncio
    async def test_list_pending_invitations_for_agent_delegates_to_repo(
        self, service: SubnetService, mock_join_repo: AsyncMock
    ) -> None:
        invites = [_pending_invitation(request_id="inv-A")]
        mock_join_repo.list_pending_invitations_for_agent.return_value = invites

        result = await service.list_pending_invitations_for_agent("bob")
        assert result == invites
        mock_join_repo.list_pending_invitations_for_agent.assert_awaited_once_with("bob")
