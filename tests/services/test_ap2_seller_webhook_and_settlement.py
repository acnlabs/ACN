"""Tests for ADR-0009 P1-A: seller webhook delivery + store settlement mirror.

Covers the three behaviors added for ACN #161:

1. ``PaymentCapability`` now persists ``webhook_url`` / ``webhook_secret``
   round-trip through the Redis-backed discovery index (previously
   ``webhook_url`` was accepted by the API but silently dropped).
2. Payment-task status transitions deliver to the *seller's* registered
   webhook (signed with the seller's per-agent secret) in addition to the
   platform default backend.
3. The store-settlement bridge mirrors a settled store order as a confirmed
   ``platform_credits`` task, idempotent on ``order_id``, and can advance the
   mirror to ``task_completed`` on fulfill (C8).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeredis_async

from acn.protocols.ap2.core import (
    PaymentCapability,
    PaymentDiscoveryService,
    PaymentTaskManager,
    PaymentTaskStatus,
    SupportedPaymentMethod,
)
from acn.protocols.ap2.webhook import WebhookEventType

SELLER = "agentmother"
BUYER = "system:human-buyer"
WEBHOOK_URL = "https://agentmother.acnlabs.org/acn/webhooks"
WEBHOOK_SECRET = "seller-shared-secret"


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[fakeredis_async.FakeRedis, None]:
    client = fakeredis_async.FakeRedis(decode_responses=False)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def discovery(redis_client) -> PaymentDiscoveryService:
    return PaymentDiscoveryService(redis_client)


@pytest_asyncio.fixture
async def seller_capability(discovery) -> PaymentCapability:
    cap = PaymentCapability(
        accepts_payment=True,
        payment_methods=[SupportedPaymentMethod.PLATFORM_CREDITS],
        webhook_url=WEBHOOK_URL,
        webhook_secret=WEBHOOK_SECRET,
    )
    await discovery.index_payment_capability(SELLER, cap)
    return cap


def _make_manager(redis_client, discovery) -> tuple[PaymentTaskManager, AsyncMock]:
    webhook = AsyncMock()
    webhook.send_event = AsyncMock(return_value=True)
    webhook.send_to = AsyncMock(return_value=True)
    mgr = PaymentTaskManager(
        redis=redis_client,
        discovery=discovery,
        webhook_service=webhook,
    )
    return mgr, webhook


# --------------------------------------------------------------------------- #
# 1. webhook_url / webhook_secret persistence
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_webhook_fields_round_trip_through_discovery(discovery, seller_capability):
    """webhook_url + webhook_secret survive the Redis index round-trip."""
    loaded = await discovery.get_agent_payment_capability(SELLER)
    assert loaded is not None
    assert loaded.webhook_url == WEBHOOK_URL
    assert loaded.webhook_secret == WEBHOOK_SECRET


def test_webhook_secret_excluded_from_read_dump():
    """The model_dump(exclude=...) used by the GET route hides the secret
    but keeps webhook_url and the computed supported_methods alias."""
    cap = PaymentCapability(
        accepts_payment=True,
        payment_methods=[SupportedPaymentMethod.PLATFORM_CREDITS],
        webhook_url=WEBHOOK_URL,
        webhook_secret=WEBHOOK_SECRET,
    )
    dumped = cap.model_dump(exclude={"webhook_secret"})
    assert "webhook_secret" not in dumped
    assert dumped["webhook_url"] == WEBHOOK_URL
    assert dumped["supported_methods"] == [SupportedPaymentMethod.PLATFORM_CREDITS]


# --------------------------------------------------------------------------- #
# 2. Per-seller webhook delivery
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_status_change_delivers_to_seller_webhook(
    redis_client, discovery, seller_capability
):
    mgr, webhook = _make_manager(redis_client, discovery)

    task = await mgr.create_payment_task(
        buyer_agent=BUYER,
        seller_agent=SELLER,
        task_description="order",
        amount="700",
        currency="credits",
        payment_method=SupportedPaymentMethod.PLATFORM_CREDITS,
    )
    await mgr.update_task_status(task.task_id, PaymentTaskStatus.PAYMENT_CONFIRMED)

    # Seller endpoint received deliveries signed with the seller's secret.
    assert webhook.send_to.await_count >= 1
    for call in webhook.send_to.await_args_list:
        assert call.kwargs["url"] == WEBHOOK_URL
        assert call.kwargs["secret"] == WEBHOOK_SECRET
    delivered_events = {call.kwargs["event"] for call in webhook.send_to.await_args_list}
    assert WebhookEventType.PAYMENT_CONFIRMED in delivered_events
    # Platform default backend still notified too.
    assert webhook.send_event.await_count >= 1


@pytest.mark.asyncio
async def test_no_seller_webhook_when_url_unset(redis_client, discovery):
    cap = PaymentCapability(
        accepts_payment=True,
        payment_methods=[SupportedPaymentMethod.PLATFORM_CREDITS],
    )
    await discovery.index_payment_capability(SELLER, cap)
    mgr, webhook = _make_manager(redis_client, discovery)

    task = await mgr.create_payment_task(
        buyer_agent=BUYER,
        seller_agent=SELLER,
        task_description="order",
        amount="700",
        currency="credits",
        payment_method=SupportedPaymentMethod.PLATFORM_CREDITS,
    )
    await mgr.update_task_status(task.task_id, PaymentTaskStatus.PAYMENT_CONFIRMED)

    webhook.send_to.assert_not_awaited()
    assert webhook.send_event.await_count >= 1


@pytest.mark.asyncio
async def test_seller_delivery_failure_does_not_break_status_change(
    redis_client, discovery, seller_capability
):
    mgr, webhook = _make_manager(redis_client, discovery)
    webhook.send_to = AsyncMock(side_effect=RuntimeError("seller down"))

    task = await mgr.create_payment_task(
        buyer_agent=BUYER,
        seller_agent=SELLER,
        task_description="order",
        amount="700",
        currency="credits",
        payment_method=SupportedPaymentMethod.PLATFORM_CREDITS,
    )
    # Must not raise despite the seller webhook blowing up.
    updated = await mgr.update_task_status(task.task_id, PaymentTaskStatus.PAYMENT_CONFIRMED)
    assert updated.status == PaymentTaskStatus.PAYMENT_CONFIRMED


# --------------------------------------------------------------------------- #
# 3. Store settlement bridge
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_record_store_settlement_creates_confirmed_task(
    redis_client, discovery, seller_capability
):
    mgr, webhook = _make_manager(redis_client, discovery)

    task = await mgr.record_store_settlement(
        order_id="APORDER-1",
        seller_agent=SELLER,
        buyer_agent=BUYER,
        amount_credits=700,
    )

    assert task.status == PaymentTaskStatus.PAYMENT_CONFIRMED
    assert task.payment_method == SupportedPaymentMethod.PLATFORM_CREDITS
    assert task.currency == "credits"
    assert task.task_metadata["order_id"] == "APORDER-1"
    assert task.task_metadata["settlement_authority"] == "backend"
    assert task.tx_hash == "APORDER-1"


@pytest.mark.asyncio
async def test_record_store_settlement_is_idempotent(
    redis_client, discovery, seller_capability
):
    mgr, _ = _make_manager(redis_client, discovery)

    first = await mgr.record_store_settlement(
        order_id="APORDER-2", seller_agent=SELLER, buyer_agent=BUYER, amount_credits=500
    )
    second = await mgr.record_store_settlement(
        order_id="APORDER-2", seller_agent=SELLER, buyer_agent=BUYER, amount_credits=500
    )
    assert first.task_id == second.task_id


@pytest.mark.asyncio
async def test_complete_store_settlement_advances_to_completed(
    redis_client, discovery, seller_capability
):
    mgr, _ = _make_manager(redis_client, discovery)

    recorded = await mgr.record_store_settlement(
        order_id="APORDER-3", seller_agent=SELLER, buyer_agent=BUYER, amount_credits=900
    )
    completed = await mgr.complete_store_settlement("APORDER-3")

    assert completed is not None
    assert completed.task_id == recorded.task_id
    assert completed.status == PaymentTaskStatus.TASK_COMPLETED

    # Idempotent: completing again keeps it completed.
    again = await mgr.complete_store_settlement("APORDER-3")
    assert again is not None
    assert again.status == PaymentTaskStatus.TASK_COMPLETED


@pytest.mark.asyncio
async def test_complete_store_settlement_unknown_order_returns_none(
    redis_client, discovery
):
    mgr, _ = _make_manager(redis_client, discovery)
    assert await mgr.complete_store_settlement("does-not-exist") is None


@pytest.mark.asyncio
async def test_record_store_settlement_rejects_unregistered_seller(redis_client, discovery):
    """A seller with no platform_credits capability raises ValueError, which the
    route maps to a non-fatal 409 so the backend falls back to reconciliation."""
    mgr, _ = _make_manager(redis_client, discovery)
    with pytest.raises(ValueError, match="does not accept"):
        await mgr.record_store_settlement(
            order_id="APORDER-4",
            seller_agent="unknown-seller",
            buyer_agent=BUYER,
            amount_credits=100,
        )
