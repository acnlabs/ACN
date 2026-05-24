"""JoinFlowService — six-branch decision tree tests (ADR-0004 Slice 2.2).

Each of the six branches in ADR §"POST /api/v1/agents/{agent_id}/
subnets/{slug} (join entry)" is verified with its happy path
plus the relevant ``State machine edges`` markers from the ADR.

Branch matrix:

| # | join_policy | caller relationship                         | result                                         |
|---|-------------|---------------------------------------------|------------------------------------------------|
| 1 | open        | any                                         | JoinFlowJoinedOpenResult                       |
| 2 | approval    | caller == owner                             | JoinFlowJoinedAsOwnerResult                    |
| 3 | approval    | pending invitation, NOT allowlisted         | JoinFlowAutoAcceptedInvitationResult(self_join)|
| 4 | approval    | pending invitation AND allowlisted          | JoinFlowAutoAcceptedInvitationResult(allowlist)|
| 5 | approval    | allowlisted, no pending invitation          | JoinFlowAllowlistAutoApprovedResult            |
| 6 | approval    | not owner / not allowlisted / no pending    | JoinFlowPendingResult                          |

State-machine edges covered:

* "Owner self-joins their own subnet"          → branch 2 fast-path
* "Invitation pending when agent self-joins"   → branch 3 auto-accept
* "Allowlist hit AND pending invitation"       → branch 4 (invitation wins)
* "Allowlist removal then leave then rejoin"   → branch 6 (rejoin path)
* "Agent self-join already a member"           → 409 ALREADY_MEMBER
* "Duplicate join request"                     → 409 JOIN_REQUEST_PENDING
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.entities import Subnet, SubnetJoinRequest
from acn.core.entities.subnet_join_request import SYSTEM_ALLOWLIST_ACTOR
from acn.core.exceptions import (
    AlreadyMemberError,
    JoinRequestPendingError,
)
from acn.core.interfaces import (
    IJoinFlowEventPublisher,
    ISubnetAllowlistRepository,
    ISubnetJoinRequestRepository,
    ISubnetRepository,
    JoinFlowEventType,
)
from acn.services._join_flow_result import (
    JoinFlowAllowlistAutoApprovedResult,
    JoinFlowAutoAcceptedInvitationResult,
    JoinFlowJoinedAsOwnerResult,
    JoinFlowJoinedOpenResult,
    JoinFlowPendingResult,
)
from acn.services.join_flow_service import JoinFlowService
from acn.services.subnet_service import SubnetService


def _subnet(
    slug: str = "s-1",
    owner: str = "alice",
    join_policy: str = "approval",
    members: set[str] | None = None,
    is_private: bool = False,
) -> Subnet:
    return Subnet(
        slug=slug,
        name=slug,
        owner=owner,
        is_private=is_private,
        join_policy=join_policy,
        created_at=datetime.now(UTC),
        member_agent_ids=members if members is not None else {owner},
    )


def _pending_invitation(
    request_id: str = "inv-1",
    slug: str = "s-1",
    agent_id: str = "bob",
) -> SubnetJoinRequest:
    return SubnetJoinRequest(
        request_id=request_id,
        slug=slug,
        agent_id=agent_id,
        kind="invitation",
        status="pending",
        initiated_by="alice",
    )


def _pending_join_request(
    request_id: str = "rq-OLD",
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
    repo.find_pending_for.return_value = None
    return repo


@pytest.fixture
def mock_allowlist_repo() -> AsyncMock:
    repo = AsyncMock(spec=ISubnetAllowlistRepository)
    repo.is_member.return_value = False
    return repo


@pytest.fixture
def mock_publisher() -> MagicMock:
    publisher = MagicMock(spec=IJoinFlowEventPublisher)
    publisher.publish = AsyncMock()
    return publisher


@pytest.fixture
def subnet_service(
    mock_subnet_repo: AsyncMock,
    mock_join_repo: AsyncMock,
    mock_allowlist_repo: AsyncMock,
    mock_publisher: MagicMock,
) -> SubnetService:
    # The JoinFlowService composes over SubnetService; share the
    # same publisher / repos so a test can introspect a single
    # event-emit chain regardless of which service emitted.
    return SubnetService(
        subnet_repository=mock_subnet_repo,
        subnet_join_request_repository=mock_join_repo,
        subnet_allowlist_repository=mock_allowlist_repo,
        join_flow_event_publisher=mock_publisher,
    )


@pytest.fixture
def service(
    subnet_service: SubnetService,
    mock_join_repo: AsyncMock,
    mock_allowlist_repo: AsyncMock,
    mock_publisher: MagicMock,
) -> JoinFlowService:
    return JoinFlowService(
        subnet_service=subnet_service,
        join_request_repository=mock_join_repo,
        allowlist_repository=mock_allowlist_repo,
        event_publisher=mock_publisher,
    )


# ============================================================
# Branch 1 — open subnet
# ============================================================


class TestBranch1Open:
    @pytest.mark.asyncio
    async def test_open_subnet_immediate_join_no_request_row(
        self,
        service: JoinFlowService,
        mock_subnet_repo: AsyncMock,
        mock_join_repo: AsyncMock,
        mock_publisher: MagicMock,
    ) -> None:
        mock_subnet_repo.find_by_id.return_value = _subnet(join_policy="open")

        result = await service.join_subnet("s-1", "bob")

        assert isinstance(result, JoinFlowJoinedOpenResult)
        assert result.slug == "s-1"
        assert result.agent_id == "bob"

        # NO row created in subnet_join_requests.
        mock_join_repo.save.assert_not_called()

        # NO webhook published — open joins are implicit (no
        # lifecycle event in ADR §"Webhook event catalogue").
        mock_publisher.publish.assert_not_called()

        # add_member ran — subnet saved.
        mock_subnet_repo.save.assert_awaited()

    @pytest.mark.asyncio
    async def test_open_subnet_owner_takes_branch_1_not_2(
        self,
        service: JoinFlowService,
        mock_subnet_repo: AsyncMock,
    ) -> None:
        # ADR §join "branch order matters" — open + owner self-join
        # takes branch 1 (the open check is first).
        subnet = _subnet(join_policy="open", owner="alice", members={"alice"})
        subnet.member_agent_ids = set()  # owner not yet in members
        mock_subnet_repo.find_by_id.return_value = subnet

        result = await service.join_subnet("s-1", "alice")
        assert isinstance(result, JoinFlowJoinedOpenResult)


# ============================================================
# Branch 2 — approval + caller is owner
# ============================================================


class TestBranch2OwnerSelfJoin:
    @pytest.mark.asyncio
    async def test_owner_self_join_bypasses_request_table(
        self,
        service: JoinFlowService,
        mock_subnet_repo: AsyncMock,
        mock_join_repo: AsyncMock,
        mock_publisher: MagicMock,
    ) -> None:
        # ADR §State machine edges "Owner self-joins their own
        # subnet". Owner is canonically a member from creation
        # already, but defence-in-depth on direct join calls.
        subnet = _subnet(join_policy="approval", owner="alice")
        subnet.member_agent_ids = set()
        mock_subnet_repo.find_by_id.return_value = subnet

        result = await service.join_subnet("s-1", "alice")

        assert isinstance(result, JoinFlowJoinedAsOwnerResult)
        mock_join_repo.save.assert_not_called()
        mock_publisher.publish.assert_not_called()


# ============================================================
# Branch 3 — approval + pending invitation (NOT allowlisted)
# ============================================================


class TestBranch3InvitationSelfJoin:
    @pytest.mark.asyncio
    async def test_pending_invitation_auto_accept_via_self_join(
        self,
        service: JoinFlowService,
        mock_join_repo: AsyncMock,
        mock_allowlist_repo: AsyncMock,
        mock_publisher: MagicMock,
    ) -> None:
        invite = _pending_invitation()
        mock_join_repo.find_pending_for.return_value = invite
        mock_join_repo.find_by_id.return_value = invite
        mock_allowlist_repo.is_member.return_value = False

        result = await service.join_subnet("s-1", "bob")

        assert isinstance(result, JoinFlowAutoAcceptedInvitationResult)
        assert result.via == "self_join"
        assert result.invitation.status == "approved"
        # decided_by is the invitee (NOT system:allowlist) for
        # plain self-join — see ADR §State transition table.
        assert result.invitation.decided_by == "bob"

        # INVITATION_ACCEPTED fired with trigger=auto_on_join,
        # via=self_join.
        mock_publisher.publish.assert_awaited_once()
        call = mock_publisher.publish.await_args
        assert call.args[0] == JoinFlowEventType.INVITATION_ACCEPTED
        assert call.kwargs["trigger"] == "auto_on_join"
        assert call.kwargs["via"] == "self_join"


# ============================================================
# Branch 4 — approval + allowlist hit AND pending invitation
# ============================================================


class TestBranch4InvitationWithAllowlistMerge:
    @pytest.mark.asyncio
    async def test_invitation_wins_over_allowlist_auto(
        self,
        service: JoinFlowService,
        mock_join_repo: AsyncMock,
        mock_allowlist_repo: AsyncMock,
        mock_publisher: MagicMock,
    ) -> None:
        # ADR §State machine edges "Allowlist hit AND pending
        # invitation". Crucial: NO allowlist_auto row created.
        # The invitation is the one row that survives, with
        # decided_by='system:allowlist'.
        invite = _pending_invitation()
        mock_join_repo.find_pending_for.return_value = invite
        mock_join_repo.find_by_id.return_value = invite
        mock_allowlist_repo.is_member.return_value = True

        result = await service.join_subnet("s-1", "bob")

        assert isinstance(result, JoinFlowAutoAcceptedInvitationResult)
        assert result.via == "allowlist"
        # ADR §Merge-path event mapping pins decided_by to
        # 'system:allowlist' on this specific path.
        assert result.invitation.decided_by == SYSTEM_ALLOWLIST_ACTOR

        call = mock_publisher.publish.await_args
        assert call.args[0] == JoinFlowEventType.INVITATION_ACCEPTED
        assert call.kwargs["via"] == "allowlist"

        # The save count: only the invitation's transition was
        # written (CAS to approved). No separate allowlist_auto row.
        # The mock counts each save() call — one CAS transition.
        assert mock_join_repo.save.await_count == 1


# ============================================================
# Branch 5 — approval + allowlisted (no pending invitation)
# ============================================================


class TestBranch5AllowlistAutoApproved:
    @pytest.mark.asyncio
    async def test_allowlist_creates_born_approved_row(
        self,
        service: JoinFlowService,
        mock_subnet_repo: AsyncMock,
        mock_join_repo: AsyncMock,
        mock_allowlist_repo: AsyncMock,
        mock_publisher: MagicMock,
    ) -> None:
        mock_allowlist_repo.is_member.return_value = True

        result = await service.join_subnet("s-1", "bob")

        assert isinstance(result, JoinFlowAllowlistAutoApprovedResult)
        assert result.request.kind == "allowlist_auto"
        assert result.request.status == "approved"
        assert result.request.initiated_by == SYSTEM_ALLOWLIST_ACTOR
        assert result.request.decided_by == SYSTEM_ALLOWLIST_ACTOR

        # Row saved via join_request_repository.save (NOT the CAS path).
        mock_join_repo.save.assert_awaited_once()

        # add_member ran.
        mock_subnet_repo.save.assert_awaited()

        # JOIN_APPROVED fires with trigger=auto_on_join, via=allowlist.
        # (Not INVITATION_ACCEPTED — there's no invitation here.)
        call = mock_publisher.publish.await_args
        assert call.args[0] == JoinFlowEventType.JOIN_APPROVED
        assert call.kwargs["trigger"] == "auto_on_join"
        assert call.kwargs["via"] == "allowlist"


# ============================================================
# Branch 6 — fallback: create pending join_request
# ============================================================


class TestBranch6PendingJoinRequest:
    @pytest.mark.asyncio
    async def test_no_pending_no_allowlist_creates_pending(
        self,
        service: JoinFlowService,
        mock_subnet_repo: AsyncMock,
        mock_join_repo: AsyncMock,
        mock_publisher: MagicMock,
    ) -> None:
        result = await service.join_subnet("s-1", "bob")

        assert isinstance(result, JoinFlowPendingResult)
        assert result.request.kind == "join_request"
        assert result.request.status == "pending"
        assert result.request.initiated_by == "bob"

        # NO add_member on branch 6 — applicant is not yet a member.
        # add_member would call subnet_repo.save — verify no save fired
        # on the subnet side.
        # NOTE: mock_subnet_repo.find_by_id is called (read), but
        # save isn't. We pin save specifically.
        mock_subnet_repo.save.assert_not_called()

        # JOIN_REQUESTED fires (the "ask submitted" signal).
        call = mock_publisher.publish.await_args
        assert call.args[0] == JoinFlowEventType.JOIN_REQUESTED

    @pytest.mark.asyncio
    async def test_allowlist_removed_then_rejoin_takes_branch_6(
        self,
        service: JoinFlowService,
        mock_subnet_repo: AsyncMock,
        mock_allowlist_repo: AsyncMock,
    ) -> None:
        # ADR §State machine edges "Allowlist removal then leave
        # then rejoin". Agent A was on the allowlist, joined,
        # owner removed A, A leaves, A re-joins → branch 6 (fresh
        # join_request) because the allowlist no longer matches.
        # The agent is not currently a member (left earlier).
        subnet = _subnet(join_policy="approval", members={"alice"})
        mock_subnet_repo.find_by_id.return_value = subnet
        mock_allowlist_repo.is_member.return_value = False

        result = await service.join_subnet("s-1", "bob")
        assert isinstance(result, JoinFlowPendingResult)


# ============================================================
# Cross-cutting state-machine edges
# ============================================================


class TestStateMachineEdges:
    @pytest.mark.asyncio
    async def test_agent_already_member_raises_409(
        self, service: JoinFlowService, mock_subnet_repo: AsyncMock
    ) -> None:
        # ADR §State machine edges "Agent self-join a subnet they
        # are already in" → 409 ALREADY_MEMBER. Checked BEFORE
        # branch dispatch.
        subnet = _subnet(join_policy="approval", members={"alice", "bob"})
        mock_subnet_repo.find_by_id.return_value = subnet

        with pytest.raises(AlreadyMemberError):
            await service.join_subnet("s-1", "bob")

    @pytest.mark.asyncio
    async def test_open_subnet_already_member_raises_409(
        self, service: JoinFlowService, mock_subnet_repo: AsyncMock
    ) -> None:
        # Same edge but on an open subnet — the membership check
        # hoists above the open branch, so even open joins reject
        # duplicates rather than silently no-op.
        subnet = _subnet(join_policy="open", members={"alice", "bob"})
        mock_subnet_repo.find_by_id.return_value = subnet
        with pytest.raises(AlreadyMemberError):
            await service.join_subnet("s-1", "bob")

    @pytest.mark.asyncio
    async def test_pending_join_request_collides_with_self_join(
        self,
        service: JoinFlowService,
        mock_join_repo: AsyncMock,
    ) -> None:
        # ADR §State machine edges "Duplicate join request" — agent
        # has a pending join_request and calls join again. NOT a
        # silent no-op; 409 with the existing request_id.
        existing = _pending_join_request(request_id="rq-OLD")
        mock_join_repo.find_pending_for.return_value = existing

        with pytest.raises(JoinRequestPendingError) as exc_info:
            await service.join_subnet("s-1", "bob")
        assert exc_info.value.existing_request_id == "rq-OLD"


# ============================================================
# Branch-order normativity
# ============================================================


class TestBranchOrderNormativity:
    """ADR §join: "The branch order matters and is normative."
    These tests pin the precedence so a refactor that reorders
    the branches loudly fails."""

    @pytest.mark.asyncio
    async def test_open_beats_owner_check(
        self, service: JoinFlowService, mock_subnet_repo: AsyncMock
    ) -> None:
        # Open + owner — branch 1 (open) wins, NOT branch 2 (owner).
        subnet = _subnet(join_policy="open", owner="alice")
        subnet.member_agent_ids = set()
        mock_subnet_repo.find_by_id.return_value = subnet

        result = await service.join_subnet("s-1", "alice")
        assert isinstance(result, JoinFlowJoinedOpenResult)
        assert not isinstance(result, JoinFlowJoinedAsOwnerResult)

    @pytest.mark.asyncio
    async def test_owner_beats_invitation_check(
        self,
        service: JoinFlowService,
        mock_subnet_repo: AsyncMock,
        mock_join_repo: AsyncMock,
    ) -> None:
        # Approval + owner + (hypothetical) pending invitation —
        # branch 2 (owner) wins, NOT branch 3 (invitation).
        subnet = _subnet(join_policy="approval", owner="alice")
        subnet.member_agent_ids = set()
        mock_subnet_repo.find_by_id.return_value = subnet
        # The owner branch returns before we check find_pending_for,
        # so we don't need to stub it — but stub anyway for safety.
        mock_join_repo.find_pending_for.return_value = _pending_invitation(agent_id="alice")

        result = await service.join_subnet("s-1", "alice")
        assert isinstance(result, JoinFlowJoinedAsOwnerResult)

    @pytest.mark.asyncio
    async def test_invitation_beats_allowlist_branch(
        self,
        service: JoinFlowService,
        mock_join_repo: AsyncMock,
        mock_allowlist_repo: AsyncMock,
    ) -> None:
        # Pending invitation AND on allowlist — branch 4 wins (invite
        # auto-accept), NOT branch 5 (fresh allowlist_auto row).
        invite = _pending_invitation()
        mock_join_repo.find_pending_for.return_value = invite
        mock_join_repo.find_by_id.return_value = invite
        mock_allowlist_repo.is_member.return_value = True

        result = await service.join_subnet("s-1", "bob")
        assert isinstance(result, JoinFlowAutoAcceptedInvitationResult)
        assert result.via == "allowlist"
        assert not isinstance(result, JoinFlowAllowlistAutoApprovedResult)
