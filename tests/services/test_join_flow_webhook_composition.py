"""End-to-end composition tests for the join-flow webhook chain.

ADR-0004 Slice 2.4 PR C wires :class:`WebhookJoinFlowEventPublisher`
in front of the existing ``SubnetService`` / ``JoinFlowService`` emit
sites. Slice 2.2 tests already pin that the service layer calls
``publisher.publish(...)`` with the right arguments; the adapter unit
tests pin that ``publisher.publish`` calls
``WebhookService.send_to(...)`` with the right shape. This file pins
the **composition** — stitching the two together end-to-end so a
future change that breaks the contract surfaces in CI rather than at
runtime when a real Harness suddenly stops receiving traffic.

Two contracts pinned here:

1. **Wire-through happy path** — `approve_join_request` end-to-end
   with the real adapter fires exactly one ``send_to`` call carrying
   the correct ``WebhookEventType`` and ``data`` payload.
2. **Webhook failure does NOT roll back the lifecycle** — when
   ``send_to`` raises, the service call still returns the approved
   row and the persisted state is unchanged. This is the
   ADR §"Cross-slice acceptance" guarantee.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from acn.core.entities import Subnet, SubnetJoinRequest
from acn.core.interfaces import (
    ISubnetJoinRequestRepository,
    ISubnetRepository,
)
from acn.protocols.ap2.webhook import WebhookEventType
from acn.services.subnet_service import SubnetService
from acn.services.webhook_join_flow_event_publisher import (
    WebhookJoinFlowEventPublisher,
)


def _subnet() -> Subnet:
    return Subnet(
        subnet_id="s-1",
        name="s-1",
        owner="alice",
        member_agent_ids={"alice"},
        harness_url="https://harness.example/webhook",
        harness_secret="secret",
        created_at=datetime.now(UTC),
    )


def _pending_join_request() -> SubnetJoinRequest:
    return SubnetJoinRequest(
        request_id="rq-1",
        subnet_id="s-1",
        agent_id="bob",
        kind="join_request",
        status="pending",
        initiated_by="bob",
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
def mock_webhook_service() -> AsyncMock:
    svc = AsyncMock()
    svc.send_to = AsyncMock(return_value=True)
    return svc


@pytest.fixture
def service(
    mock_subnet_repo: AsyncMock,
    mock_join_repo: AsyncMock,
    mock_webhook_service: AsyncMock,
) -> SubnetService:
    """SubnetService composed with the REAL ``WebhookJoinFlowEventPublisher``.

    Unlike the Slice 2.2 fixtures (which inject a ``MagicMock`` for the
    publisher), this composition wires the actual adapter so any drift
    between service-layer expectations and adapter behaviour surfaces
    end-to-end.
    """
    publisher = WebhookJoinFlowEventPublisher(
        webhook_service=mock_webhook_service,
    )
    return SubnetService(
        subnet_repository=mock_subnet_repo,
        subnet_join_request_repository=mock_join_repo,
        join_flow_event_publisher=publisher,
    )


class TestWireThroughHappyPath:
    @pytest.mark.asyncio
    async def test_approve_fires_webhook_with_correct_event_and_data(
        self,
        service: SubnetService,
        mock_webhook_service: AsyncMock,
    ) -> None:
        result = await service.approve_join_request("s-1", "rq-1", owner_id="alice")

        # Service-layer state transition still works.
        assert result.status == "approved"
        assert result.decided_by == "alice"

        # Adapter chained through to the wire-level webhook with the
        # ADR-pinned event + payload.
        mock_webhook_service.send_to.assert_awaited_once()
        kwargs = mock_webhook_service.send_to.await_args.kwargs
        assert kwargs["event"] is WebhookEventType.SUBNET_JOIN_APPROVED
        assert kwargs["url"] == "https://harness.example/webhook"
        assert kwargs["secret"] == "secret"
        assert kwargs["task_id"] == "s-1"
        assert kwargs["data"]["subnet_id"] == "s-1"
        assert kwargs["data"]["agent_id"] == "bob"
        assert kwargs["data"]["request_id"] == "rq-1"
        assert kwargs["data"]["kind"] == "join_request"
        assert kwargs["data"]["initiated_by"] == "bob"
        assert kwargs["data"]["decided_by"] == "alice"
        assert kwargs["data"]["trigger"] == "explicit"
        assert kwargs["data"]["via"] is None
        # ADR-0003 nesting carry-through: top-level subnet has no parent.
        assert kwargs["data"]["parent_subnet_id"] is None

    @pytest.mark.asyncio
    async def test_subnet_without_harness_url_skips_send_to(
        self,
        mock_subnet_repo: AsyncMock,
        mock_join_repo: AsyncMock,
        mock_webhook_service: AsyncMock,
    ) -> None:
        # Re-pin the subnet repo to return a harnessless subnet —
        # the adapter must short-circuit so no transport call fires.
        mock_subnet_repo.find_by_id.return_value = Subnet(
            subnet_id="s-1",
            name="s-1",
            owner="alice",
            member_agent_ids={"alice"},
            harness_url=None,
            harness_secret=None,
            created_at=datetime.now(UTC),
        )
        publisher = WebhookJoinFlowEventPublisher(
            webhook_service=mock_webhook_service
        )
        service = SubnetService(
            subnet_repository=mock_subnet_repo,
            subnet_join_request_repository=mock_join_repo,
            join_flow_event_publisher=publisher,
        )

        result = await service.approve_join_request("s-1", "rq-1", owner_id="alice")

        # Service still completes the state transition…
        assert result.status == "approved"
        # …but the wire-level webhook never fires.
        mock_webhook_service.send_to.assert_not_awaited()


class TestWebhookFailureDoesNotRollbackLifecycle:
    """ADR §"Cross-slice acceptance" — "Webhook delivery failures
    **do not** roll back the underlying DB transaction." Pinned twice
    (transport raise + retry exhaustion) to make sure any future
    refactor of the adapter's swallow gate keeps the invariant."""

    @pytest.mark.asyncio
    async def test_send_to_runtime_error_does_not_break_approve(
        self,
        service: SubnetService,
        mock_webhook_service: AsyncMock,
        mock_join_repo: AsyncMock,
    ) -> None:
        mock_webhook_service.send_to.side_effect = RuntimeError("transport boom")

        # The state transition must still complete cleanly.
        result = await service.approve_join_request("s-1", "rq-1", owner_id="alice")

        assert result.status == "approved"
        # Row persisted exactly once — the failed webhook did NOT
        # cause an extra repo write (or a delete) trying to undo the
        # commit.
        assert mock_join_repo.save.await_count == 1

    @pytest.mark.asyncio
    async def test_send_to_returning_false_does_not_break_approve(
        self,
        service: SubnetService,
        mock_webhook_service: AsyncMock,
        mock_join_repo: AsyncMock,
    ) -> None:
        """``send_to`` returns ``False`` when every retry attempt
        failed without raising. The adapter logs and proceeds; the
        service contract is unaffected."""
        mock_webhook_service.send_to.return_value = False

        result = await service.approve_join_request("s-1", "rq-1", owner_id="alice")

        assert result.status == "approved"
        assert mock_join_repo.save.await_count == 1
