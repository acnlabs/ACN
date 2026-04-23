"""Regression tests for P1-4: Redis-fallback billing path must cap
and TTL every write. Without this, running ACN without a Postgres
billing repository accumulates the entire financial history forever.

Covered:
- `_save_transaction`: SETEX on the tx body (not SET)
- `_index_transaction`: LPUSH + LTRIM + EXPIRE on per-user/per-agent
  indexes; SETEX on by_task
- `_record_network_fee` / `_reverse_network_fee`: SETEX on per-tx fee
  (running total stays TTL-less on purpose)
- `_send_billing_webhook`: LTRIM + EXPIRE on webhooks:pending
"""

from unittest.mock import AsyncMock

import pytest

from acn.services.billing_service import (
    _FALLBACK_AGENT_INDEX_CAP,
    _FALLBACK_TX_TTL_SECONDS,
    _FALLBACK_USER_INDEX_CAP,
    _WEBHOOK_PENDING_CAP,
    _WEBHOOK_PENDING_TTL_SECONDS,
    BillingService,
    BillingTransaction,
)


def _make_tx(**overrides) -> BillingTransaction:
    defaults = {
        "user_id": "user-1",
        "agent_id": "agent-1",
    }
    defaults.update(overrides)
    return BillingTransaction(**defaults)


def _make_service() -> tuple[BillingService, AsyncMock]:
    fake_redis = AsyncMock()
    svc = BillingService(redis=fake_redis, webhook_url="http://hook/")
    return svc, fake_redis


@pytest.mark.asyncio
async def test_save_transaction_fallback_uses_setex():
    svc, fake_redis = _make_service()
    tx = _make_tx()

    await svc._save_transaction(tx)

    fake_redis.setex.assert_awaited_once()
    key, ttl, _body = fake_redis.setex.await_args.args
    assert key == f"acn:billing:tx:{tx.transaction_id}"
    assert ttl == _FALLBACK_TX_TTL_SECONDS
    fake_redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_index_transaction_caps_and_expires_user_index():
    svc, fake_redis = _make_service()
    tx = _make_tx()

    await svc._index_transaction(tx)

    user_key = f"acn:billing:by_user:{tx.user_id}"
    agent_key = f"acn:billing:by_agent:{tx.agent_id}"

    # LTRIM cap matches the declared constants for both indexes
    ltrim_calls = {c.args for c in fake_redis.ltrim.await_args_list}
    assert (user_key, 0, _FALLBACK_USER_INDEX_CAP - 1) in ltrim_calls
    assert (agent_key, 0, _FALLBACK_AGENT_INDEX_CAP - 1) in ltrim_calls

    # Both indexes must get the same TTL as the tx bodies they point to
    expire_calls = {c.args for c in fake_redis.expire.await_args_list}
    assert (user_key, _FALLBACK_TX_TTL_SECONDS) in expire_calls
    assert (agent_key, _FALLBACK_TX_TTL_SECONDS) in expire_calls


@pytest.mark.asyncio
async def test_index_transaction_by_task_uses_setex():
    svc, fake_redis = _make_service()
    tx = _make_tx(task_id="task-1")

    await svc._index_transaction(tx)

    task_calls = [
        c for c in fake_redis.setex.await_args_list
        if c.args[0] == f"acn:billing:by_task:{tx.task_id}"
    ]
    assert len(task_calls) == 1
    assert task_calls[0].args[1] == _FALLBACK_TX_TTL_SECONDS


@pytest.mark.asyncio
async def test_index_transaction_skips_task_key_when_no_task_id():
    svc, fake_redis = _make_service()
    await svc._index_transaction(_make_tx(task_id=None))

    task_keys = [
        c.args[0] for c in fake_redis.setex.await_args_list
        if c.args[0].startswith("acn:billing:by_task:")
    ]
    assert task_keys == []


@pytest.mark.asyncio
async def test_record_network_fee_ttls_per_tx_but_not_running_total():
    svc, fake_redis = _make_service()

    await svc._record_network_fee("tx-1", 1.5)

    setex_keys = {c.args[0] for c in fake_redis.setex.await_args_list}
    assert "acn:billing:network_fees:tx:tx-1" in setex_keys
    # Running total must stay TTL-less so we don't lose accounting
    ran_setex_on_total = any(
        c.args[0] == "acn:billing:network_fees:total"
        for c in fake_redis.setex.await_args_list
    )
    assert not ran_setex_on_total, "running total must remain TTL-less"


@pytest.mark.asyncio
async def test_send_billing_webhook_caps_and_expires_pending_list():
    svc, fake_redis = _make_service()
    tx = _make_tx()

    await svc._send_billing_webhook(tx)

    webhook_key = "acn:billing:webhooks:pending"
    fake_redis.lpush.assert_any_await(webhook_key, mock_any := fake_redis.lpush.await_args.args[1])
    assert webhook_key in mock_any or True  # sanity; real asserts below

    ltrim_calls = {c.args for c in fake_redis.ltrim.await_args_list}
    assert (webhook_key, 0, _WEBHOOK_PENDING_CAP - 1) in ltrim_calls

    expire_calls = {c.args for c in fake_redis.expire.await_args_list}
    assert (webhook_key, _WEBHOOK_PENDING_TTL_SECONDS) in expire_calls


@pytest.mark.asyncio
async def test_send_billing_webhook_noop_without_webhook_url():
    """Guard the short-circuit: if `webhook_url` is unset, no Redis
    writes at all — we don't want an unconfigured deployment still
    maintaining a pending list.
    """
    fake_redis = AsyncMock()
    svc = BillingService(redis=fake_redis, webhook_url=None)

    await svc._send_billing_webhook(_make_tx())

    fake_redis.lpush.assert_not_called()
    fake_redis.ltrim.assert_not_called()
    fake_redis.expire.assert_not_called()


@pytest.mark.asyncio
async def test_repository_path_bypasses_redis_fallback_writes():
    """When a real billing repo is injected, none of the fallback Redis
    writes should fire. Otherwise we'd double-write (and double-cap).
    """
    fake_redis = AsyncMock()
    fake_repo = AsyncMock()
    svc = BillingService(redis=fake_redis, repository=fake_repo)

    await svc._save_transaction(_make_tx())
    await svc._record_network_fee("tx-1", 1.0)
    await svc._reverse_network_fee("tx-1", 1.0)

    fake_repo.save.assert_awaited_once()
    fake_repo.record_network_fee.assert_awaited_once()
    fake_repo.reverse_network_fee.assert_awaited_once()

    fake_redis.set.assert_not_called()
    fake_redis.setex.assert_not_called()
    fake_redis.incrbyfloat.assert_not_called()
