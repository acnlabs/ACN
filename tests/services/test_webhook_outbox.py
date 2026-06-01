"""Tests for ACN #162: durable webhook outbox (ADR-0009 C7).

After in-process retries are exhausted, ``WebhookService`` parks the delivery in
Redis and a background worker re-drives it until delivered or aged out
(at-least-once). These tests drive ``_drain_outbox_once`` directly (instead of
the background loop) for determinism, and assert:

- enqueue parks the exact body + secret and schedules it on the due ZSET;
- a successful sweep delivers, removes the item, marks history ``delivered``;
- a failed sweep reschedules with a later score and bumps attempts;
- past ``max_age`` the item is dead-lettered (history ``dead``), not retried;
- the ZSET ``ZREM`` claim is atomic (only one owner per due item);
- the secret lives in the outbox item but never in the history record;
- the re-driven body is signed with the original secret so HMAC still matches.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeredis_async

from acn.protocols.ap2.webhook import (
    _OUTBOX_DUE_ZSET,
    _OUTBOX_ITEM_PREFIX,
    _OUTBOX_METRIC_PREFIX,
    OutboxItem,
    WebhookConfig,
    WebhookDelivery,
    WebhookEventType,
    WebhookPayload,
    WebhookService,
)

URL = "https://seller.example/acn/webhooks"
SECRET = "seller-shared-secret"
EVENT = WebhookEventType.PAYMENT_CONFIRMED


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[fakeredis_async.FakeRedis, None]:
    client = fakeredis_async.FakeRedis(decode_responses=False)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


def _make_service(redis_client, **kw) -> WebhookService:
    # Worker is NOT started; tests call _drain_outbox_once() directly.
    return WebhookService(
        redis_client,
        WebhookConfig(url="https://platform.example/wh", secret="platform"),
        outbox_enabled=True,
        outbox_poll_interval=1,
        outbox_max_age_seconds=kw.get("max_age", 86400),
        outbox_max_backoff=kw.get("max_backoff", 600),
    )


def _payload(delivery_id="wh_task1_pc") -> tuple[WebhookDelivery, str]:
    payload = WebhookPayload(
        event=EVENT,
        task_id="task1",
        data={"task_metadata": {"order_id": "ord-1"}},
        seller_agent="seller-agent",
    )
    delivery = WebhookDelivery(id=delivery_id, payload=payload, url=URL, attempts=3)
    return delivery, payload.model_dump_json()


def _fake_http(*, ok: bool):
    """A stand-in httpx.AsyncClient whose post() returns a 2xx/5xx response."""
    client = MagicMock()
    resp = MagicMock()
    resp.is_success = ok
    resp.status_code = 200 if ok else 503
    resp.text = "ok" if ok else "nope"

    async def _post(*a, **k):
        _post.calls.append(k)
        return resp

    _post.calls = []
    client.post = _post
    return client


async def _park_now(svc: WebhookService, item: OutboxItem) -> None:
    """Persist an outbox item and mark it due now (score in the past)."""
    await svc._save_outbox_item(item)
    await svc.redis.zadd(_OUTBOX_DUE_ZSET, {item.delivery_id: time.time() - 1})


def _item(delivery_id="wh_task1_pc", payload_json=None, **kw) -> OutboxItem:
    now = time.time()
    return OutboxItem(
        delivery_id=delivery_id,
        url=URL,
        secret=SECRET,
        payload=payload_json or _payload(delivery_id)[1],
        event=EVENT.value,
        timestamp="2026-06-01T00:00:00+00:00",
        task_id="task1",
        attempts=kw.get("attempts", 3),
        first_enqueued_at=kw.get("first_enqueued_at", now),
        next_attempt_at=now,
    )


# --------------------------------------------------------------------------- #
# enqueue
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_enqueue_parks_body_secret_and_schedules(redis_client):
    svc = _make_service(redis_client)
    delivery, payload_json = _payload()
    cfg = WebhookConfig(url=URL, secret=SECRET, retry_delay=5)

    await svc._enqueue_outbox(delivery, cfg, payload_json)

    stored = await redis_client.get(f"{_OUTBOX_ITEM_PREFIX}{delivery.id}")
    assert stored is not None
    parsed = OutboxItem.model_validate_json(stored)
    assert parsed.url == URL
    assert parsed.secret == SECRET  # outbox keeps the secret to re-sign
    assert parsed.payload == payload_json  # exact body preserved for HMAC + dedupe
    # scheduled on the due ZSET
    assert await redis_client.zscore(_OUTBOX_DUE_ZSET, delivery.id) is not None


# --------------------------------------------------------------------------- #
# successful drain
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_drain_delivers_removes_and_marks_history(redis_client):
    svc = _make_service(redis_client)
    svc._http_client = _fake_http(ok=True)
    delivery, payload_json = _payload()
    await svc._save_delivery(delivery)  # history record exists (status updated in place)
    await _park_now(svc, _item())

    processed = await svc._drain_outbox_once()

    assert processed == 1
    assert await redis_client.zscore(_OUTBOX_DUE_ZSET, delivery.id) is None
    assert await redis_client.get(f"{_OUTBOX_ITEM_PREFIX}{delivery.id}") is None
    hist = WebhookDelivery.model_validate_json(
        await redis_client.get(f"acn:webhooks:deliveries:{delivery.id}")
    )
    assert hist.status == "delivered"
    assert hist.delivered_at is not None
    assert await redis_client.get(f"{_OUTBOX_METRIC_PREFIX}delivered:{EVENT.value}") == b"1"


@pytest.mark.asyncio
async def test_redriven_body_is_signed_with_original_secret(redis_client):
    svc = _make_service(redis_client)
    http = _fake_http(ok=True)
    svc._http_client = http
    _, payload_json = _payload()
    await _park_now(svc, _item(payload_json=payload_json))

    await svc._drain_outbox_once()

    sent = http.post.calls[-1]
    expected = "sha256=" + hmac.new(
        SECRET.encode(), payload_json.encode(), hashlib.sha256
    ).hexdigest()
    assert sent["headers"]["X-ACN-Signature"] == expected
    assert sent["headers"]["X-ACN-Event"] == EVENT.value
    assert sent["content"] == payload_json


# --------------------------------------------------------------------------- #
# failure -> reschedule
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_drain_failure_reschedules_with_later_score(redis_client):
    svc = _make_service(redis_client)
    svc._http_client = _fake_http(ok=False)
    delivery, _ = _payload()
    delivery.status = "queued"  # mirrors _deliver_webhook before enqueue
    await svc._save_delivery(delivery)
    await _park_now(svc, _item(attempts=3))

    before = time.time()
    await svc._drain_outbox_once()

    score = await redis_client.zscore(_OUTBOX_DUE_ZSET, delivery.id)
    assert score is not None and score > before  # re-queued for the future
    item = OutboxItem.model_validate_json(
        await redis_client.get(f"{_OUTBOX_ITEM_PREFIX}{delivery.id}")
    )
    assert item.attempts == 4  # bumped
    assert item.last_error
    assert await redis_client.get(f"{_OUTBOX_METRIC_PREFIX}failed:{EVENT.value}") == b"1"
    # not yet terminal
    hist = WebhookDelivery.model_validate_json(
        await redis_client.get(f"acn:webhooks:deliveries:{delivery.id}")
    )
    assert hist.status == "queued"


# --------------------------------------------------------------------------- #
# dead-letter past max_age
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dead_letter_after_max_age(redis_client):
    svc = _make_service(redis_client, max_age=0)  # any failed attempt ages out
    svc._http_client = _fake_http(ok=False)
    delivery, _ = _payload()
    await svc._save_delivery(delivery)
    await _park_now(svc, _item(first_enqueued_at=time.time() - 10))

    await svc._drain_outbox_once()

    assert await redis_client.zscore(_OUTBOX_DUE_ZSET, delivery.id) is None
    assert await redis_client.get(f"{_OUTBOX_ITEM_PREFIX}{delivery.id}") is None
    hist = WebhookDelivery.model_validate_json(
        await redis_client.get(f"acn:webhooks:deliveries:{delivery.id}")
    )
    assert hist.status == "dead"
    assert await redis_client.get(f"{_OUTBOX_METRIC_PREFIX}dead:{EVENT.value}") == b"1"


# --------------------------------------------------------------------------- #
# atomic claim + secret hygiene
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_zrem_claim_is_atomic(redis_client):
    svc = _make_service(redis_client)
    await redis_client.zadd(_OUTBOX_DUE_ZSET, {"wh_x": time.time() - 1})
    # First claim wins, second sees nothing.
    assert await redis_client.zrem(_OUTBOX_DUE_ZSET, "wh_x") == 1
    assert await redis_client.zrem(_OUTBOX_DUE_ZSET, "wh_x") == 0
    # An empty due set processes nothing.
    assert await svc._drain_outbox_once() == 0


@pytest.mark.asyncio
async def test_secret_never_written_to_history_record(redis_client):
    svc = _make_service(redis_client)
    delivery, payload_json = _payload()
    cfg = WebhookConfig(url=URL, secret=SECRET, retry_delay=5)

    delivery.status = "queued"
    await svc._save_delivery(delivery)
    await svc._enqueue_outbox(delivery, cfg, payload_json)

    raw_hist = await redis_client.get(f"acn:webhooks:deliveries:{delivery.id}")
    assert SECRET.encode() not in raw_hist  # history is secret-free
    # ...but the outbox item retains it (internal Redis only).
    raw_item = await redis_client.get(f"{_OUTBOX_ITEM_PREFIX}{delivery.id}")
    assert SECRET.encode() in raw_item
    assert json.loads(raw_hist)["status"] == "queued"
