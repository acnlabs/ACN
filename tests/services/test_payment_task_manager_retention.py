"""Regression tests for P1-5: PaymentTaskManager must bound every
piece of Redis state it writes.

Before: `_save_task` was SET (no TTL), `_index_task` was SADD (no TTL
on the set), `_audit_log` was LPUSH with nothing else. At scale that
means "every payment task ever processed, plus every status change,
forever" — trivially GB-to-TB steady state.

After:
- terminal-status saves go through SETEX on a long TTL
- in-flight saves keep SET so a stuck pending task can't disappear
- index sets get a sliding TTL
- audit lists are capped and TTL'd
"""

from unittest.mock import AsyncMock

import pytest

from acn.protocols.ap2.core import (
    _PAYMENT_AUDIT_CAP,
    _PAYMENT_INDEX_TTL_SECONDS,
    _PAYMENT_TASK_TERMINAL_TTL_SECONDS,
    _PAYMENT_TERMINAL_STATUSES,
    PaymentTask,
    PaymentTaskManager,
    PaymentTaskStatus,
)


def _make_mgr() -> tuple[PaymentTaskManager, AsyncMock]:
    fake_redis = AsyncMock()
    mgr = PaymentTaskManager(redis=fake_redis, discovery=AsyncMock())
    return mgr, fake_redis


def _make_task(status: PaymentTaskStatus = PaymentTaskStatus.CREATED) -> PaymentTask:
    return PaymentTask(
        buyer_agent="buyer-1",
        seller_agent="seller-1",
        task_description="ship it",
        amount="1.00",
        status=status,
    )


@pytest.mark.asyncio
async def test_save_task_inflight_stays_ttl_less():
    """A stuck-in-PAYMENT_PENDING task must not evaporate on TTL — the
    system needs to see it in order to retry or fail it.
    """
    mgr, fake_redis = _make_mgr()
    t = _make_task(status=PaymentTaskStatus.PAYMENT_PENDING)

    await mgr._save_task(t)

    fake_redis.set.assert_awaited_once()
    fake_redis.setex.assert_not_called()


@pytest.mark.asyncio
async def test_save_task_terminal_setex_on_long_ttl():
    mgr, fake_redis = _make_mgr()
    t = _make_task(status=PaymentTaskStatus.TASK_COMPLETED)

    await mgr._save_task(t)

    fake_redis.setex.assert_awaited_once()
    key, ttl, _body = fake_redis.setex.await_args.args
    assert key == f"acn:payment_tasks:{t.task_id}"
    assert ttl == _PAYMENT_TASK_TERMINAL_TTL_SECONDS
    fake_redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_terminal_statuses_include_every_settled_state():
    """Pin the terminal set so future additions to PaymentTaskStatus
    (e.g. a new 'refund_pending') can't silently bypass retention.
    """
    expected_terminals = {
        PaymentTaskStatus.TASK_COMPLETED,
        PaymentTaskStatus.PAYMENT_RELEASED,
        PaymentTaskStatus.CANCELLED,
        PaymentTaskStatus.FAILED,
        PaymentTaskStatus.PAYMENT_FAILED,
        PaymentTaskStatus.REFUNDED,
        PaymentTaskStatus.DISPUTED,
    }
    assert _PAYMENT_TERMINAL_STATUSES == expected_terminals


@pytest.mark.asyncio
async def test_index_task_expires_both_buyer_and_seller_sets():
    mgr, fake_redis = _make_mgr()
    t = _make_task()

    await mgr._index_task(t)

    buyer_key = f"acn:payment_tasks:by_buyer:{t.buyer_agent}"
    seller_key = f"acn:payment_tasks:by_seller:{t.seller_agent}"

    sadd_pairs = {(c.args[0], c.args[1]) for c in fake_redis.sadd.await_args_list}
    assert (buyer_key, t.task_id) in sadd_pairs
    assert (seller_key, t.task_id) in sadd_pairs

    expire_calls = {c.args for c in fake_redis.expire.await_args_list}
    assert (buyer_key, _PAYMENT_INDEX_TTL_SECONDS) in expire_calls
    assert (seller_key, _PAYMENT_INDEX_TTL_SECONDS) in expire_calls


@pytest.mark.asyncio
async def test_audit_log_caps_and_expires():
    mgr, fake_redis = _make_mgr()

    await mgr._audit_log(task_id="t-1", event="created", data={})

    log_key = "acn:payment_tasks:audit:t-1"

    fake_redis.lpush.assert_awaited_once()
    assert fake_redis.lpush.await_args.args[0] == log_key

    ltrim_calls = {c.args for c in fake_redis.ltrim.await_args_list}
    assert (log_key, 0, _PAYMENT_AUDIT_CAP - 1) in ltrim_calls

    expire_calls = {c.args for c in fake_redis.expire.await_args_list}
    assert (log_key, _PAYMENT_TASK_TERMINAL_TTL_SECONDS) in expire_calls


@pytest.mark.asyncio
async def test_audit_cap_well_above_typical_task_events():
    """A normal task emits ~8 status-change events; the cap must stay
    enough higher that ordinary workflows never truncate.
    """
    assert _PAYMENT_AUDIT_CAP >= 50
