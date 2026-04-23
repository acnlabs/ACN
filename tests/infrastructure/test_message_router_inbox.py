"""Unit tests for MessageRouter offline inbox behavior.

Validates the inbox refactor:
- `_store_inbox` caps entries and refreshes TTL on every write
- `get_inbox` supports consume=True to atomically clear after read
- `_log_message` no longer writes per-agent sorted sets (only global audit log)
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.infrastructure.messaging.message_router import MessageRouter


@pytest.fixture
def mock_registry() -> MagicMock:
    return MagicMock()


@pytest.fixture
def fake_redis() -> AsyncMock:
    """Fully-async Redis mock.

    `AsyncMock(spec=redis.Redis)` produces MagicMocks for some methods because
    redis-py's stub types are not all annotated as coroutines. We build a plain
    AsyncMock and let each method return an AsyncMock by default.
    """
    return AsyncMock()


@pytest.fixture
def router(mock_registry, fake_redis) -> MessageRouter:
    return MessageRouter(registry=mock_registry, redis_client=fake_redis)


class TestStoreInbox:
    """`_store_inbox` is the only per-agent persistence path after the refactor."""

    @pytest.mark.asyncio
    async def test_writes_to_recipient_key_only(self, router, fake_redis):
        await router._store_inbox(
            to_agent="agent-b",
            log_entry={"route_id": "r1", "from_agent": "agent-a", "to_agent": "agent-b"},
        )

        zadd_calls = fake_redis.zadd.await_args_list
        assert len(zadd_calls) == 1
        key, _members = zadd_calls[0].args
        assert key == "acn:inbox:agent-b"

    @pytest.mark.asyncio
    async def test_caps_at_50_via_zremrangebyrank(self, router, fake_redis):
        await router._store_inbox(to_agent="agent-b", log_entry={"k": "v"})

        fake_redis.zremrangebyrank.assert_awaited_once_with(
            "acn:inbox:agent-b",
            0,
            -51,  # -(CAP + 1) — remove everything beyond newest 50
        )

    @pytest.mark.asyncio
    async def test_refreshes_ttl_on_every_write(self, router, fake_redis):
        await router._store_inbox(to_agent="agent-b", log_entry={"k": "v1"})
        await router._store_inbox(to_agent="agent-b", log_entry={"k": "v2"})

        expire_calls = fake_redis.expire.await_args_list
        assert len(expire_calls) == 2
        for call in expire_calls:
            key, ttl = call.args
            assert key == "acn:inbox:agent-b"
            assert ttl == 30 * 24 * 3600


class TestGetInbox:
    @pytest.mark.asyncio
    async def test_reads_newest_first(self, router, fake_redis):
        fake_redis.zrevrange.return_value = [
            json.dumps({"route_id": "r2"}),
            json.dumps({"route_id": "r1"}),
        ]

        result = await router.get_inbox("agent-b", limit=10)

        fake_redis.zrevrange.assert_awaited_once_with("acn:inbox:agent-b", 0, 9)
        assert result == [{"route_id": "r2"}, {"route_id": "r1"}]
        fake_redis.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_consume_deletes_key(self, router, fake_redis):
        fake_redis.zrevrange.return_value = [json.dumps({"route_id": "r1"})]

        await router.get_inbox("agent-b", limit=100, consume=True)

        fake_redis.delete.assert_awaited_once_with("acn:inbox:agent-b")

    @pytest.mark.asyncio
    async def test_consume_false_does_not_delete(self, router, fake_redis):
        fake_redis.zrevrange.return_value = []

        await router.get_inbox("agent-b", limit=100, consume=False)

        fake_redis.delete.assert_not_called()


class TestLogMessageNoLongerDoubleWrites:
    """Regression guard: `_log_message` must not touch `acn:messages:agent:*`."""

    @pytest.mark.asyncio
    async def test_only_writes_global_audit_key(self, router, fake_redis):
        message = MagicMock()
        message.model_dump.return_value = {"role": "user", "parts": []}

        await router._log_message(
            route_id="r1",
            from_agent="agent-a",
            to_agent="agent-b",
            message=message,
            direction="outbound",
        )

        # The only persistence call should be the 7-day audit log SETEX
        fake_redis.setex.assert_awaited_once()
        setex_key = fake_redis.setex.await_args.args[0]
        assert setex_key == "acn:messages:log:r1"

        # Must NOT write per-agent history sorted sets
        assert fake_redis.zadd.await_count == 0
        assert fake_redis.zremrangebyrank.await_count == 0
