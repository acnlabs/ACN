"""Unit tests — MessageRouter ↔ AllowlistService integration (Phase 2 PR #2).

Sister suite to ``test_message_router_manifest.py``: where that file
pinned the manifest divert path, this one pins the allowlist divert
branch added in PR #2.

The router does not implement allowlist semantics itself — that's
the policy service's job. What the router DOES do is:

* Thread ``AllowlistService.is_member`` into ``check_inbound`` as
  the ``is_in_allowlist`` callback.
* React to ``decision.route_to`` exactly the same way for
  ``allowlist`` non-members as it does for ``manifest`` mode
  (divert via dispatcher; no inbox / DLQ / HTTP).
* Route allowlist MEMBERS through the inbox/HTTP path, exactly
  like ``open`` mode.

These tests pin both halves so a future refactor can't drop the
allowlist callback (silent fail-closed-to-manifest for everyone)
or accidentally divert allowlist members.
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
# Fixtures (shape mirrors test_message_router_manifest.py)
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
def mock_registry() -> MagicMock:
    return MagicMock()


@pytest.fixture
def stub_dispatcher() -> MagicMock:
    dispatcher = MagicMock(spec=ManifestDispatcher)
    dispatcher.dispatch = AsyncMock(
        return_value=ManifestEntry(
            mid="abc",
            sender_id="alice",
            summary="hi",
            ts_ms=1714377600000,
            content_size=4,
        )
    )
    return dispatcher


def _allowlist_service(members: dict[str, set[str]]):
    """Build a stub ``AllowlistService`` whose ``is_member`` resolves
    against ``members``. Other methods are async stubs in case the
    router accidentally calls them — they would fail loudly if
    invoked because we'd see the ``return None`` propagate as
    ``False`` and the test would mis-route.
    """
    svc = MagicMock()

    async def _check(owner_id: str, target_id: str) -> bool:
        return target_id in members.get(owner_id, set())

    svc.is_member = _check
    return svc


def _make_agent_info(*, status="online", endpoint="http://b:8000", policy=None):
    info = MagicMock()
    info.status = status
    info.endpoint = endpoint
    info.communication_policy = policy
    return info


# ---------------------------------------------------------------------------
# Allowlist member: routed to inbox (HTTP path)
# ---------------------------------------------------------------------------


class TestAllowlistMember:
    """Members on the recipient's allowlist must reach the inbox /
    HTTP path exactly like ``open`` mode. The fact that the
    recipient OPTED IN to allowlist mode is precisely so that
    trusted senders get the fast path."""

    async def test_member_does_not_trigger_dispatcher(
        self, mock_registry, fake_redis, policy_service, stub_dispatcher
    ):
        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info(
                status="offline",  # falls into inbox path; HTTP is mocked anyway
                policy={"mode": "allowlist"},
            )
        )
        allowlist_service = _allowlist_service({"agent-b": {"alice"}})
        router = MessageRouter(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
            allowlist_service=allowlist_service,
        )

        result = await router.route(
            from_agent="alice",
            to_agent="agent-b",
            message=create_text_message("hi"),
        )

        # Inbox path response shape — NOT manifest.
        assert result["delivery_mode"] == "inbox"
        stub_dispatcher.dispatch.assert_not_awaited()


# ---------------------------------------------------------------------------
# Allowlist non-member: diverted to manifest
# ---------------------------------------------------------------------------


class TestAllowlistNonMember:
    """Non-members must take the SAME divert path as manifest mode.
    ``status="sent"`` + ``delivery_mode="manifest"`` is the
    public-facing contract; the underlying ``ManifestDispatcher``
    is invoked with ``path="router"`` so the metric label tracks
    the ingress channel (not the policy mode that triggered the
    divert)."""

    async def test_non_member_routes_to_dispatcher(
        self, mock_registry, fake_redis, policy_service, stub_dispatcher
    ):
        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info(policy={"mode": "allowlist"})
        )
        allowlist_service = _allowlist_service({"agent-b": {"alice"}})
        router = MessageRouter(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
            allowlist_service=allowlist_service,
        )

        result = await router.route(
            from_agent="stranger",
            to_agent="agent-b",
            message=create_text_message("hi"),
        )

        assert result["status"] == "sent"
        assert result["delivery_mode"] == "manifest"
        stub_dispatcher.dispatch.assert_awaited_once()
        kwargs = stub_dispatcher.dispatch.await_args.kwargs
        assert kwargs["owner_id"] == "agent-b"
        assert kwargs["sender_id"] == "stranger"
        assert kwargs["path"] == "router"

    async def test_non_member_does_not_open_http(
        self, mock_registry, fake_redis, policy_service, stub_dispatcher
    ):
        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info(policy={"mode": "allowlist"})
        )
        allowlist_service = _allowlist_service({"agent-b": {"alice"}})
        router = MessageRouter(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
            allowlist_service=allowlist_service,
        )
        router._get_client = AsyncMock()

        await router.route(
            from_agent="stranger",
            to_agent="agent-b",
            message=create_text_message("hi"),
        )

        router._get_client.assert_not_awaited()


# ---------------------------------------------------------------------------
# Empty allowlist + missing service: P0-3 fail-closed to manifest
# ---------------------------------------------------------------------------


class TestAllowlistFailClosed:
    """The fail-closed direction: when the allowlist callback can't
    answer authoritatively (service not wired, IO failure, empty
    list), the router must DIVERT, not raise / not open."""

    async def test_empty_list_diverts_everyone_to_manifest(
        self, mock_registry, fake_redis, policy_service, stub_dispatcher
    ):
        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info(policy={"mode": "allowlist"})
        )
        allowlist_service = _allowlist_service({"agent-b": set()})
        router = MessageRouter(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
            allowlist_service=allowlist_service,
        )

        result = await router.route(
            from_agent="alice",
            to_agent="agent-b",
            message=create_text_message("hi"),
        )

        assert result["delivery_mode"] == "manifest"
        stub_dispatcher.dispatch.assert_awaited_once()

    async def test_missing_allowlist_service_diverts_to_manifest(
        self, mock_registry, fake_redis, policy_service, stub_dispatcher
    ):
        """Rollout-opt-out path: ``allowlist_service=None`` + recipient
        flipped to ``mode=allowlist``. PolicyCheckService's "missing
        callback → divert to manifest" branch must engage so the
        router does NOT crash."""
        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info(policy={"mode": "allowlist"})
        )
        router = MessageRouter(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
            allowlist_service=None,
        )

        result = await router.route(
            from_agent="alice",
            to_agent="agent-b",
            message=create_text_message("hi"),
        )

        assert result["delivery_mode"] == "manifest"
        stub_dispatcher.dispatch.assert_awaited_once()

    async def test_callback_failure_diverts_to_manifest(
        self, mock_registry, fake_redis, policy_service, stub_dispatcher
    ):
        """Redis / PG outage on the cache layer must NOT propagate as
        a 5xx — divert to manifest preserves the message."""
        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info(policy={"mode": "allowlist"})
        )
        broken = MagicMock()

        async def _explode(*_a, **_kw):
            raise RuntimeError("redis blip")

        broken.is_member = _explode

        router = MessageRouter(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
            manifest_dispatcher=stub_dispatcher,
            allowlist_service=broken,
        )

        result = await router.route(
            from_agent="alice",
            to_agent="agent-b",
            message=create_text_message("hi"),
        )

        assert result["delivery_mode"] == "manifest"
        stub_dispatcher.dispatch.assert_awaited_once()


# ---------------------------------------------------------------------------
# System sender bypass under allowlist mode
# ---------------------------------------------------------------------------


async def test_system_sender_bypasses_allowlist(
    mock_registry, fake_redis, policy_service, stub_dispatcher
):
    """Same uniformity property as manifest mode: system traffic
    never gets diverted regardless of the recipient's policy. A
    user-facing chat-mention notification must reach the inbox even
    if the recipient is in strict allowlist mode."""
    mock_registry.get_agent = AsyncMock(
        return_value=_make_agent_info(
            status="offline",
            policy={"mode": "allowlist"},
        )
    )
    allowlist_service = _allowlist_service({"agent-b": set()})
    router = MessageRouter(
        registry=mock_registry,
        redis_client=fake_redis,
        policy_service=policy_service,
        manifest_dispatcher=stub_dispatcher,
        allowlist_service=allowlist_service,
    )

    result = await router.route(
        from_agent="system:chat",
        to_agent="agent-b",
        message=create_text_message("you were mentioned"),
    )

    # System bypass → inbox path, not manifest.
    assert result["delivery_mode"] == "inbox"
    stub_dispatcher.dispatch.assert_not_awaited()
