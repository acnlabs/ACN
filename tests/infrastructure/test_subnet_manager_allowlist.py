"""Unit tests — SubnetManager ↔ AllowlistService integration (Phase 2 PR #2).

Sister suite to ``test_subnet_manager_manifest.py``. Allowlist mode
must behave **identically** across the HTTP path (MessageRouter)
and the subnet WebSocket path (SubnetManager). Allowlist
non-members on a subnet connection must NOT receive direct
``A2A_REQUEST`` frames — they must divert into the manifest queue,
exactly like manifest-mode recipients in PR #1.

The PR #2 plan flagged this as **P0-4** because PR #1 had been
shipped with manifest-mode subnet support broken before — without
explicit allowlist coverage on the subnet path the same gap could
land again.

What this file pins:

1. Allowlist non-member on a subnet recipient → divert via
   dispatcher, ``path="subnet"`` metric label, no WebSocket push.
2. Allowlist member on a subnet recipient → normal WebSocket
   push, dispatcher untouched.
3. Empty allowlist + allowlist mode → divert (graceful UX).
4. ``allowlist_service=None`` + allowlist policy → divert
   (rollout-opt-out fail-closed safety).
5. System sender bypasses allowlist gate even on subnet path.
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
# Fixtures (mirrors test_subnet_manager_manifest.py)
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
    dispatcher = MagicMock(spec=ManifestDispatcher)
    dispatcher.dispatch = AsyncMock(
        return_value=ManifestEntry(
            mid="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            sender_id="alice",
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


def _allowlist_service(members: dict[str, set[str]]):
    svc = MagicMock()

    async def _check(owner_id: str, target_id: str) -> bool:
        return target_id in members.get(owner_id, set())

    svc.is_member = _check
    return svc


def _exploding_allowlist_service():
    svc = MagicMock()

    async def _check(*_a, **_kw):
        raise RuntimeError("redis blip")

    svc.is_member = _check
    return svc


def _build_manager(
    *,
    agent_service: AsyncMock,
    redis_client: AsyncMock,
    policy_service: PolicyCheckService | None,
    manifest_dispatcher: MagicMock | None = None,
    allowlist_service=None,
    slug: str = "public",
    agent_id: str = "agent-b",
) -> tuple[SubnetManager, MagicMock]:
    manager = SubnetManager(
        agent_service=agent_service,
        redis_client=redis_client,
        policy_service=policy_service,
        manifest_dispatcher=manifest_dispatcher,
        allowlist_service=allowlist_service,
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


_PAYLOAD = {
    "role": "user",
    "message_id": "msg-1",
    "parts": [{"kind": "text", "text": "hi"}],
}


# ---------------------------------------------------------------------------
# Allowlist non-member: subnet divert (P0-4 core scenario)
# ---------------------------------------------------------------------------


class TestAllowlistNonMemberDiverts:
    """Behaviour parity with HTTP path — non-member on a subnet
    recipient must divert via the manifest queue, not via direct
    WebSocket push."""

    async def test_no_websocket_frame_on_non_member(
        self, mock_agent_service, fake_redis, policy_service, stub_dispatcher
    ):
        mock_agent_service.find_agent = AsyncMock(
            return_value=_make_agent_info({"mode": "allowlist"})
        )
        manager, websocket = _build_manager(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
            allowlist_service=_allowlist_service({"agent-b": {"alice"}}),
        )

        result = await manager.forward_request(
            "public", "agent-b", _PAYLOAD, from_agent="stranger"
        )

        websocket.send_json.assert_not_awaited()
        assert result["status"] == "sent"
        assert result["delivery_mode"] == "manifest"

    async def test_dispatcher_called_with_subnet_path(
        self, mock_agent_service, fake_redis, policy_service, stub_dispatcher
    ):
        """Metric label parity — same ``path="subnet"`` shape as
        manifest mode. The metric does not differentiate "diverted
        because non-allowlisted" vs "diverted because manifest-mode";
        intentional, see PR #2 plan B2 decision."""
        mock_agent_service.find_agent = AsyncMock(
            return_value=_make_agent_info({"mode": "allowlist"})
        )
        manager, _ = _build_manager(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
            allowlist_service=_allowlist_service({"agent-b": {"alice"}}),
        )

        await manager.forward_request(
            "public", "agent-b", _PAYLOAD, from_agent="stranger"
        )

        stub_dispatcher.dispatch.assert_awaited_once()
        kwargs = stub_dispatcher.dispatch.await_args.kwargs
        assert kwargs["owner_id"] == "agent-b"
        assert kwargs["sender_id"] == "stranger"
        assert kwargs["path"] == "subnet"


# ---------------------------------------------------------------------------
# Allowlist member: WebSocket push works
# ---------------------------------------------------------------------------


