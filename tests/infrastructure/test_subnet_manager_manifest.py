"""Unit tests — SubnetManager ↔ ManifestDispatcher integration.

Phase 2 PR #1 review fix (P0-A1, P0-A2): the original PR #1
shipped manifest divert only on the HTTP/A2A path inside
``MessageRouter``. The subnet path was using
``check_inbound_or_raise`` which silently allows manifest mode
through (it only raises on ``allow=False``), so a manifest-mode
recipient connected via subnet WebSocket would still receive
direct ``A2A_REQUEST`` frames — the recipient's opt-in was
silently bypassed.

This file pins the fixed contract:

- Manifest-mode recipient on a subnet connection: divert into the
  manifest queue instead of pushing the WebSocket frame.
- The dispatcher gets ``path="subnet"`` so the divert metric can
  separate ingress channels.
- Closed mode still raises ``PolicyRejected`` (Phase 1 contract
  preserved).
- ``manifest_dispatcher=None`` + manifest recipient → loud
  ``RuntimeError``, not silent fall-through.
- Open / system bypass paths unchanged.

The dispatcher itself is unit-tested separately; here we only
assert the wiring contract.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.infrastructure.messaging.manifest_dispatcher import ManifestDispatcher
from acn.infrastructure.messaging.subnet_manager import (
    GatewayConnection,
    SubnetManager,
)
from acn.services.manifest_service import ManifestEntry
from acn.services.policy_service import PolicyCheckService

# ---------------------------------------------------------------------------
# Fixtures (matches test_subnet_manager_policy.py shape)
# ---------------------------------------------------------------------------


@pytest.fixture
def policy_service() -> PolicyCheckService:
    return PolicyCheckService()


@pytest.fixture
def fake_redis() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_agent_service() -> MagicMock:
    return MagicMock()


@pytest.fixture
def stub_dispatcher() -> MagicMock:
    """Mock dispatcher returning a deterministic ``ManifestEntry``."""
    dispatcher = MagicMock(spec=ManifestDispatcher)
    dispatcher.dispatch = AsyncMock(
        return_value=ManifestEntry(
            mid="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            sender_id="agent-a",
            summary="hi",
            ts_ms=1714377600000,
            content_size=24,
        )
    )
    return dispatcher


def _make_agent_info(communication_policy: dict | None = None) -> MagicMock:
    info = MagicMock()
    info.communication_policy = communication_policy
    return info


def _build_manager(
    *,
    agent_service: AsyncMock,
    redis_client: AsyncMock,
    policy_service: PolicyCheckService | None,
    manifest_dispatcher: MagicMock | None = None,
    slug: str = "public",
    agent_id: str = "agent-b",
) -> tuple[SubnetManager, MagicMock]:
    manager = SubnetManager(
        agent_service=agent_service,
        redis_client=redis_client,
        policy_service=policy_service,
        manifest_dispatcher=manifest_dispatcher,
    )
    websocket = MagicMock()
    websocket.send_json = AsyncMock()
    connection = GatewayConnection(
        connection_id="conn-1",
        slug=slug,
        agent_id=agent_id,
        websocket=websocket,
    )
    manager._subnets[slug].connections[agent_id] = connection
    return manager, websocket


# ---------------------------------------------------------------------------
# 1. Manifest mode → divert, not WebSocket push
# ---------------------------------------------------------------------------


class TestManifestRecipientDivertsOnSubnet:
    """The behaviour the PR #1 review caught as broken: subnet
    forwards to a manifest-mode recipient must NOT issue a direct
    WebSocket ``A2A_REQUEST`` frame. Doing so would defeat the
    recipient's manifest opt-in (whose entire premise is "I'll pull
    when ready").
    """

    @pytest.mark.asyncio
    async def test_does_not_send_websocket_frame(
        self, mock_agent_service, fake_redis, policy_service, stub_dispatcher
    ):
        """The single most important assertion: no ``send_json``."""
        mock_agent_service.find_agent = AsyncMock(
            return_value=_make_agent_info({"mode": "manifest"})
        )
        manager, websocket = _build_manager(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
        )

        result = await manager.forward_request(
            "public",
            "agent-b",
            {
                "role": "user",
                "message_id": "msg-1",
                "parts": [{"kind": "text", "text": "hello"}],
            },
            from_agent="agent-a",
        )

        websocket.send_json.assert_not_awaited()
        assert result["status"] == "sent"
        assert result["delivery_mode"] == "manifest"
        assert result["mid"] == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        assert result["ts"] == 1714377600000

    @pytest.mark.asyncio
    async def test_dispatcher_called_with_subnet_path(
        self, mock_agent_service, fake_redis, policy_service, stub_dispatcher
    ):
        """Pin the ``path="subnet"`` metric label so the divert
        counter can separate subnet vs router traffic. Drift on this
        string would silently merge the two channels in dashboards.
        """
        mock_agent_service.find_agent = AsyncMock(
            return_value=_make_agent_info({"mode": "manifest"})
        )
        manager, _ws = _build_manager(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
        )

        await manager.forward_request(
            "public",
            "agent-b",
            {
                "role": "user",
                "message_id": "msg-1",
                "parts": [{"kind": "text", "text": "hi"}],
            },
            from_agent="agent-a",
        )

        stub_dispatcher.dispatch.assert_awaited_once()
        kwargs = stub_dispatcher.dispatch.await_args.kwargs
        assert kwargs["owner_id"] == "agent-b"
        assert kwargs["sender_id"] == "agent-a"
        assert kwargs["path"] == "subnet"


# ---------------------------------------------------------------------------
# 2. Closed mode still raises (Phase 1 contract preserved)
# ---------------------------------------------------------------------------


class TestClosedStillRejectsAfterManifestRefactor:
    """Regression guard for the manifest refactor.

    The decision-table change replaced ``check_inbound_or_raise``
    with ``check_inbound``. Closed mode used to raise via the
    ``..._or_raise`` helper; we now raise manually inside the
    branch. This test makes sure the new code path's
    ``PolicyRejected`` payload is identical to the old one — no
    drift in ``reason`` or ``reject_reason``, no missing
    ``recipient_id``.
    """

    @pytest.mark.asyncio
    async def test_closed_recipient_raises_policy_rejected(
        self, mock_agent_service, fake_redis, policy_service, stub_dispatcher
    ):
        from acn.core.exceptions import PolicyRejected

        mock_agent_service.find_agent = AsyncMock(
            return_value=_make_agent_info(
                {"mode": "closed", "reject_reason": "do not disturb"}
            )
        )
        manager, websocket = _build_manager(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
        )

        with pytest.raises(PolicyRejected) as exc_info:
            await manager.forward_request(
                "public",
                "agent-b",
                {"role": "user"},
                from_agent="agent-a",
            )

        assert exc_info.value.reason == "policy_closed"
        assert exc_info.value.reject_reason == "do not disturb"
        assert exc_info.value.recipient_id == "agent-b"
        websocket.send_json.assert_not_awaited()
        # Closed branch must not invoke the dispatcher.
        stub_dispatcher.dispatch.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. Wiring guard: manifest mode + missing dispatcher = RuntimeError
# ---------------------------------------------------------------------------


class TestMissingDispatcherFailsLoudly:
    @pytest.mark.asyncio
    async def test_raises_runtime_error_when_dispatcher_unwired(
        self, mock_agent_service, fake_redis, policy_service
    ):
        """Configuration error must surface immediately on the subnet
        path too. Same rationale as the router-side guard:

        - Silent fall-through to WebSocket push would defeat the
          recipient's opt-in (the bug PR #1 review caught).
        - Silent drop would lose the message without trace.

        A loud RuntimeError lets the operator see the missing wiring
        on first manifest send rather than chasing ghost messages
        across logs.
        """
        mock_agent_service.find_agent = AsyncMock(
            return_value=_make_agent_info({"mode": "manifest"})
        )
        manager, websocket = _build_manager(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=None,
        )

        with pytest.raises(RuntimeError, match="ManifestDispatcher"):
            await manager.forward_request(
                "public",
                "agent-b",
                {"role": "user", "parts": []},
                from_agent="agent-a",
            )

        websocket.send_json.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. Open / system paths unaffected by the manifest refactor
# ---------------------------------------------------------------------------


class TestOpenAndSystemUnaffected:
    @pytest.mark.asyncio
    async def test_open_recipient_still_uses_websocket_push(
        self, mock_agent_service, fake_redis, policy_service, stub_dispatcher
    ):
        """Regression guard: open recipients still get WebSocket
        push. The manifest branch must never accidentally swallow
        non-manifest traffic."""
        mock_agent_service.find_agent = AsyncMock(
            return_value=_make_agent_info({"mode": "open"})
        )
        manager, websocket = _build_manager(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
        )

        with pytest.raises(TimeoutError):
            await manager.forward_request(
                "public",
                "agent-b",
                {"role": "user"},
                timeout=0.05,
                from_agent="agent-a",
            )

        websocket.send_json.assert_awaited_once()
        stub_dispatcher.dispatch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_system_sender_bypasses_manifest_recipient(
        self, mock_agent_service, fake_redis, policy_service, stub_dispatcher
    ):
        """``system:*`` exemption must beat manifest divert on the
        subnet path too — internal notifications are time-sensitive
        and shouldn't sit in a manifest queue waiting for the
        recipient to poll."""
        mock_agent_service.find_agent = AsyncMock(
            return_value=_make_agent_info({"mode": "manifest"})
        )
        manager, websocket = _build_manager(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
        )

        with pytest.raises(TimeoutError):
            await manager.forward_request(
                "public",
                "agent-b",
                {"role": "user"},
                timeout=0.05,
                from_agent="system:audit-pipeline",
            )

        # The bypass observable: WS frame did go out, dispatcher
        # was NOT invoked (so no manifest queue write either).
        websocket.send_json.assert_awaited_once()
        stub_dispatcher.dispatch.assert_not_awaited()
