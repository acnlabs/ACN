"""WebhookJoinFlowEventPublisher unit tests (ADR-0004 Slice 2.4 PR C).

Pins the adapter contract documented at the top of
``acn/services/webhook_join_flow_event_publisher.py``:

1. **No-harness gate** — subnets without ``harness_url`` skip the
   webhook entirely (``WebhookService.send_to`` is not called).
2. **1-1 enum mapping** — every ``JoinFlowEventType`` resolves to
   the matching ``WebhookEventType`` member (string value preserved).
3. **Payload shape** — the ``data`` block carries the canonical
   ADR §"Payload shape" fields verbatim, including ``parent_subnet_id``
   from ADR-0003 nesting.
4. **Never raise** — transport exceptions are caught and logged; the
   adapter returns ``None`` so the calling service-layer state
   transition is never rolled back by a delivery failure.
5. **Harness URL / secret pass-through** — the adapter forwards
   ``subnet.harness_url`` / ``subnet.harness_secret`` to ``send_to``
   unchanged (no extra signing / URL massaging).

Style mirrors ``tests/protocols/test_webhook_parent_subnet_id.py`` so
the join-flow webhook surface and the ADR-0003 membership webhook
surface read consistently.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from acn.core.entities import Subnet, SubnetJoinRequest
from acn.core.interfaces.join_flow_event_publisher import JoinFlowEventType
from acn.protocols.ap2.webhook import WebhookEventType
from acn.services.webhook_join_flow_event_publisher import (
    _EVENT_MAP,
    WebhookJoinFlowEventPublisher,
)


def _make_subnet(
    *,
    subnet_id: str = "subnet-1",
    parent_subnet_id: str | None = None,
    harness_url: str | None = "https://harness.example/webhook",
    harness_secret: str | None = "s3cr3t",
) -> Subnet:
    return Subnet(
        subnet_id=subnet_id,
        name=subnet_id,
        owner="alice",
        parent_subnet_id=parent_subnet_id,
        member_agent_ids={"alice"},
        harness_url=harness_url,
        harness_secret=harness_secret,
        created_at=datetime.now(UTC),
    )


def _make_join_request(
    *,
    request_id: str = "req-1",
    subnet_id: str = "subnet-1",
    agent_id: str = "bob",
    kind: str = "join_request",
    status: str = "pending",
    initiated_by: str = "bob",
    decided_by: str | None = None,
) -> SubnetJoinRequest:
    # Entity invariants require both decided_by + decided_at on
    # non-pending rows; mirror that here so the fixture covers
    # approved / rejected rows too.
    decided_at = (
        datetime.now(UTC) if status != "pending" and decided_by is not None else None
    )
    return SubnetJoinRequest(
        request_id=request_id,
        subnet_id=subnet_id,
        agent_id=agent_id,
        kind=kind,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        initiated_by=initiated_by,
        decided_by=decided_by,
        decided_at=decided_at,
    )


@pytest.fixture
def webhook_service() -> AsyncMock:
    svc = AsyncMock()
    svc.send_to = AsyncMock(return_value=True)
    return svc


@pytest.fixture
def publisher(webhook_service: AsyncMock) -> WebhookJoinFlowEventPublisher:
    return WebhookJoinFlowEventPublisher(webhook_service=webhook_service)


# ---------------------------------------------------------------------------
# Gate 1 — no harness URL → publisher returns silently
# ---------------------------------------------------------------------------


class TestNoHarnessGate:
    @pytest.mark.asyncio
    async def test_skips_when_harness_url_is_none(
        self, publisher, webhook_service
    ):
        subnet = _make_subnet(harness_url=None, harness_secret=None)
        request = _make_join_request()

        await publisher.publish(
            JoinFlowEventType.JOIN_REQUESTED, subnet=subnet, request=request
        )

        webhook_service.send_to.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_harness_url_is_empty_string(
        self, publisher, webhook_service
    ):
        subnet = _make_subnet(harness_url="", harness_secret=None)
        request = _make_join_request()

        await publisher.publish(
            JoinFlowEventType.JOIN_APPROVED,
            subnet=subnet,
            request=request,
            trigger="explicit",
        )

        webhook_service.send_to.assert_not_awaited()


# ---------------------------------------------------------------------------
# Gate 2 — 1-1 enum mapping
# ---------------------------------------------------------------------------


class TestEnumMapping:
    """All eight :class:`JoinFlowEventType` members map to a matching
    :class:`WebhookEventType` member of equal string value."""

    @pytest.mark.parametrize(
        "join_flow_event,wire_event",
        [
            (JoinFlowEventType.JOIN_REQUESTED, WebhookEventType.SUBNET_JOIN_REQUESTED),
            (JoinFlowEventType.JOIN_APPROVED, WebhookEventType.SUBNET_JOIN_APPROVED),
            (JoinFlowEventType.JOIN_REJECTED, WebhookEventType.SUBNET_JOIN_REJECTED),
            (JoinFlowEventType.JOIN_WITHDRAWN, WebhookEventType.SUBNET_JOIN_WITHDRAWN),
            (JoinFlowEventType.INVITATION_SENT, WebhookEventType.SUBNET_INVITATION_SENT),
            (
                JoinFlowEventType.INVITATION_ACCEPTED,
                WebhookEventType.SUBNET_INVITATION_ACCEPTED,
            ),
            (
                JoinFlowEventType.INVITATION_REJECTED,
                WebhookEventType.SUBNET_INVITATION_REJECTED,
            ),
            (
                JoinFlowEventType.INVITATION_CANCELED,
                WebhookEventType.SUBNET_INVITATION_CANCELED,
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_each_event_routes_to_its_wire_counterpart(
        self,
        publisher,
        webhook_service,
        join_flow_event: JoinFlowEventType,
        wire_event: WebhookEventType,
    ):
        subnet = _make_subnet()
        request = _make_join_request(
            status="approved" if "APPROVED" in join_flow_event.name else "pending",
            decided_by="alice" if "APPROVED" in join_flow_event.name else None,
        )

        await publisher.publish(join_flow_event, subnet=subnet, request=request)

        webhook_service.send_to.assert_awaited_once()
        assert webhook_service.send_to.await_args.kwargs["event"] is wire_event

    def test_event_map_covers_every_join_flow_event(self):
        """No-drift contract: ``_EVENT_MAP`` is exhaustive over
        :class:`JoinFlowEventType`. A new event added to the service-
        layer enum without a matching adapter entry fails here."""
        assert set(_EVENT_MAP.keys()) == set(JoinFlowEventType)

    def test_event_map_values_match_string_values(self):
        """Both enums share the same canonical ADR string. The map
        must agree on every pair so a JSON-equality lookup (used by
        downstream Harnesses) stays sound."""
        for join_event, wire_event in _EVENT_MAP.items():
            assert join_event.value == wire_event.value


# ---------------------------------------------------------------------------
# Gate 3 — payload shape per ADR §"Payload shape"
# ---------------------------------------------------------------------------


class TestPayloadShape:
    @pytest.mark.asyncio
    async def test_top_level_subnet_sets_parent_to_null(
        self, publisher, webhook_service
    ):
        subnet = _make_subnet(parent_subnet_id=None)
        request = _make_join_request()

        await publisher.publish(
            JoinFlowEventType.JOIN_REQUESTED, subnet=subnet, request=request
        )

        data = webhook_service.send_to.await_args.kwargs["data"]
        assert data["parent_subnet_id"] is None

    @pytest.mark.asyncio
    async def test_child_subnet_propagates_parent_id(
        self, publisher, webhook_service
    ):
        subnet = _make_subnet(subnet_id="squad-1", parent_subnet_id="parent-1")
        request = _make_join_request(subnet_id="squad-1")

        await publisher.publish(
            JoinFlowEventType.JOIN_REQUESTED, subnet=subnet, request=request
        )

        data = webhook_service.send_to.await_args.kwargs["data"]
        assert data["parent_subnet_id"] == "parent-1"

    @pytest.mark.asyncio
    async def test_data_block_carries_all_adr_fields(
        self, publisher, webhook_service
    ):
        subnet = _make_subnet(subnet_id="subnet-X", parent_subnet_id="parent-X")
        request = _make_join_request(
            request_id="req-X",
            subnet_id="subnet-X",
            agent_id="bob",
            kind="invitation",
            status="approved",
            initiated_by="alice",
            decided_by="bob",
        )

        await publisher.publish(
            JoinFlowEventType.INVITATION_ACCEPTED,
            subnet=subnet,
            request=request,
            trigger="auto_on_join",
            via="self_join",
        )

        data = webhook_service.send_to.await_args.kwargs["data"]
        assert data == {
            "subnet_id": "subnet-X",
            "agent_id": "bob",
            "request_id": "req-X",
            "parent_subnet_id": "parent-X",
            "kind": "invitation",
            "initiated_by": "alice",
            "decided_by": "bob",
            "trigger": "auto_on_join",
            "via": "self_join",
        }

    @pytest.mark.asyncio
    async def test_explicit_trigger_via_defaults_to_none(
        self, publisher, webhook_service
    ):
        subnet = _make_subnet()
        request = _make_join_request()

        await publisher.publish(
            JoinFlowEventType.JOIN_REQUESTED, subnet=subnet, request=request
        )

        data = webhook_service.send_to.await_args.kwargs["data"]
        assert data["trigger"] == "explicit"
        assert data["via"] is None

    @pytest.mark.asyncio
    async def test_decided_by_null_on_pending_rows(
        self, publisher, webhook_service
    ):
        subnet = _make_subnet()
        request = _make_join_request(status="pending", decided_by=None)

        await publisher.publish(
            JoinFlowEventType.JOIN_REQUESTED, subnet=subnet, request=request
        )

        data = webhook_service.send_to.await_args.kwargs["data"]
        assert data["decided_by"] is None

    @pytest.mark.asyncio
    async def test_task_id_carries_subnet_id_for_harness_routing(
        self, publisher, webhook_service
    ):
        """ADR-0003 convention: ``WebhookPayload.task_id`` is set to
        the subnet_id for non-payment events so Harnesses keying on
        the wrapper field still route correctly."""
        subnet = _make_subnet(subnet_id="subnet-route-1")
        request = _make_join_request(subnet_id="subnet-route-1")

        await publisher.publish(
            JoinFlowEventType.JOIN_REQUESTED, subnet=subnet, request=request
        )

        assert (
            webhook_service.send_to.await_args.kwargs["task_id"]
            == "subnet-route-1"
        )


# ---------------------------------------------------------------------------
# Gate 4 — never raise on transport failure
# ---------------------------------------------------------------------------


class TestNeverRaise:
    @pytest.mark.asyncio
    async def test_swallow_runtime_error_from_send_to(self, webhook_service):
        webhook_service.send_to.side_effect = RuntimeError("transport down")
        publisher = WebhookJoinFlowEventPublisher(webhook_service=webhook_service)
        subnet = _make_subnet()
        request = _make_join_request()

        # Must NOT raise — the calling service is mid-transaction and
        # the row has already been committed; raising here would either
        # corrupt the transaction or escape as a 500 after success.
        result = await publisher.publish(
            JoinFlowEventType.JOIN_REQUESTED, subnet=subnet, request=request
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_swallow_timeout_from_send_to(self, webhook_service):
        webhook_service.send_to.side_effect = TimeoutError("upstream slow")
        publisher = WebhookJoinFlowEventPublisher(webhook_service=webhook_service)
        subnet = _make_subnet()

        await publisher.publish(
            JoinFlowEventType.JOIN_APPROVED,
            subnet=subnet,
            request=_make_join_request(
                status="approved", decided_by="alice"
            ),
            trigger="explicit",
        )

        # If we reach here the exception was swallowed.

    @pytest.mark.asyncio
    async def test_swallow_when_send_to_returns_false(
        self, publisher, webhook_service
    ):
        """``send_to`` returns ``False`` on exhausted retries; the
        adapter treats that as a delivery failure but still doesn't
        propagate it to the caller."""
        webhook_service.send_to.return_value = False
        subnet = _make_subnet()
        request = _make_join_request()

        result = await publisher.publish(
            JoinFlowEventType.JOIN_REQUESTED, subnet=subnet, request=request
        )

        assert result is None
        webhook_service.send_to.assert_awaited_once()


# ---------------------------------------------------------------------------
# Gate 5 — harness URL / secret pass-through
# ---------------------------------------------------------------------------


class TestUrlSecretPassThrough:
    @pytest.mark.asyncio
    async def test_passes_harness_url_and_secret_verbatim(
        self, publisher, webhook_service
    ):
        subnet = _make_subnet(
            harness_url="https://special.example/hook",
            harness_secret="my-secret",
        )
        request = _make_join_request()

        await publisher.publish(
            JoinFlowEventType.JOIN_REQUESTED, subnet=subnet, request=request
        )

        kwargs = webhook_service.send_to.await_args.kwargs
        assert kwargs["url"] == "https://special.example/hook"
        assert kwargs["secret"] == "my-secret"

    @pytest.mark.asyncio
    async def test_none_secret_passes_through_as_none(
        self, publisher, webhook_service
    ):
        """Subnets that opt in to webhooks without a secret get an
        unsigned delivery — the adapter must not invent a default."""
        subnet = _make_subnet(
            harness_url="https://insecure.example/hook",
            harness_secret=None,
        )
        request = _make_join_request()

        await publisher.publish(
            JoinFlowEventType.JOIN_REQUESTED, subnet=subnet, request=request
        )

        kwargs = webhook_service.send_to.await_args.kwargs
        assert kwargs["secret"] is None
