"""Regression test for P2-#4: ``acn:audit:type:*`` lists must be expired.

Before the fix, the per-type list had only an entry cap (``ltrim``) and no
TTL, so cold or deprecated event types would sit in Redis forever, eating
memory and diluting analyses with stale rows. After the fix it must get the
same ``expire(retention_days)`` treatment as ``day_key``.
"""

from unittest.mock import AsyncMock, call

import pytest

from acn.monitoring.audit import AuditEventType, AuditLogger


@pytest.mark.asyncio
async def test_type_key_is_trimmed_and_expired_on_every_log():
    fake_redis = AsyncMock()
    logger = AuditLogger(redis=fake_redis, max_entries=100_000, retention_days=90)

    await logger.log_event(
        event_type=AuditEventType.AGENT_REGISTERED,
        target_id="agent-1",
    )

    expected_type_key = "acn:audit:type:agent_registered"

    type_ltrim_calls = [
        c
        for c in fake_redis.ltrim.await_args_list
        if c.args and c.args[0] == expected_type_key
    ]
    assert type_ltrim_calls, "type_key must be ltrim'd on every log_event"
    assert call(expected_type_key, 0, 99_999) in fake_redis.ltrim.await_args_list

    type_expire_calls = [
        c
        for c in fake_redis.expire.await_args_list
        if c.args and c.args[0] == expected_type_key
    ]
    assert type_expire_calls, "type_key must be expired (P2-#4 regression)"
    assert call(expected_type_key, 90 * 24 * 3600) in fake_redis.expire.await_args_list


@pytest.mark.asyncio
async def test_type_key_expire_uses_configured_retention_days():
    fake_redis = AsyncMock()
    logger = AuditLogger(redis=fake_redis, max_entries=100_000, retention_days=30)

    await logger.log_event(
        event_type=AuditEventType.SECURITY_SSRF_BLOCKED,
        actor_id="ip:1.2.3.4",
    )

    expected_type_key = "acn:audit:type:security_ssrf_blocked"
    assert call(expected_type_key, 30 * 24 * 3600) in fake_redis.expire.await_args_list


@pytest.mark.asyncio
async def test_type_key_and_day_key_share_retention_window():
    """Both per-day and per-type indexes must use identical TTLs.

    Mismatched TTLs would let one window outlive the other and break the
    invariant that "a queryable type implies a queryable day", complicating
    cleanup logic.
    """
    fake_redis = AsyncMock()
    logger = AuditLogger(redis=fake_redis, max_entries=100_000, retention_days=90)

    await logger.log_event(
        event_type=AuditEventType.MESSAGE_SENT,
        actor_id="agent-a",
        target_id="agent-b",
    )

    day_ttls = {
        c.args[1]
        for c in fake_redis.expire.await_args_list
        if c.args and c.args[0].startswith("acn:audit:day:")
    }
    type_ttls = {
        c.args[1]
        for c in fake_redis.expire.await_args_list
        if c.args and c.args[0].startswith("acn:audit:type:")
    }
    assert day_ttls and type_ttls
    assert day_ttls == type_ttls, (
        f"day TTLs {day_ttls} != type TTLs {type_ttls}; both must use retention_days"
    )
