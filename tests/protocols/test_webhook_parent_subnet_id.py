"""Webhook payload ``parent_subnet_id`` field — ADR-0003 Phase 3.

The ADR adds a single new field (``parent_subnet_id``) to the
``data`` block of two existing webhook events —
``AGENT_JOINED_SUBNET`` and ``AGENT_LEFT_SUBNET`` — fired by
``routes/_subnet_membership.py::do_join_subnet`` /
``::do_leave_subnet`` when a subnet has ``harness_url`` registered.

Contract pinned here:

* Top-level subnet (``Subnet.parent_subnet_id is None``) →
  ``payload.data["parent_subnet_id"] is None``.
* Child subnet (``Subnet.parent_subnet_id == "<parent_id>"``) →
  ``payload.data["parent_subnet_id"] == "<parent_id>"``.
* All other ``data`` fields stay byte-identical to the pre-Phase-3
  shape so harnesses that ignore the new field continue to work.
* No new ``WebhookEventType`` values are added — events used are
  still ``AGENT_JOINED_SUBNET`` / ``AGENT_LEFT_SUBNET``.

Placed under ``tests/protocols/`` alongside the other protocol-level
tests; the ADR refers to a hypothetical ``ap2/`` subdirectory but
no such subdirectory exists yet in this repo, and creating one for
a single file would diverge from the established layout convention.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from acn.core.entities import Subnet
from acn.protocols.ap2 import WebhookEventType
from acn.routes._subnet_membership import do_join_subnet, do_leave_subnet
from acn.services._join_flow_result import JoinFlowJoinedOpenResult


def _make_subnet(
    subnet_id: str = "subnet-1",
    *,
    parent_subnet_id: str | None = None,
    owner: str = "alice",
    member_agent_ids: set[str] | None = None,
    harness_url: str = "https://harness.example/webhook",
    harness_secret: str | None = "s3cr3t",
) -> Subnet:
    return Subnet(
        subnet_id=subnet_id,
        name=subnet_id,
        owner=owner,
        parent_subnet_id=parent_subnet_id,
        member_agent_ids=member_agent_ids or {owner},
        harness_url=harness_url,
        harness_secret=harness_secret,
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def webhook_service() -> AsyncMock:
    svc = AsyncMock()
    svc.send_to = AsyncMock(return_value=None)
    return svc


@pytest.fixture
def agent_service() -> AsyncMock:
    svc = AsyncMock()
    svc.join_subnet = AsyncMock(return_value=None)
    svc.leave_subnet = AsyncMock(return_value=None)
    return svc


def _build_join_flow_service(subnet_service: AsyncMock) -> AsyncMock:
    """Stub ``JoinFlowService`` that forwards open-branch to subnet_service.

    ADR-0004 Slice 2.3 — ``do_join_subnet`` now dispatches the
    six-branch decision tree through ``JoinFlowService.join_subnet``
    rather than calling ``subnet_service.add_member`` directly. The
    webhook assertions still want ``add_member`` to fire (it now
    happens inside the service), so the stub forwards to the same
    underlying ``subnet_service`` mock the tests already pin.
    """
    svc = AsyncMock()

    async def _join(subnet_id: str, agent_id: str):
        await subnet_service.add_member(subnet_id, agent_id)
        return JoinFlowJoinedOpenResult(subnet_id=subnet_id, agent_id=agent_id)

    svc.join_subnet = AsyncMock(side_effect=_join)
    return svc


def _build_subnet_service_for_join(
    subnet: Subnet,
    *,
    parent_subnet: Subnet | None = None,
) -> AsyncMock:
    """Build a ``SubnetService`` mock that returns ``subnet`` (and a
    parent subnet when ``parent_subnet_id`` is set).

    For child subnets, ``do_join_subnet`` performs a parent-membership
    pre-check via ``subnet_service.get_subnet(parent_subnet_id)``; we
    need to seed that response too so the join doesn't reject with
    ``NOT_SUBNET_MEMBER`` before reaching the webhook.
    """
    svc = AsyncMock()

    async def _get_subnet(sid: str):
        if sid == subnet.subnet_id:
            return subnet
        if parent_subnet is not None and sid == parent_subnet.subnet_id:
            return parent_subnet
        raise AssertionError(f"unexpected get_subnet({sid!r})")

    svc.get_subnet = AsyncMock(side_effect=_get_subnet)
    svc.add_member = AsyncMock(return_value=subnet)
    svc.remove_member = AsyncMock(return_value=subnet)
    return svc


# ---------------------------------------------------------------------------
# AGENT_JOINED_SUBNET — payload.data.parent_subnet_id
# ---------------------------------------------------------------------------


class TestJoinSubnetWebhookParentField:
    @pytest.mark.asyncio
    async def test_top_level_subnet_emits_null_parent(
        self,
        webhook_service,
        agent_service,
    ):
        subnet = _make_subnet(parent_subnet_id=None)
        subnet_service = _build_subnet_service_for_join(subnet)
        join_flow_service = _build_join_flow_service(subnet_service)

        await do_join_subnet(
            agent_id="alice",
            subnet_id="subnet-1",
            agent_info={"agent_id": "alice"},
            subnet_service=subnet_service,
            agent_service=agent_service,
            webhook_service=webhook_service,
            join_flow_service=join_flow_service,
        )

        webhook_service.send_to.assert_awaited_once()
        call = webhook_service.send_to.await_args
        assert call.kwargs["event"] == WebhookEventType.AGENT_JOINED_SUBNET
        data = call.kwargs["data"]
        assert data == {
            "subnet_id": "subnet-1",
            "agent_id": "alice",
            "parent_subnet_id": None,
        }

    @pytest.mark.asyncio
    async def test_child_subnet_emits_parent_id(
        self,
        webhook_service,
        agent_service,
    ):
        child = _make_subnet(
            subnet_id="squad-1",
            parent_subnet_id="parent-1",
            member_agent_ids={"alice"},
        )
        parent = _make_subnet(
            subnet_id="parent-1",
            parent_subnet_id=None,
            member_agent_ids={"alice"},  # alice is in parent → join allowed
            harness_url=None,  # parent doesn't need a harness for this test
            harness_secret=None,
        )
        subnet_service = _build_subnet_service_for_join(child, parent_subnet=parent)
        join_flow_service = _build_join_flow_service(subnet_service)

        await do_join_subnet(
            agent_id="alice",
            subnet_id="squad-1",
            agent_info={"agent_id": "alice"},
            subnet_service=subnet_service,
            agent_service=agent_service,
            webhook_service=webhook_service,
            join_flow_service=join_flow_service,
        )

        webhook_service.send_to.assert_awaited_once()
        data = webhook_service.send_to.await_args.kwargs["data"]
        assert data == {
            "subnet_id": "squad-1",
            "agent_id": "alice",
            "parent_subnet_id": "parent-1",
        }

    @pytest.mark.asyncio
    async def test_no_harness_no_webhook_delivery(
        self,
        webhook_service,
        agent_service,
    ):
        """Sanity check — subnets without ``harness_url`` do NOT emit
        a webhook regardless of the parent_subnet_id field. Phase 3
        does not change this gate."""
        subnet = _make_subnet(
            parent_subnet_id=None,
            harness_url=None,
            harness_secret=None,
        )
        subnet_service = _build_subnet_service_for_join(subnet)
        join_flow_service = _build_join_flow_service(subnet_service)

        await do_join_subnet(
            agent_id="alice",
            subnet_id="subnet-1",
            agent_info={"agent_id": "alice"},
            subnet_service=subnet_service,
            agent_service=agent_service,
            webhook_service=webhook_service,
            join_flow_service=join_flow_service,
        )

        webhook_service.send_to.assert_not_awaited()


# ---------------------------------------------------------------------------
# AGENT_LEFT_SUBNET — symmetric pin
# ---------------------------------------------------------------------------


class TestLeaveSubnetWebhookParentField:
    @pytest.mark.asyncio
    async def test_top_level_subnet_leave_emits_null_parent(
        self,
        webhook_service,
        agent_service,
    ):
        subnet = _make_subnet(parent_subnet_id=None)
        subnet_service = _build_subnet_service_for_join(subnet)

        await do_leave_subnet(
            agent_id="alice",
            subnet_id="subnet-1",
            agent_info={"agent_id": "alice"},
            subnet_service=subnet_service,
            agent_service=agent_service,
            webhook_service=webhook_service,
        )

        webhook_service.send_to.assert_awaited_once()
        call = webhook_service.send_to.await_args
        assert call.kwargs["event"] == WebhookEventType.AGENT_LEFT_SUBNET
        data = call.kwargs["data"]
        assert data == {
            "subnet_id": "subnet-1",
            "agent_id": "alice",
            "parent_subnet_id": None,
        }

    @pytest.mark.asyncio
    async def test_child_subnet_leave_emits_parent_id(
        self,
        webhook_service,
        agent_service,
    ):
        child = _make_subnet(
            subnet_id="squad-1",
            parent_subnet_id="parent-1",
        )
        # do_leave_subnet does NOT do the parent-membership pre-check
        # (that's join-only), so we don't need to seed a parent
        # response on the subnet_service stub.
        subnet_service = _build_subnet_service_for_join(child)

        await do_leave_subnet(
            agent_id="alice",
            subnet_id="squad-1",
            agent_info={"agent_id": "alice"},
            subnet_service=subnet_service,
            agent_service=agent_service,
            webhook_service=webhook_service,
        )

        webhook_service.send_to.assert_awaited_once()
        data = webhook_service.send_to.await_args.kwargs["data"]
        assert data == {
            "subnet_id": "squad-1",
            "agent_id": "alice",
            "parent_subnet_id": "parent-1",
        }
