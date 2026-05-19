"""Unit tests — MessageRouter ↔ ManifestDispatcher integration.

Phase 2 PR #1 review fix (P0-A2): the manifest divert path was
shipped without end-to-end coverage at the router boundary. The
PR #1 audit caught two specific gaps that this file plugs:

1. ``mode=manifest`` recipients must NOT hit the inbox path, the
   DLQ path, or the upstream A2A client. The divert short-circuits
   identically to the ``closed`` rejection path; without an
   explicit pin a future refactor could silently route the message
   into the inbox queue (which the recipient was specifically
   trying to opt out of).

2. The router's response shape on a manifest divert is part of the
   public SDK contract — clients branch on ``status == "sent"`` for
   success and read ``delivery_mode == "manifest"`` to distinguish
   inbox vs manifest delivery. Both fields are pinned here.

The dispatcher itself is unit-tested in
``test_manifest_dispatcher.py``; here we only assert that the
router *invokes* it correctly and *returns* its result correctly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.infrastructure.messaging.manifest_dispatcher import ManifestDispatcher
from acn.infrastructure.messaging.message_router import (
    MessageRouter,
    create_text_message,
)
from acn.services.manifest_service import ManifestEntry
from acn.services.policy_service import PolicyCheckService

# ---------------------------------------------------------------------------
# Fixtures (matches test_message_router_policy.py shape so test infra is
# consistent across the two suites)
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
    return PolicyCheckService()


@pytest.fixture
def mock_agent_service() -> AsyncMock:
    # Default to is_alive=True so legacy tests written for status='online''
    # keep their happy-path semantics. Offline tests override per-test.
    svc = AsyncMock()
    svc.is_alive = AsyncMock(return_value=True)
    return svc


@pytest.fixture
def stub_dispatcher() -> MagicMock:
    """Dispatcher that returns a deterministic ``ManifestEntry``.

    Using a MagicMock with an AsyncMock ``dispatch`` rather than a
    real ``ManifestDispatcher(...)`` keeps these tests focused on
    the router's wiring contract — call shape, response shape — and
    keeps the actual divert mechanics in the dispatcher's own
    suite.
    """
    dispatcher = MagicMock(spec=ManifestDispatcher)
    dispatcher.dispatch = AsyncMock(
        return_value=ManifestEntry(
            mid="0123456789abcdef0123456789abcdef",
            sender_id="agent-a",
            summary="hello",
            ts_ms=1714377600000,
            content_size=42,
        )
    )
    return dispatcher


def _make_agent_info(
    *,
    status: str = "online",
    endpoint: str = "http://agent-b:8000",
    communication_policy: dict | None = None,
):
    info = MagicMock()
    info.status = status
    info.endpoint = endpoint
    info.communication_policy = communication_policy
    return info


# ---------------------------------------------------------------------------
# 1. Manifest mode short-circuits inbox / DLQ / HTTP — same as closed mode
# ---------------------------------------------------------------------------


class TestManifestRecipientDiverts:
    """Manifest mode is "the inbound path that doesn't hit the inbox".

    The negative assertions here mirror the ``closed`` suite — they
    are the entire reason this branch lives at the router rather
    than further upstream. Drift between the two would be a
    correctness regression: a manifest recipient must never
    discover their queue silently growing twice as fast because the
    inbox path also fired.
    """

    @pytest.mark.asyncio
    async def test_returns_status_sent_with_delivery_mode_manifest(
        self, mock_agent_service, fake_redis, policy_service, stub_dispatcher
    ):
        """The public response contract.

        SDK clients today branch on ``status == "sent"`` for success.
        Manifest divert must continue to satisfy that branch — a
        ``status == "manifest"`` response (the original PR #1 shape)
        would silently look like a *failure* to those clients.
        ``delivery_mode == "manifest"`` is the new field for clients
        that want to distinguish the two delivery paths.
        """
        mock_agent_service.find_agent = AsyncMock(
            return_value=_make_agent_info(
                communication_policy={"mode": "manifest"},
            )
        )
        router = MessageRouter(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
        )

        result = await router.route(
            from_agent="agent-a",
            to_agent="agent-b",
            message=create_text_message("hello"),
        )

        assert result["status"] == "sent"
        assert result["delivery_mode"] == "manifest"
        assert result["mid"] == "0123456789abcdef0123456789abcdef"
        assert result["ts"] == 1714377600000
        assert "route_id" in result

    @pytest.mark.asyncio
    async def test_dispatcher_called_with_router_path(
        self, mock_agent_service, fake_redis, policy_service, stub_dispatcher
    ):
        """Pin the ``path="router"`` label.

        The dispatcher records its caller in the
        ``messages_diverted_to_manifest_total{path}`` metric. Drift
        on this string would silently break the operator's ability
        to correlate divert volume with ingress channel.
        """
        mock_agent_service.find_agent = AsyncMock(
            return_value=_make_agent_info(
                communication_policy={"mode": "manifest"},
            )
        )
        router = MessageRouter(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
        )

        await router.route(
            from_agent="agent-a",
            to_agent="agent-b",
            message=create_text_message("hi"),
        )

        stub_dispatcher.dispatch.assert_awaited_once()
        kwargs = stub_dispatcher.dispatch.await_args.kwargs
        assert kwargs["owner_id"] == "agent-b"
        assert kwargs["sender_id"] == "agent-a"
        assert kwargs["path"] == "router"

    @pytest.mark.asyncio
    async def test_does_not_open_http_connection(
        self, mock_agent_service, fake_redis, policy_service, stub_dispatcher
    ):
        """The whole point of the divert: don't bother the recipient.

        If we accidentally fall through to ``_get_client`` after
        dispatching, the recipient would receive the message twice
        (once via manifest queue, once via direct HTTP push) — and
        their A2A endpoint may not even be reachable, which would
        also crash the divert.
        """
        mock_agent_service.find_agent = AsyncMock(
            return_value=_make_agent_info(
                communication_policy={"mode": "manifest"},
            )
        )
        router = MessageRouter(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
        )
        router._get_client = AsyncMock()

        await router.route(
            from_agent="agent-a",
            to_agent="agent-b",
            message=create_text_message("hi"),
        )

        router._get_client.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_not_write_inbox(
        self, mock_agent_service, fake_redis, fake_pipe, policy_service, stub_dispatcher
    ):
        """Manifest is the *opposite* of inbox — recipient pulled out.

        ``_store_inbox`` calls ``pipe.zadd``. If that fires under a
        manifest-mode recipient, we've accidentally double-stored.
        """
        mock_agent_service.find_agent = AsyncMock(
            return_value=_make_agent_info(
                communication_policy={"mode": "manifest"},
            )
        )
        router = MessageRouter(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
        )

        await router.route(
            from_agent="agent-a",
            to_agent="agent-b",
            message=create_text_message("hi"),
        )

        assert fake_pipe.zadd.call_count == 0

    @pytest.mark.asyncio
    async def test_offline_recipient_still_diverts(
        self, mock_agent_service, fake_redis, fake_pipe, policy_service, stub_dispatcher
    ):
        """Edge case: ``manifest`` ∧ ``offline``.

        The recipient may be offline — that's fine, manifest mode
        is *designed* for asynchronous pickup. The divert must
        still happen; we must NOT fall through to the offline-inbox
        branch (which would defeat the recipient's manifest opt-in).
        """
        mock_agent_service.find_agent = AsyncMock(
            return_value=_make_agent_info(
                status="offline",
                communication_policy={"mode": "manifest"},
            )
        )
        router = MessageRouter(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
        )

        result = await router.route(
            from_agent="agent-a",
            to_agent="agent-b",
            message=create_text_message("hi"),
        )

        assert result["delivery_mode"] == "manifest"
        assert fake_pipe.zadd.call_count == 0
        stub_dispatcher.dispatch.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. system:* sender bypasses manifest mode (same exemption as closed)
# ---------------------------------------------------------------------------


class TestSystemSenderBypassesManifest:
    @pytest.mark.asyncio
    async def test_system_sender_reaches_http_path_under_manifest_recipient(
        self, mock_agent_service, fake_redis, policy_service, stub_dispatcher
    ):
        """The system bypass is single-source (PolicyCheckService).

        Pinning at the router so a refactor that strips the prefix
        before reaching the policy gate (e.g. service mutates
        sender_id to drop ``system:``) is loud. ACN-internal
        notifications must never be silently parked in the manifest
        queue — they're often time-sensitive (rate-limit warnings,
        admin reset confirmations) and the recipient may not poll.
        """
        mock_agent_service.find_agent = AsyncMock(
            return_value=_make_agent_info(
                communication_policy={"mode": "manifest"},
            )
        )
        router = MessageRouter(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
        )

        stub_client = AsyncMock()
        stub_client.send_message = AsyncMock(return_value={"ok": True})
        router._get_client = AsyncMock(return_value=stub_client)

        result = await router.route(
            from_agent="system:audit-pipeline",
            to_agent="agent-b",
            message=create_text_message("notify"),
        )

        assert result == {"ok": True}
        router._get_client.assert_awaited_once()
        # The dispatcher must NOT have fired — the bypass takes
        # precedence over manifest divert.
        stub_dispatcher.dispatch.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. Wiring guard: manifest mode + missing dispatcher = loud failure
# ---------------------------------------------------------------------------


class TestMissingDispatcherFailsLoudly:
    @pytest.mark.asyncio
    async def test_raises_runtime_error_when_dispatcher_unwired(
        self, mock_agent_service, fake_redis, policy_service
    ):
        """Configuration error must surface immediately.

        Silent fall-through to inbox would defeat the recipient's
        opt-in; silent drop would lose the message without trace.
        Both are worse than a loud RuntimeError that the operator
        sees during deploy or in their first manifest send.
        """
        mock_agent_service.find_agent = AsyncMock(
            return_value=_make_agent_info(
                communication_policy={"mode": "manifest"},
            )
        )
        router = MessageRouter(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=None,
        )

        with pytest.raises(RuntimeError, match="ManifestDispatcher"):
            await router.route(
                from_agent="agent-a",
                to_agent="agent-b",
                message=create_text_message("hi"),
            )
