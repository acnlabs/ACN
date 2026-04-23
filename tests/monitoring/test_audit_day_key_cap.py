"""Regression test for P0-3: acn:audit:day:YYYYMMDD must be capped.

Before the fix, the per-day list had only a TTL and no length cap, so a
single high-traffic day could grow it to multi-GB and stall Redis on the
hot path. After the fix it must be ltrim'd like `type_key`.
"""

from unittest.mock import AsyncMock, call

import pytest

from acn.monitoring.audit import AuditEventType, AuditLogger


@pytest.mark.asyncio
async def test_day_key_is_trimmed_and_expired_on_every_log():
    fake_redis = AsyncMock()
    logger = AuditLogger(redis=fake_redis, max_entries=100_000, retention_days=90)

    await logger.log_event(
        event_type=AuditEventType.AGENT_REGISTERED,
        target_id="agent-1",
    )

    day_key_candidates = [
        c.args[0]
        for c in fake_redis.ltrim.await_args_list
        if c.args and c.args[0].startswith("acn:audit:day:")
    ]
    assert day_key_candidates, "day_key must be ltrim'd on every log_event"
    day_key = day_key_candidates[0]

    assert call(day_key, 0, 99_999) in fake_redis.ltrim.await_args_list
    assert call(day_key, 90 * 24 * 3600) in fake_redis.expire.await_args_list
