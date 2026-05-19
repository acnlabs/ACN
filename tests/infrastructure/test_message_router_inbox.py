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
def mock_agent_service() -> AsyncMock:
    # Default to is_alive=True so legacy tests written for status='online''
    # keep their happy-path semantics. Offline tests override per-test.
    svc = AsyncMock()
    svc.is_alive = AsyncMock(return_value=True)
    return svc


@pytest.fixture
def fake_pipe() -> AsyncMock:
    """Mock for the redis pipeline object returned by `async with redis.pipeline()`.

    Pipeline command methods (zadd, zremrangebyrank, expire …) are called
    *synchronously* — they queue commands.  Only `execute()` is awaited.
    We therefore use plain MagicMock for command methods and AsyncMock for
    execute so the test can use `assert_called_with` / `assert_awaited`.
    """
    pipe = MagicMock()
    pipe.execute = AsyncMock(return_value=[])
    return pipe


@pytest.fixture
def fake_redis(fake_pipe) -> AsyncMock:
    """Fully-async Redis mock.

    `AsyncMock(spec=redis.Redis)` produces MagicMocks for some methods because
    redis-py's stub types are not all annotated as coroutines. We build a plain
    AsyncMock and let each method return an AsyncMock by default.

    `pipeline()` is a *sync* method in redis-py that returns an async context
    manager; we wire it up explicitly so `async with redis.pipeline(...) as p`
    works without a "coroutine does not support async context manager" error.
    """
    mock = AsyncMock()
    pipe_cm = MagicMock()
    pipe_cm.__aenter__ = AsyncMock(return_value=fake_pipe)
    pipe_cm.__aexit__ = AsyncMock(return_value=False)
    mock.pipeline = MagicMock(return_value=pipe_cm)
    return mock


@pytest.fixture
def router(mock_agent_service, fake_redis) -> MessageRouter:
    return MessageRouter(agent_service=mock_agent_service, redis_client=fake_redis)


class TestStoreInbox:
    """`_store_inbox` is the only per-agent persistence path after the refactor."""

    @pytest.mark.asyncio
    async def test_writes_to_recipient_key_only(self, router, fake_redis, fake_pipe):
        await router._store_inbox(
            to_agent="agent-b",
            log_entry={"route_id": "r1", "from_agent": "agent-a", "to_agent": "agent-b"},
        )

        # pipeline() must be called with transaction=False
        fake_redis.pipeline.assert_called_once_with(transaction=False)
        # zadd is a sync pipeline command (queued, not awaited directly)
        assert fake_pipe.zadd.call_count == 1
        key, _members = fake_pipe.zadd.call_args.args
        assert key == "acn:inbox:agent-b"
        # execute() must be awaited to flush the pipeline
        fake_pipe.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_caps_at_50_via_zremrangebyrank(self, router, fake_pipe):
        await router._store_inbox(to_agent="agent-b", log_entry={"k": "v"})

        # zremrangebyrank is a sync pipeline command
        fake_pipe.zremrangebyrank.assert_called_once_with(
            "acn:inbox:agent-b",
            0,
            -51,  # -(CAP + 1) — remove everything beyond newest 50
        )

    @pytest.mark.asyncio
    async def test_refreshes_ttl_on_every_write(self, router, fake_pipe):
        await router._store_inbox(to_agent="agent-b", log_entry={"k": "v1"})
        await router._store_inbox(to_agent="agent-b", log_entry={"k": "v2"})

        # expire is a sync pipeline command — called once per _store_inbox invocation
        assert fake_pipe.expire.call_count == 2
        for call in fake_pipe.expire.call_args_list:
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


class TestLogMessageUsesCappedStream:
    """Regression guard: `_log_message` must append to a single capped
    audit stream, not per-agent sorted sets, not per-route string keys.
    """

    @pytest.mark.asyncio
    async def test_writes_to_capped_stream_only(self, router, fake_redis):
        from acn.infrastructure.messaging.message_router import (
            MESSAGE_LOG_STREAM_KEY,
            MESSAGE_LOG_STREAM_MAXLEN,
        )

        message = MagicMock()
        message.model_dump.return_value = {"role": "user", "parts": []}

        await router._log_message(
            route_id="r1",
            from_agent="agent-a",
            to_agent="agent-b",
            message=message,
            direction="outbound",
        )

        fake_redis.xadd.assert_awaited_once()
        args = fake_redis.xadd.await_args
        assert args.args[0] == MESSAGE_LOG_STREAM_KEY
        fields = args.args[1]
        assert fields["route_id"] == "r1"
        assert fields["from_agent"] == "agent-a"
        assert fields["to_agent"] == "agent-b"
        assert fields["direction"] == "outbound"
        # Payload must be a JSON string, not a dict — stream fields are scalars.
        assert isinstance(fields["message"], str)

        # The write must request MAXLEN trimming, or the "capped" guarantee
        # is worthless and steady-state memory is unbounded.
        assert args.kwargs.get("maxlen") == MESSAGE_LOG_STREAM_MAXLEN
        assert args.kwargs.get("approximate") is True

        # Must NOT revive the old per-route string key, nor per-agent sorted sets.
        assert fake_redis.setex.await_count == 0
        assert fake_redis.zadd.await_count == 0
        assert fake_redis.zremrangebyrank.await_count == 0

    @pytest.mark.asyncio
    async def test_stream_cap_is_sane(self):
        """If MAXLEN ever drops to something that holds <1 minute of prod
        traffic, the stream stops being a useful audit tool.
        """
        from acn.infrastructure.messaging.message_router import (
            MESSAGE_LOG_STREAM_MAXLEN,
        )

        assert MESSAGE_LOG_STREAM_MAXLEN >= 10_000


