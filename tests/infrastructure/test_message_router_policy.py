"""Unit tests for MessageRouter ↔ PolicyCheckService integration.

Covers Step 2.2 of the communication-policy rollout:

- Inbound messages to ``closed`` recipients short-circuit at the gate.
  Specifically: no inbox write, no DLQ write, no HTTP connection
  opened. These three negatives together are the entire reason the
  check lives at this layer rather than in MessageService.
- ``system:*`` senders bypass policy entirely (single-source exemption
  rule shared with PolicyCheckService unit tests).
- ``policy_service=None`` (rollout opt-out) preserves pre-Phase-1
  behaviour so legacy fixtures keep passing without rewiring.
- ``retry_dlq`` honours the recipient's *current* policy: a
  ``closed`` policy installed after the message was queued causes
  the entry to be dropped rather than requeued.

See docs/features/acn-communication-economic-model.md
"Phase 1 网关执行点决策" for the design rationale these tests guard.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.exceptions import PolicyRejected
from acn.infrastructure.messaging.message_router import MessageRouter
from acn.services.policy_service import PolicyCheckService

# ---------------------------------------------------------------------------
# Fixtures (mirrors test_message_router_inbox.py so test infra stays uniform)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_pipe() -> AsyncMock:
    pipe = MagicMock()
    pipe.execute = AsyncMock(return_value=[])
    return pipe


@pytest.fixture
def fake_redis(fake_pipe) -> AsyncMock:
    mock = AsyncMock()
    pipe_cm = MagicMock()
    pipe_cm.__aenter__ = AsyncMock(return_value=fake_pipe)
    pipe_cm.__aexit__ = AsyncMock(return_value=False)
    mock.pipeline = MagicMock(return_value=pipe_cm)
    return mock


@pytest.fixture
def policy_service() -> PolicyCheckService:
    """Real service — it's pure logic so there's no value in mocking it."""
    return PolicyCheckService()


@pytest.fixture
def mock_registry() -> MagicMock:
    return MagicMock()


def _make_agent_info(
    *,
    status: str = "online",
    endpoint: str = "http://agent-b:8000",
    communication_policy: dict | None = None,
):
    """Build a minimal AgentInfo-shaped mock for router consumption.

    We use MagicMock rather than the real ``AgentInfo`` Pydantic model
    so test setup stays terse — ``router.route`` only accesses three
    attributes (``status``, ``endpoint``, ``communication_policy``).
    """
    info = MagicMock()
    info.status = status
    info.endpoint = endpoint
    info.communication_policy = communication_policy
    return info


def _make_message():
    """Build the minimum message stub ``route()`` will accept on the
    short-circuit paths (we never reach SendMessageRequest validation
    on closed-policy paths because we want to assert *no HTTP* fires).
    """
    message = MagicMock()
    message.model_dump.return_value = {"role": "user", "parts": []}
    return message


# ---------------------------------------------------------------------------
# 1. Closed recipient → PolicyRejected, no side effects
# ---------------------------------------------------------------------------


class TestClosedRecipientShortCircuits:
    """A ``closed`` recipient must produce zero side effects: the gate
    rejects before *any* of the inbox / DLQ / HTTP machinery runs.

    The three negative assertions below are the entire reason the
    check is installed at the router rather than higher up — the
    router is the single point where all three sinks converge.
    """

    @pytest.mark.asyncio
    async def test_raises_policy_rejected(
        self, mock_registry, fake_redis, policy_service
    ):
        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info(
                communication_policy={"mode": "closed", "reject_reason": "busy"},
            )
        )
        router = MessageRouter(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
        )

        with pytest.raises(PolicyRejected) as exc_info:
            await router.route(
                from_agent="agent-a",
                to_agent="agent-b",
                message=_make_message(),
            )

        assert exc_info.value.reason == "policy_closed"
        assert exc_info.value.reject_reason == "busy"
        assert exc_info.value.recipient_id == "agent-b"

    @pytest.mark.asyncio
    async def test_does_not_write_inbox(
        self, mock_registry, fake_redis, fake_pipe, policy_service
    ):
        """Pinning that ``closed`` rejection does NOT silently park the
        message in the recipient's inbox — that would defeat the whole
        point of opting out of inbound traffic."""
        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info(
                communication_policy={"mode": "closed"},
            )
        )
        router = MessageRouter(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
        )

        with pytest.raises(PolicyRejected):
            await router.route(
                from_agent="agent-a",
                to_agent="agent-b",
                message=_make_message(),
            )

        # _store_inbox uses pipe.zadd; it must not have been queued.
        assert fake_pipe.zadd.call_count == 0

    @pytest.mark.asyncio
    async def test_does_not_write_dlq(
        self, mock_registry, fake_redis, policy_service
    ):
        """Pinning that policy rejection is treated as access-denied,
        not as a retryable delivery failure. DLQ is reserved for
        genuine network/upstream errors."""
        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info(
                communication_policy={"mode": "closed"},
            )
        )
        router = MessageRouter(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
        )

        with pytest.raises(PolicyRejected):
            await router.route(
                from_agent="agent-a",
                to_agent="agent-b",
                message=_make_message(),
            )

        # lpush is used exclusively by _store_dlq.
        fake_redis.lpush.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_not_open_http_connection(
        self, mock_registry, fake_redis, policy_service
    ):
        """Pinning that the rejection happens before any A2A client
        instantiation. Even SSRF resolution shouldn't fire on a
        closed-policy path."""
        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info(
                communication_policy={"mode": "closed"},
            )
        )
        router = MessageRouter(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
        )
        # Replace _get_client with a sentry: any call signals the
        # short-circuit didn't fire early enough.
        router._get_client = AsyncMock()

        with pytest.raises(PolicyRejected):
            await router.route(
                from_agent="agent-a",
                to_agent="agent-b",
                message=_make_message(),
            )

        router._get_client.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_offline_closed_agent_still_rejected(
        self, mock_registry, fake_redis, fake_pipe, policy_service
    ):
        """Edge case worth pinning: ``closed`` ∧ ``offline`` must still
        reject (no inbox write). Without the early policy gate the
        offline-pre-check branch would happily park the message."""
        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info(
                status="offline",
                communication_policy={"mode": "closed"},
            )
        )
        router = MessageRouter(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
        )

        with pytest.raises(PolicyRejected):
            await router.route(
                from_agent="agent-a",
                to_agent="agent-b",
                message=_make_message(),
            )

        assert fake_pipe.zadd.call_count == 0


# ---------------------------------------------------------------------------
# 2. System sender exemption survives at the router boundary
# ---------------------------------------------------------------------------


class TestSystemSenderExemption:
    @pytest.mark.asyncio
    async def test_system_sender_bypasses_closed_recipient(
        self, mock_registry, fake_redis, policy_service
    ):
        """Pinning at the router so a refactor that loses the exemption
        on the way down (e.g. service mutates sender_id) is loud."""
        from acn.infrastructure.messaging.message_router import create_text_message

        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info(
                communication_policy={"mode": "closed"},
            )
        )
        router = MessageRouter(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
        )

        stub_client = AsyncMock()
        stub_client.send_message = AsyncMock(return_value={"ok": True})
        router._get_client = AsyncMock(return_value=stub_client)

        result = await router.route(
            from_agent="system:chat-backend",
            to_agent="agent-b",
            message=create_text_message("notify"),
        )

        # Reaches the HTTP path; exemption only useful if delivery proceeds.
        assert result == {"ok": True}
        router._get_client.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. Open recipient: policy installed but transparent
# ---------------------------------------------------------------------------


class TestOpenRecipientUnaffected:
    @pytest.mark.asyncio
    async def test_open_policy_passes_through_to_http(
        self, mock_registry, fake_redis, policy_service
    ):
        """Regression guard: installing the policy gate must not change
        the happy-path delivery flow for ``open`` agents (the legacy
        default)."""
        from acn.infrastructure.messaging.message_router import create_text_message

        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info(
                communication_policy={"mode": "open"},
            )
        )
        router = MessageRouter(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
        )

        stub_client = AsyncMock()
        stub_client.send_message = AsyncMock(return_value={"ok": True})
        router._get_client = AsyncMock(return_value=stub_client)

        result = await router.route(
            from_agent="agent-a",
            to_agent="agent-b",
            message=create_text_message("hello"),
        )

        assert result == {"ok": True}
        router._get_client.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_policy_field_treated_as_open(
        self, mock_registry, fake_redis, policy_service
    ):
        """Backward compat: an AgentInfo without ``communication_policy``
        (pre-Step-2.2 legacy entry) must be treated as ``open``."""
        from acn.infrastructure.messaging.message_router import create_text_message

        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info(communication_policy=None)
        )
        router = MessageRouter(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
        )

        stub_client = AsyncMock()
        stub_client.send_message = AsyncMock(return_value={"ok": True})
        router._get_client = AsyncMock(return_value=stub_client)

        result = await router.route(
            from_agent="agent-a",
            to_agent="agent-b",
            message=create_text_message("hello"),
        )

        assert result == {"ok": True}


# ---------------------------------------------------------------------------
# 4. policy_service=None opt-out preserves legacy behaviour
# ---------------------------------------------------------------------------


class TestPolicyServiceOptional:
    @pytest.mark.asyncio
    async def test_no_policy_service_skips_gate(
        self, mock_registry, fake_redis
    ):
        """Pinning the rollout opt-out: routers built without a policy
        service (legacy tests, scripts, the api.py that's about to be
        wired in Step 2.7) must behave exactly as before. Loss of this
        guarantee would force every test fixture to be rewired in the
        same PR."""
        from acn.infrastructure.messaging.message_router import create_text_message

        # Even a closed policy must NOT short-circuit when the service
        # is not installed.
        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info(
                communication_policy={"mode": "closed"},
            )
        )
        router = MessageRouter(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=None,
        )

        stub_client = AsyncMock()
        stub_client.send_message = AsyncMock(return_value={"ok": True})
        router._get_client = AsyncMock(return_value=stub_client)

        result = await router.route(
            from_agent="agent-a",
            to_agent="agent-b",
            message=create_text_message("hello"),
        )

        assert result == {"ok": True}


# ---------------------------------------------------------------------------
# 5. DLQ retry honors the recipient's current policy
# ---------------------------------------------------------------------------


class TestDlqRetryHonorsCurrentPolicy:
    """The contract pinned here: when ``retry_dlq`` re-runs a queued
    entry and the recipient's ``communication_policy`` now denies it,
    drop without requeue. Forcing the message through would silently
    violate the recipient's *current* opt-out — the entire point of
    ``closed``.

    These tests mock ``router.route`` directly rather than feeding a
    real DLQ entry through the rebuild + route flow. Why: ``retry_dlq``
    rebuilds the A2A ``Message`` from the stored payload, and that
    rebuild path has its own constraints (e.g. ``messageId`` must be
    present) that are orthogonal to what we want to verify here. Mocking
    ``route`` isolates the ``except PolicyRejected`` branch we're
    actually testing.
    """

    @pytest.mark.asyncio
    async def test_policy_rejected_during_retry_drops_without_requeue(
        self, mock_registry, fake_redis, policy_service
    ):
        dlq_entry = {
            "route_id": "deadbeef",
            "from_agent": "agent-a",
            "to_agent": "agent-b",
            "message": {
                "role": "user",
                "message_id": "msg-1",
                "parts": [{"kind": "text", "text": "stale"}],
            },
            "error": "previous transient failure",
            "timestamp": "2026-04-29T10:00:00+00:00",
            "retry_count": 0,
        }

        fake_redis.rpop = AsyncMock(side_effect=[json.dumps(dlq_entry), None])

        router = MessageRouter(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
        )
        # Route is what the retry path calls into. We force it to raise
        # PolicyRejected so the test is exclusively about how retry_dlq
        # *handles* that exception, not about whether the policy gate
        # itself fires (covered by TestClosedRecipientShortCircuits).
        router.route = AsyncMock(  # type: ignore[method-assign]
            side_effect=PolicyRejected(
                reason="policy_closed",
                reject_reason="busy",
                recipient_id="agent-b",
            )
        )

        retried = await router.retry_dlq()

        assert retried == 0
        # The critical assertion — entry must NOT be re-queued.
        fake_redis.lpush.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_generic_exception_during_retry_still_requeues(
        self, mock_registry, fake_redis, policy_service
    ):
        """Regression guard: only ``PolicyRejected`` triggers the new
        drop path. Genuine network failures must keep their existing
        requeue behaviour, otherwise this change quietly turns every
        DLQ entry into a one-shot delivery."""
        dlq_entry = {
            "route_id": "cafebabe",
            "from_agent": "agent-a",
            "to_agent": "agent-b",
            "message": {
                "role": "user",
                "message_id": "msg-2",
                "parts": [{"kind": "text", "text": "still trying"}],
            },
            "error": "previous transient failure",
            "timestamp": "2026-04-29T10:00:00+00:00",
            "retry_count": 0,
        }

        fake_redis.rpop = AsyncMock(side_effect=[json.dumps(dlq_entry), None])

        router = MessageRouter(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
        )
        router.route = AsyncMock(  # type: ignore[method-assign]
            side_effect=ConnectionError("upstream gone")
        )

        retried = await router.retry_dlq()

        assert retried == 0
        # Non-policy failures still re-queue.
        fake_redis.lpush.assert_awaited_once()
