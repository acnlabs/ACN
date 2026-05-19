"""SubnetService — join_request lifecycle (ADR-0004 Slice 2.2).

Covers the three methods that drive the ``kind='join_request'``
state machine:

- ``approve_join_request`` — owner says yes; agent joins.
- ``reject_join_request``  — owner says no; agent stays out.
- ``withdraw_join_request`` — applicant pulls their own request.

All three CAS on ``status='pending'`` and raise
``JoinRequestAlreadyDecidedError`` when the row has already
moved off pending. All three fire the matching
``JoinFlowEventType`` via the injected publisher.

The merge path (owner invites an agent who already has a pending
join_request → ``approve_join_request(trigger='auto_on_invite')``
is exercised in ``test_subnet_service_invitations.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.entities import Subnet, SubnetJoinRequest
from acn.core.exceptions import (
    JoinRequestAlreadyDecidedError,
    JoinRequestNotFoundError,
    SubnetNotFoundException,
)
from acn.core.interfaces import (
    IJoinFlowEventPublisher,
    ISubnetJoinRequestRepository,
    ISubnetRepository,
    JoinFlowEventType,
)
from acn.services.subnet_service import SubnetService


def _subnet(
    subnet_id: str = "s-1",
    owner: str = "alice",
    members: set[str] | None = None,
) -> Subnet:
    return Subnet(
        subnet_id=subnet_id,
        name=subnet_id,
        owner=owner,
        created_at=datetime.now(UTC),
        member_agent_ids=members if members is not None else {owner},
    )


def _pending_join_request(
    request_id: str = "rq-1",
    subnet_id: str = "s-1",
    agent_id: str = "bob",
) -> SubnetJoinRequest:
    return SubnetJoinRequest(
        request_id=request_id,
        subnet_id=subnet_id,
        agent_id=agent_id,
        kind="join_request",
        status="pending",
        initiated_by=agent_id,
    )


def _decided_row(
    request_id: str = "rq-1",
    subnet_id: str = "s-1",
    agent_id: str = "bob",
    status: str = "approved",
    decided_by: str = "alice",
) -> SubnetJoinRequest:
    return SubnetJoinRequest(
        request_id=request_id,
        subnet_id=subnet_id,
        agent_id=agent_id,
        kind="join_request",
        status=status,
        initiated_by=agent_id,
        decided_by=decided_by,
        decided_at=datetime.now(UTC),
    )


@pytest.fixture
def mock_subnet_repo() -> AsyncMock:
    repo = AsyncMock(spec=ISubnetRepository)
    repo.find_by_id.return_value = _subnet()
    return repo


@pytest.fixture
def mock_join_repo() -> AsyncMock:
    repo = AsyncMock(spec=ISubnetJoinRequestRepository)
    repo.find_by_id.return_value = _pending_join_request()
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


class TestApproveJoinRequest:
    @pytest.mark.asyncio
    async def test_happy_path_cas_save_and_add_member(
        self,
        service: SubnetService,
        mock_join_repo: AsyncMock,
        mock_subnet_repo: AsyncMock,
        mock_publisher: MagicMock,
    ) -> None:
        result = await service.approve_join_request("s-1", "rq-1", owner_id="alice")

        # CAS: a fresh entity was saved with status=approved.
        assert result.status == "approved"
        assert result.decided_by == "alice"
        assert result.decided_at is not None
        mock_join_repo.save.assert_awaited_once()

        # add_member ran AFTER the CAS — verified by the subnet.save call.
        # (The subnet repo's save is invoked from add_member.)
        mock_subnet_repo.save.assert_awaited()

        # JOIN_APPROVED published with trigger=explicit (default).
        mock_publisher.publish.assert_awaited_once()
        call = mock_publisher.publish.await_args
        assert call.args[0] == JoinFlowEventType.JOIN_APPROVED
        assert call.kwargs["trigger"] == "explicit"
        assert call.kwargs["request"].status == "approved"

    @pytest.mark.asyncio
    async def test_trigger_auto_on_invite_passed_through(
        self, service: SubnetService, mock_publisher: MagicMock
    ) -> None:
        # The merge path (invite collides with pending join_request)
        # routes through approve with trigger=auto_on_invite — the
        # webhook publisher must surface that for ADR §"Merge-path
        # event mapping".
        await service.approve_join_request(
            "s-1", "rq-1", owner_id="alice", trigger="auto_on_invite"
        )
        assert mock_publisher.publish.await_args.kwargs["trigger"] == ("auto_on_invite")

    @pytest.mark.asyncio
    async def test_already_decided_raises_409(
        self,
        service: SubnetService,
        mock_join_repo: AsyncMock,
        mock_publisher: MagicMock,
    ) -> None:
        # ADR §State machine edges "Concurrent decision" — second
        # owner's approve loses the CAS and gets 409.
        mock_join_repo.find_by_id.return_value = _decided_row(status="approved")

        with pytest.raises(JoinRequestAlreadyDecidedError) as exc_info:
            await service.approve_join_request("s-1", "rq-1", owner_id="alice")
        assert exc_info.value.request_id == "rq-1"
        assert exc_info.value.current_status == "approved"

        mock_join_repo.save.assert_not_called()
        mock_publisher.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_wrong_kind_returns_404_in_join_request_namespace(
        self, service: SubnetService, mock_join_repo: AsyncMock
    ) -> None:
        # ADR §"URL alias routing rules": invitation id on
        # /join-requests/ path returns JOIN_REQUEST_NOT_FOUND.
        wrong_kind = SubnetJoinRequest(
            request_id="rq-1",
            subnet_id="s-1",
            agent_id="bob",
            kind="invitation",
            status="pending",
            initiated_by="alice",
        )
        mock_join_repo.find_by_id.return_value = wrong_kind
        with pytest.raises(JoinRequestNotFoundError):
            await service.approve_join_request("s-1", "rq-1", owner_id="alice")

    @pytest.mark.asyncio
    async def test_wrong_subnet_returns_404(
        self, service: SubnetService, mock_join_repo: AsyncMock
    ) -> None:
        # request_id exists but belongs to a different subnet → 404
        # (no existence leak across subnets).
        other_subnet_row = _pending_join_request(subnet_id="s-OTHER")
        mock_join_repo.find_by_id.return_value = other_subnet_row
        with pytest.raises(JoinRequestNotFoundError):
            await service.approve_join_request("s-1", "rq-1", owner_id="alice")

    @pytest.mark.asyncio
    async def test_missing_request_id_returns_404(
        self, service: SubnetService, mock_join_repo: AsyncMock
    ) -> None:
        mock_join_repo.find_by_id.return_value = None
        with pytest.raises(JoinRequestNotFoundError):
            await service.approve_join_request("s-1", "rq-1", owner_id="alice")

    @pytest.mark.asyncio
    async def test_missing_subnet_returns_404(
        self, service: SubnetService, mock_subnet_repo: AsyncMock
    ) -> None:
        mock_subnet_repo.find_by_id.return_value = None
        with pytest.raises(SubnetNotFoundException):
            await service.approve_join_request("missing", "rq-1", owner_id="alice")


class TestRejectJoinRequest:
    @pytest.mark.asyncio
    async def test_happy_path(
        self,
        service: SubnetService,
        mock_join_repo: AsyncMock,
        mock_publisher: MagicMock,
    ) -> None:
        result = await service.reject_join_request(
            "s-1", "rq-1", owner_id="alice", note="not a fit"
        )

        assert result.status == "rejected"
        assert result.decided_by == "alice"
        assert result.note == "not a fit"

        # No add_member on rejection.
        mock_publisher.publish.assert_awaited_once()
        assert mock_publisher.publish.await_args.args[0] == JoinFlowEventType.JOIN_REJECTED

    @pytest.mark.asyncio
    async def test_already_decided_raises_409(
        self, service: SubnetService, mock_join_repo: AsyncMock
    ) -> None:
        mock_join_repo.find_by_id.return_value = _decided_row(status="rejected")
        with pytest.raises(JoinRequestAlreadyDecidedError):
            await service.reject_join_request("s-1", "rq-1", owner_id="alice")

    @pytest.mark.asyncio
    async def test_note_optional(self, service: SubnetService) -> None:
        # Note defaults to None — verifies the kwarg is genuinely
        # optional, not just defensively-tested.
        result = await service.reject_join_request("s-1", "rq-1", owner_id="alice")
        assert result.note is None


class TestWithdrawJoinRequest:
    @pytest.mark.asyncio
    async def test_happy_path(
        self,
        service: SubnetService,
        mock_publisher: MagicMock,
    ) -> None:
        result = await service.withdraw_join_request("s-1", "rq-1", applicant_id="bob")

        assert result.status == "withdrawn"
        assert result.decided_by == "bob"

        # WITHDRAWN event fires.
        mock_publisher.publish.assert_awaited_once()
        assert mock_publisher.publish.await_args.args[0] == JoinFlowEventType.JOIN_WITHDRAWN

    @pytest.mark.asyncio
    async def test_already_decided_raises_409(
        self, service: SubnetService, mock_join_repo: AsyncMock
    ) -> None:
        mock_join_repo.find_by_id.return_value = _decided_row(status="withdrawn", decided_by="bob")
        with pytest.raises(JoinRequestAlreadyDecidedError):
            await service.withdraw_join_request("s-1", "rq-1", applicant_id="bob")


class TestReadPaths:
    @pytest.mark.asyncio
    async def test_list_join_requests_default_kind(
        self, service: SubnetService, mock_join_repo: AsyncMock
    ) -> None:
        mock_join_repo.list_by_subnet.return_value = []
        await service.list_join_requests("s-1")
        mock_join_repo.list_by_subnet.assert_awaited_once_with(
            "s-1", kind="join_request", status=None, limit=100, offset=0
        )

    @pytest.mark.asyncio
    async def test_list_join_requests_allowlist_auto_filter(
        self, service: SubnetService, mock_join_repo: AsyncMock
    ) -> None:
        mock_join_repo.list_by_subnet.return_value = []
        await service.list_join_requests("s-1", kind="allowlist_auto", status="approved")
        mock_join_repo.list_by_subnet.assert_awaited_once_with(
            "s-1",
            kind="allowlist_auto",
            status="approved",
            limit=100,
            offset=0,
        )
