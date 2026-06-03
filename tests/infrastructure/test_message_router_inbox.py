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


class TestDirectDelivery:
    """ADR-0012 "reachable == online": route() ATTEMPTS the direct push even
    when the Redis alive key is absent.

    A reachable agent whose heartbeat loop died still receives in real time,
    and a completed delivery renews the alive TTL. Only the FAILURE path
    branches on the alive key:
      - believed-online + failed  → inbox + DLQ + raise (retry-worthy)
      - believed-offline + failed → inbox only, graceful envelope (no raise)
    """

    def _make_agent_info(self, status: str = "offline"):
        info = MagicMock()
        info.status = status
        info.endpoint = "http://agent-b:8000"
        return info

    @pytest.mark.asyncio
    async def test_offline_but_reachable_delivers_in_realtime(self, router, fake_redis, fake_pipe):
        """Alive key absent but endpoint reachable → real-time delivery, NOT inbox.

        This is the core regression for the agentmother case: an agent whose
        heartbeat stopped (alive key expired) but whose HTTP server is still up
        must be pushed to in real time instead of being stranded on the inbox.
        """
        from acn.infrastructure.messaging.message_router import create_text_message

        router.agent_service.find_agent = AsyncMock(
            return_value=self._make_agent_info("offline")
        )
        router.agent_service.is_alive = AsyncMock(return_value=False)
        message = create_text_message("hello")

        stub_client = AsyncMock()
        stub_client.send_message = AsyncMock(return_value={"ok": True})
        router._get_client = AsyncMock(return_value=stub_client)

        result = await router.route(
            from_agent="agent-a", to_agent="agent-b", message=message
        )

        # Delivered for real — not parked in the inbox.
        assert result == {"ok": True}
        router._get_client.assert_awaited_once_with("http://agent-b:8000")
        stub_client.send_message.assert_awaited_once()
        fake_pipe.zadd.assert_not_called()  # no inbox write
        # Success renews the alive TTL so traffic keeps the agent online.
        router.agent_service.touch_alive.assert_awaited_once_with("agent-b")

    @pytest.mark.asyncio
    async def test_offline_unreachable_parks_in_inbox(self, router, fake_pipe):
        """Alive key absent AND endpoint unreachable → graceful inbox, no raise."""
        router.agent_service.find_agent = AsyncMock(
            return_value=self._make_agent_info("offline")
        )
        router.agent_service.is_alive = AsyncMock(return_value=False)
        # Simulate a down host: the connect attempt fails fast.
        router._get_client = AsyncMock(side_effect=RuntimeError("connect failed"))
        message = MagicMock()
        message.model_dump.return_value = {"role": "user", "parts": []}

        result = await router.route(
            from_agent="agent-a", to_agent="agent-b", message=message
        )

        # Graceful inbox envelope, not a raise.
        assert result["status"] == "inbox"
        assert result["delivery_mode"] == "inbox"
        assert "route_id" in result

        assert fake_pipe.zadd.call_count == 1
        key, _ = fake_pipe.zadd.call_args.args
        assert key == "acn:inbox:agent-b"
        # A failed probe must NOT renew the alive key.
        router.agent_service.touch_alive.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_offline_unreachable_writes_inbox_but_not_dlq(self, router, fake_redis):
        """Believed-offline + unreachable is an expected condition → no DLQ.

        DLQ is for unexpected delivery failures; flooding it with messages for
        agents that are simply offline would make retry_dlq useless.
        """
        router.agent_service.find_agent = AsyncMock(
            return_value=self._make_agent_info("offline")
        )
        router.agent_service.is_alive = AsyncMock(return_value=False)
        router._get_client = AsyncMock(side_effect=RuntimeError("connect failed"))
        message = MagicMock()
        message.model_dump.return_value = {"role": "user", "parts": []}

        await router.route(from_agent="agent-a", to_agent="agent-b", message=message)

        # lpush is used exclusively by _store_dlq
        fake_redis.lpush.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_online_agent_delivers_and_renews_alive(self, router, fake_redis):
        """Online agents go through the HTTP path and renew the alive TTL."""
        from acn.infrastructure.messaging.message_router import create_text_message

        router.agent_service.find_agent = AsyncMock(
            return_value=self._make_agent_info("online")
        )
        router.agent_service.is_alive = AsyncMock(return_value=True)
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
        router.agent_service.touch_alive.assert_awaited_once_with("agent-b")

    @pytest.mark.asyncio
    async def test_online_failure_writes_dlq_and_raises(self, router, fake_redis, fake_pipe):
        """Believed-online but delivery fails → inbox + DLQ + raise (legacy contract)."""
        router.agent_service.find_agent = AsyncMock(
            return_value=self._make_agent_info("online")
        )
        router.agent_service.is_alive = AsyncMock(return_value=True)
        router._get_client = AsyncMock(side_effect=RuntimeError("boom"))
        message = MagicMock()
        message.model_dump.return_value = {"role": "user", "parts": []}

        with pytest.raises(RuntimeError, match="boom"):
            await router.route(
                from_agent="agent-a", to_agent="agent-b", message=message
            )

        # Inbox written …
        assert fake_pipe.zadd.call_count == 1
        # … and DLQ written (lpush) for retry.
        fake_redis.lpush.assert_awaited()
        # A failed delivery must NOT renew the alive key.
        router.agent_service.touch_alive.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_audit_failure_after_delivery_does_not_reinbox(
        self, router, fake_redis, fake_pipe
    ):
        """A Redis blip while AUDITING a delivered message must not duplicate it
        to the inbox, queue a DLQ retry, or raise — the agent already has it.

        Regression: post-delivery bookkeeping lives outside the failure handler.
        """
        from acn.infrastructure.messaging.message_router import create_text_message

        router.agent_service.find_agent = AsyncMock(
            return_value=self._make_agent_info("online")
        )
        router.agent_service.is_alive = AsyncMock(return_value=True)
        message = create_text_message("hello")

        stub_client = AsyncMock()
        stub_client.send_message = AsyncMock(return_value={"ok": True})
        router._get_client = AsyncMock(return_value=stub_client)
        # Audit append (xadd) fails — but only AFTER delivery already succeeded.
        fake_redis.xadd = AsyncMock(side_effect=RuntimeError("redis blip"))

        result = await router.route(
            from_agent="agent-a", to_agent="agent-b", message=message
        )

        assert result == {"ok": True}  # delivered, not re-routed / raised
        fake_pipe.zadd.assert_not_called()  # no inbox duplicate
        fake_redis.lpush.assert_not_awaited()  # no DLQ entry
        # Liveness was still renewed (it runs before the best-effort audit).
        router.agent_service.touch_alive.assert_awaited_once_with("agent-b")

    @pytest.mark.asyncio
    async def test_ssrf_violation_propagates_not_inbox(self, router, fake_pipe):
        """A blocked endpoint is a security signal — surfaced, never diverted to inbox."""
        from acn.security import SSRFViolation

        router.agent_service.find_agent = AsyncMock(
            return_value=self._make_agent_info("offline")
        )
        router.agent_service.is_alive = AsyncMock(return_value=False)
        router._get_client = AsyncMock(side_effect=SSRFViolation("blocked"))
        message = MagicMock()
        message.model_dump.return_value = {"role": "user", "parts": []}

        with pytest.raises(SSRFViolation):
            await router.route(
                from_agent="agent-a", to_agent="agent-b", message=message
            )

        # No inbox fallback for a security rejection.
        fake_pipe.zadd.assert_not_called()