async def test_allowlist_member_uses_websocket(
    mock_agent_service, fake_redis, policy_service, stub_dispatcher, monkeypatch
):
    """Members get the fast path (subnet WebSocket push). Without
    this regression test a future refactor could accidentally
    divert ALL allowlist-mode traffic to the manifest queue —
    silently breaking the entire mode's value proposition.

    The real ``forward_request`` after sending the WebSocket frame
    awaits ``asyncio.wait_for`` on a future the agent will resolve
    via the WebSocket reply. There's no agent connected here — we
    short-circuit ``asyncio.wait_for`` so the test asserts on the
    routing decision (member → WebSocket push) without standing
    up the full reply round-trip.
    """
    mock_agent_service.find_agent = AsyncMock(
        return_value=_make_agent_info({"mode": "allowlist"})
    )
    manager, websocket = _build_manager(
        agent_service=mock_agent_service,
        redis_client=fake_redis,
        policy_service=policy_service,
        manifest_dispatcher=stub_dispatcher,
        allowlist_service=_allowlist_service({"agent-b": {"alice"}}),
    )

    import acn.infrastructure.messaging.subnet_manager as sm_module

    async def _instant_wait(_future, timeout):  # noqa: ARG001
        return {"status": "ok"}

    monkeypatch.setattr(sm_module.asyncio, "wait_for", _instant_wait)

    result = await manager.forward_request(
        "public", "agent-b", _PAYLOAD, from_agent="alice"
    )

    websocket.send_json.assert_awaited_once()
    stub_dispatcher.dispatch.assert_not_awaited()
    assert result == {"status": "ok"}


# ---------------------------------------------------------------------------
# Empty allowlist / missing service / IO failure: P0-3 fail-closed
# ---------------------------------------------------------------------------


class TestAllowlistFailClosedOnSubnet:
    """The fail-closed direction must hold on the subnet path the
    same way it does on the HTTP path. Otherwise an attacker could
    pick the ingress channel that fails open."""

    async def test_empty_allowlist_diverts(
        self, mock_agent_service, fake_redis, policy_service, stub_dispatcher
    ):
        mock_agent_service.find_agent = AsyncMock(
            return_value=_make_agent_info({"mode": "allowlist"})
        )
        manager, websocket = _build_manager(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
            allowlist_service=_allowlist_service({"agent-b": set()}),
        )

        result = await manager.forward_request(
            "public", "agent-b", _PAYLOAD, from_agent="alice"
        )

        websocket.send_json.assert_not_awaited()
        assert result["delivery_mode"] == "manifest"

    async def test_missing_allowlist_service_diverts(
        self, mock_agent_service, fake_redis, policy_service, stub_dispatcher
    ):
        mock_agent_service.find_agent = AsyncMock(
            return_value=_make_agent_info({"mode": "allowlist"})
        )
        manager, websocket = _build_manager(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
            allowlist_service=None,  # opt-out path
        )

        result = await manager.forward_request(
            "public", "agent-b", _PAYLOAD, from_agent="alice"
        )

        websocket.send_json.assert_not_awaited()
        assert result["delivery_mode"] == "manifest"

    async def test_callback_failure_diverts(
        self, mock_agent_service, fake_redis, policy_service, stub_dispatcher
    ):
        mock_agent_service.find_agent = AsyncMock(
            return_value=_make_agent_info({"mode": "allowlist"})
        )
        manager, websocket = _build_manager(
            agent_service=mock_agent_service,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
            allowlist_service=_exploding_allowlist_service(),
        )

        result = await manager.forward_request(
            "public", "agent-b", _PAYLOAD, from_agent="alice"
        )

        websocket.send_json.assert_not_awaited()
        assert result["delivery_mode"] == "manifest"


# ---------------------------------------------------------------------------
# System sender bypass under allowlist mode
# ---------------------------------------------------------------------------


async def test_system_sender_bypasses_allowlist_on_subnet(
    mock_agent_service, fake_redis, policy_service, stub_dispatcher, monkeypatch
):
    """System namespace bypasses every gate uniformly across
    ingress channels — same property pinned for HTTP path, must
    also hold on subnet."""
    mock_agent_service.find_agent = AsyncMock(
        return_value=_make_agent_info({"mode": "allowlist"})
    )
    manager, websocket = _build_manager(
        agent_service=mock_agent_service,
        redis_client=fake_redis,
        policy_service=policy_service,
        manifest_dispatcher=stub_dispatcher,
        allowlist_service=_allowlist_service({"agent-b": set()}),
    )

    import acn.infrastructure.messaging.subnet_manager as sm_module

    async def _instant_wait(_future, timeout):  # noqa: ARG001
        return {"status": "ok"}

    monkeypatch.setattr(sm_module.asyncio, "wait_for", _instant_wait)

    result = await manager.forward_request(
        "public", "agent-b", _PAYLOAD, from_agent="system:notifier"
    )

    websocket.send_json.assert_awaited_once()
    stub_dispatcher.dispatch.assert_not_awaited()
    assert result == {"status": "ok"}