class TestOfflinePrecheck:
    """route() must skip the HTTP round-trip when the registry reports
    the target agent as anything other than 'online'.

    Before this fix, an offline agent caused a 30 s httpx timeout before
    the message landed in the inbox.  After the fix, we short-circuit
    immediately on the status field returned by get_agent().
    """

    def _make_agent_info(self, status: str = "offline"):
        info = MagicMock()
        info.status = status
        info.endpoint = "http://agent-b:8000"
        return info

    @pytest.mark.asyncio
    async def test_offline_agent_skips_http_and_writes_inbox(self, router, fake_pipe):
        """Core contract: no HTTP call, message lands in inbox."""
        router.agent_service.find_agent = AsyncMock(
            return_value=self._make_agent_info("offline")
        )
        # Liveness now drives the offline short-circuit (Redis alive key,
        # single source of truth); legacy ``info.status`` is no longer read.
        router.agent_service.is_alive = AsyncMock(return_value=False)
        message = MagicMock()
        message.model_dump.return_value = {"role": "user", "parts": []}

        result = await router.route(
            from_agent="agent-a",
            to_agent="agent-b",
            message=message,
        )

        # Must return inbox status, not raise
        assert result["status"] == "inbox"
        assert "route_id" in result

        # Inbox must have been written via pipeline
        assert fake_pipe.zadd.call_count == 1
        key, _ = fake_pipe.zadd.call_args.args
        assert key == "acn:inbox:agent-b"

    @pytest.mark.asyncio
    async def test_offline_agent_does_not_open_http_connection(self, router, fake_redis):
        """No A2A client should be created or called for offline agents."""
        router.agent_service.find_agent = AsyncMock(
            return_value=self._make_agent_info("offline")
        )
        router.agent_service.is_alive = AsyncMock(return_value=False)
        message = MagicMock()
        message.model_dump.return_value = {"role": "user", "parts": []}

        # Patch _get_client to detect if it was called
        router._get_client = AsyncMock()

        await router.route(from_agent="agent-a", to_agent="agent-b", message=message)

        router._get_client.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_online_agent_proceeds_to_http(self, router, fake_redis):
        """Online agents must still go through the normal HTTP path."""
        from acn.infrastructure.messaging.message_router import create_text_message

        router.agent_service.find_agent = AsyncMock(
            return_value=self._make_agent_info("online")
        )
        # ``mock_agent_service`` fixture already defaults is_alive=True, but
        # we restate it here so this test's intent stays explicit.
        router.agent_service.is_alive = AsyncMock(return_value=True)
        # A real Message is required — the HTTP path constructs SendMessageRequest
        # which runs Pydantic validation on the message field.
        message = create_text_message("hello")

        stub_client = AsyncMock()
        stub_client.send_message = AsyncMock(return_value={"ok": True})
        router._get_client = AsyncMock(return_value=stub_client)

        result = await router.route(
            from_agent="agent-a", to_agent="agent-b", message=message
        )

        router._get_client.assert_awaited_once_with("http://agent-b:8000")
        stub_client.send_message.assert_awaited_once()
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_inbox_written_but_not_dlq_for_known_offline(self, router, fake_redis):
        """Known-offline agents write to inbox only — no DLQ entry.

        DLQ is for unexpected delivery failures; a registered-offline agent
        is an expected condition that deserves inbox delivery, not a retry queue.
        The old path wrote to both (via exception → _store_dlq); the new
        path writes only inbox so retry_dlq doesn't get flooded with messages
        destined for agents that are intentionally offline.
        """
        router.agent_service.find_agent = AsyncMock(
            return_value=self._make_agent_info("offline")
        )
        router.agent_service.is_alive = AsyncMock(return_value=False)
        message = MagicMock()
        message.model_dump.return_value = {"role": "user", "parts": []}

        await router.route(from_agent="agent-a", to_agent="agent-b", message=message)

        # lpush is used exclusively by _store_dlq
        fake_redis.lpush.assert_not_awaited()
