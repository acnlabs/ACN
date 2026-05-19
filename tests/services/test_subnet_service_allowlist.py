"""SubnetService — admission-allowlist methods (ADR-0004 Slice 2.2).

Covers the three allowlist methods on ``SubnetService``:
``add_allowlist`` / ``remove_allowlist`` / ``list_allowlist``.
None of them emit webhooks — ADR §"Webhook event catalogue"
explicitly excludes allowlist mutations from the lifecycle
event family.

Authorisation is the route layer's job (Slice 2.3); these tests
verify pure service behaviour — repo wiring, idempotency,
existence checks, and the AllowlistEntryExistsError 409 shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from acn.core.entities import Subnet, SubnetAllowlist
from acn.core.exceptions import (
    AllowlistEntryExistsError,
    SubnetNotFoundException,
)
from acn.core.interfaces import (
    ISubnetAllowlistRepository,
    ISubnetJoinRequestRepository,
    ISubnetRepository,
)
from acn.services._no_op_join_flow_event_publisher import (
    NoOpJoinFlowEventPublisher,
)
from acn.services.subnet_service import SubnetService


def _subnet(subnet_id: str = "s-1", owner: str = "alice") -> Subnet:
    return Subnet(
        subnet_id=subnet_id,
        name=subnet_id,
        owner=owner,
        created_at=datetime.now(UTC),
        member_agent_ids={owner},
    )


@pytest.fixture
def mock_subnet_repo() -> AsyncMock:
    repo = AsyncMock(spec=ISubnetRepository)
    repo.find_by_id.return_value = _subnet()
    return repo


@pytest.fixture
def mock_join_repo() -> AsyncMock:
    return AsyncMock(spec=ISubnetJoinRequestRepository)


@pytest.fixture
def mock_allowlist_repo() -> AsyncMock:
    repo = AsyncMock(spec=ISubnetAllowlistRepository)
    repo.add.return_value = True  # default: row inserted (not duplicate)
    repo.remove.return_value = True
    repo.list_for_subnet.return_value = []
    return repo


@pytest.fixture
def service(
    mock_subnet_repo: AsyncMock,
    mock_join_repo: AsyncMock,
    mock_allowlist_repo: AsyncMock,
) -> SubnetService:
    return SubnetService(
        subnet_repository=mock_subnet_repo,
        subnet_join_request_repository=mock_join_repo,
        subnet_allowlist_repository=mock_allowlist_repo,
    )


class TestAddAllowlist:
    @pytest.mark.asyncio
    async def test_inserts_new_pair_returns_entry(
        self, service: SubnetService, mock_allowlist_repo: AsyncMock
    ) -> None:
        entry = await service.add_allowlist("s-1", "bob", added_by="alice")

        assert entry.subnet_id == "s-1"
        assert entry.agent_id == "bob"
        assert entry.added_by == "alice"
        mock_allowlist_repo.add.assert_awaited_once()
        saved = mock_allowlist_repo.add.await_args.args[0]
        assert isinstance(saved, SubnetAllowlist)
        assert saved.subnet_id == "s-1"
        assert saved.agent_id == "bob"

    @pytest.mark.asyncio
    async def test_duplicate_raises_allowlist_entry_exists_409(
        self, service: SubnetService, mock_allowlist_repo: AsyncMock
    ) -> None:
        # Repo signals "row already existed, no insert performed"
        mock_allowlist_repo.add.return_value = False

        with pytest.raises(AllowlistEntryExistsError) as exc_info:
            await service.add_allowlist("s-1", "bob", added_by="alice")

        assert exc_info.value.subnet_id == "s-1"
        assert exc_info.value.agent_id == "bob"
        assert exc_info.value.reason == "already_on_allowlist"

    @pytest.mark.asyncio
    async def test_missing_subnet_raises_404(
        self, service: SubnetService, mock_subnet_repo: AsyncMock
    ) -> None:
        mock_subnet_repo.find_by_id.return_value = None

        with pytest.raises(SubnetNotFoundException):
            await service.add_allowlist("missing", "bob", added_by="alice")

    @pytest.mark.asyncio
    async def test_raises_runtime_error_when_repo_missing(
        self, mock_subnet_repo: AsyncMock
    ) -> None:
        # Service constructed without the allowlist repo → fail fast
        # on first use rather than silently no-op.
        svc = SubnetService(subnet_repository=mock_subnet_repo)
        with pytest.raises(RuntimeError, match="allowlist_repository is required"):
            await svc.add_allowlist("s-1", "bob", added_by="alice")


class TestRemoveAllowlist:
    @pytest.mark.asyncio
    async def test_existing_pair_returns_true(
        self, service: SubnetService, mock_allowlist_repo: AsyncMock
    ) -> None:
        mock_allowlist_repo.remove.return_value = True
        result = await service.remove_allowlist("s-1", "bob", remover="alice")
        assert result is True
        mock_allowlist_repo.remove.assert_awaited_once_with("s-1", "bob")

    @pytest.mark.asyncio
    async def test_absent_pair_returns_false_idempotent(
        self, service: SubnetService, mock_allowlist_repo: AsyncMock
    ) -> None:
        # ADR §"Allowlist endpoints": DELETE is idempotent —
        # removing an absent pair is not an error.
        mock_allowlist_repo.remove.return_value = False
        result = await service.remove_allowlist("s-1", "bob", remover="alice")
        assert result is False

    @pytest.mark.asyncio
    async def test_missing_subnet_raises_404(
        self, service: SubnetService, mock_subnet_repo: AsyncMock
    ) -> None:
        mock_subnet_repo.find_by_id.return_value = None
        with pytest.raises(SubnetNotFoundException):
            await service.remove_allowlist("missing", "bob", remover="alice")

    @pytest.mark.asyncio
    async def test_does_not_evict_existing_member(
        self,
        service: SubnetService,
        mock_subnet_repo: AsyncMock,
        mock_allowlist_repo: AsyncMock,
    ) -> None:
        # ADR §"State machine edges": "Allowlist removal does not
        # evict members". remove_member MUST NOT be called.
        subnet = _subnet()
        subnet.member_agent_ids = {"alice", "bob"}
        mock_subnet_repo.find_by_id.return_value = subnet

        await service.remove_allowlist("s-1", "bob", remover="alice")

        # The subnet.save() call shouldn't have been invoked — we only
        # touch the allowlist repo. (mock_subnet_repo.save isn't called
        # because the service doesn't mutate subnet on this path.)
        mock_subnet_repo.save.assert_not_called()


class TestListAllowlist:
    @pytest.mark.asyncio
    async def test_returns_entries_from_repo(
        self, service: SubnetService, mock_allowlist_repo: AsyncMock
    ) -> None:
        entries = [
            SubnetAllowlist(
                subnet_id="s-1",
                agent_id="bob",
                added_by="alice",
                added_at=datetime.now(UTC),
            ),
            SubnetAllowlist(
                subnet_id="s-1",
                agent_id="carol",
                added_by="alice",
                added_at=datetime.now(UTC),
            ),
        ]
        mock_allowlist_repo.list_for_subnet.return_value = entries

        result = await service.list_allowlist("s-1")

        assert result == entries
        mock_allowlist_repo.list_for_subnet.assert_awaited_once_with("s-1", limit=100, offset=0)

    @pytest.mark.asyncio
    async def test_pagination_passed_through(
        self, service: SubnetService, mock_allowlist_repo: AsyncMock
    ) -> None:
        await service.list_allowlist("s-1", limit=25, offset=50)
        mock_allowlist_repo.list_for_subnet.assert_awaited_once_with("s-1", limit=25, offset=50)

    @pytest.mark.asyncio
    async def test_missing_subnet_raises_404(
        self, service: SubnetService, mock_subnet_repo: AsyncMock
    ) -> None:
        mock_subnet_repo.find_by_id.return_value = None
        with pytest.raises(SubnetNotFoundException):
            await service.list_allowlist("missing")


class TestPublisherDefaultsToNoOp:
    """If the constructor isn't given an event publisher, the
    service installs the NoOp stub so call sites stay
    publisher-aware without a `if publisher is None` guard."""

    def test_no_publisher_kwarg_installs_noop(self, mock_subnet_repo: AsyncMock) -> None:
        svc = SubnetService(subnet_repository=mock_subnet_repo)
        assert isinstance(svc.event_publisher, NoOpJoinFlowEventPublisher)

    def test_publisher_kwarg_preserved_as_passed(self, mock_subnet_repo: AsyncMock) -> None:
        stub = NoOpJoinFlowEventPublisher()
        svc = SubnetService(
            subnet_repository=mock_subnet_repo,
            join_flow_event_publisher=stub,
        )
        assert svc.event_publisher is stub
