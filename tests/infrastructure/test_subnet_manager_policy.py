"""Unit tests for SubnetManager ↔ PolicyCheckService integration.

Covers Step 2.3 of the communication-policy rollout.

Why this layer needs its own gate (separate from MessageRouter):
``SubnetManager.forward_request`` pushes A2A requests directly over a
subnet agent's WebSocket. It bypasses ``MessageRouter`` entirely, so a
policy gate installed only at the router would leave subnet-attached
agents unprotected against ``communication_policy=closed``. The Phase 1
"一刀切" decision (subnet path also passes through policy) is what
these tests pin.

The gate's contract here:

- Reject with ``PolicyRejected`` *before* the WebSocket send_json fires
  — the recipient never sees a rejected request.
- Re-fetch the canonical policy from the registry on every forward, so
  owner edits to ``communication_policy`` take effect immediately
  without requiring a WebSocket reconnect.
- Fall through to ``open`` semantics when the registry lookup fails,
  so a transient Redis flake does not manufacture an outage on top of
  whatever caused the original flake.
- ``policy_service=None`` opt-out preserves pre-Phase-1 behaviour for
  legacy fixtures and the ``api.py`` wiring transition.

See docs/features/acn-communication-economic-model.md
"Phase 1 网关执行点决策".
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.exceptions import PolicyRejected
from acn.infrastructure.messaging.subnet_manager import (
    GatewayConnection,
    SubnetManager,
)
from acn.services.policy_service import PolicyCheckService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def policy_service() -> PolicyCheckService:
    """Real service — pure logic, no benefit to mocking."""
    return PolicyCheckService()


@pytest.fixture
def fake_redis() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_registry() -> MagicMock:
    """Registry is the lookup the policy gate fans out to. Tests
    override ``get_agent`` per-case."""
    return MagicMock()


def _make_agent_info(communication_policy: dict | None = None) -> MagicMock:
    """Minimal AgentInfo-shaped mock (only the field policy reads)."""
    info = MagicMock()
    info.communication_policy = communication_policy
    return info


def _build_manager_with_connected_agent(
    *,
    registry: MagicMock,
    redis_client: AsyncMock,
    policy_service: PolicyCheckService | None,
    subnet_id: str = "public",
    agent_id: str = "agent-b",
) -> tuple[SubnetManager, MagicMock]:
    """Construct a SubnetManager with one fake connection installed.

    Returns the manager and the mock WebSocket so tests can assert on
    ``send_json`` (specifically: that it was NOT awaited on policy-reject
    paths, which is the central contract of this layer).
    """
    manager = SubnetManager(
        registry=registry,
        redis_client=redis_client,
        policy_service=policy_service,
    )
    websocket = MagicMock()
    websocket.send_json = AsyncMock()
    connection = GatewayConnection(
        connection_id="conn-1",
        subnet_id=subnet_id,
        agent_id=agent_id,
        websocket=websocket,
    )
    manager._subnets[subnet_id].connections[agent_id] = connection
    return manager, websocket


# ---------------------------------------------------------------------------
# 1. Closed recipient → PolicyRejected, no WebSocket send
# ---------------------------------------------------------------------------


class TestClosedRecipientShortCircuits:
    @pytest.mark.asyncio
    async def test_raises_policy_rejected(
        self, mock_registry, fake_redis, policy_service
    ):
        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info(
                {"mode": "closed", "reject_reason": "do not disturb"}
            )
        )
        manager, _ws = _build_manager_with_connected_agent(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
        )

        with pytest.raises(PolicyRejected) as exc_info:
            await manager.forward_request(
                "public",
                "agent-b",
                {"role": "user", "parts": []},
                from_agent="agent-a",
            )

        assert exc_info.value.reason == "policy_closed"
        assert exc_info.value.reject_reason == "do not disturb"
        assert exc_info.value.recipient_id == "agent-b"

    @pytest.mark.asyncio
    async def test_does_not_send_websocket_frame(
        self, mock_registry, fake_redis, policy_service
    ):
        """The single most important assertion at this layer: a closed
        recipient never observes a rejected request — there must be
        ZERO ``send_json`` calls when policy denies the forward."""
        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info({"mode": "closed"})
        )
        manager, websocket = _build_manager_with_connected_agent(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
        )

        with pytest.raises(PolicyRejected):
            await manager.forward_request(
                "public",
                "agent-b",
                {"role": "user"},
                from_agent="agent-a",
            )

        websocket.send_json.assert_not_awaited()


# ---------------------------------------------------------------------------
# 2. System sender exemption survives at the subnet boundary
# ---------------------------------------------------------------------------


class TestSystemSenderExemption:
    @pytest.mark.asyncio
    async def test_system_sender_bypasses_closed_recipient(
        self, mock_registry, fake_redis, policy_service
    ):
        """``system:*`` sender must reach the WebSocket layer even when
        recipient is closed. The forward will time out (no responder
        wires up the future in this unit test), but the policy gate
        itself must NOT fire — that's what we assert here by checking
        that ``send_json`` did go through."""
        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info({"mode": "closed"})
        )
        manager, websocket = _build_manager_with_connected_agent(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
        )

        # No responder will resolve the future — use a tiny timeout so
        # the test fails fast in TimeoutError if the gate would have
        # rejected (no send_json).
        with pytest.raises(TimeoutError):
            await manager.forward_request(
                "public",
                "agent-b",
                {"role": "user"},
                timeout=0.05,
                from_agent="system:notifier",
            )

        # The exemption is observable as: the request *was* sent over
        # the wire even though the policy is ``closed``.
        websocket.send_json.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. Open recipient: policy installed but transparent
# ---------------------------------------------------------------------------


class TestOpenRecipientUnaffected:
    @pytest.mark.asyncio
    async def test_open_policy_lets_request_through(
        self, mock_registry, fake_redis, policy_service
    ):
        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info({"mode": "open"})
        )
        manager, websocket = _build_manager_with_connected_agent(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
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


# ---------------------------------------------------------------------------
# 4. policy_service=None opt-out (rollout safety net)
# ---------------------------------------------------------------------------


class TestPolicyServiceOptional:
    @pytest.mark.asyncio
    async def test_no_policy_service_skips_gate(
        self, mock_registry, fake_redis
    ):
        """Pinning the rollout opt-out: a SubnetManager built without a
        policy service must behave exactly as before — even a closed
        recipient gets the WebSocket frame. Loss of this guarantee
        forces every test fixture and the api.py wiring to flip in the
        same PR."""
        # Even closed must NOT short-circuit when the service is absent.
        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info({"mode": "closed"})
        )
        manager, websocket = _build_manager_with_connected_agent(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=None,
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
        # And critically: when the gate is uninstalled, the registry is
        # not consulted either — important for legacy fixtures that
        # don't set up ``registry.get_agent`` at all.
        mock_registry.get_agent.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Registry lookup failure falls through to "open" (availability over safety)
# ---------------------------------------------------------------------------


class TestRegistryLookupResilience:
    @pytest.mark.asyncio
    async def test_registry_lookup_exception_falls_through_to_open(
        self, mock_registry, fake_redis, policy_service
    ):
        """Pinning the explicit availability decision: when the registry
        read raises (Redis flake, transient timeout), we treat the
        recipient as ``open`` and let the request proceed. The
        WebSocket connection itself proves the agent's presence; failing
        closed here would manufacture an outage on top of whatever
        caused the registry flake."""
        mock_registry.get_agent = AsyncMock(side_effect=ConnectionError("redis flake"))
        manager, websocket = _build_manager_with_connected_agent(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
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

    @pytest.mark.asyncio
    async def test_registry_returns_none_falls_through_to_open(
        self, mock_registry, fake_redis, policy_service
    ):
        """Edge case: the registry no longer has this agent (e.g. it
        was unregistered between WebSocket connect and forward). The
        connection cache still has a slot; the policy gate must not
        crash the forward — fall through to ``open`` for the same
        availability reason as the exception case."""
        mock_registry.get_agent = AsyncMock(return_value=None)
        manager, websocket = _build_manager_with_connected_agent(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
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


# ---------------------------------------------------------------------------
# 6. from_agent default behaviour
# ---------------------------------------------------------------------------


class TestFromAgentDefault:
    @pytest.mark.asyncio
    async def test_unknown_sender_treated_as_non_system(
        self, mock_registry, fake_redis, policy_service
    ):
        """When the caller doesn't supply ``from_agent`` (legacy code
        path), the gate must NOT silently treat them as system-exempt.
        Closed policy still wins."""
        mock_registry.get_agent = AsyncMock(
            return_value=_make_agent_info({"mode": "closed"})
        )
        manager, websocket = _build_manager_with_connected_agent(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
        )

        # No ``from_agent`` keyword — defaults to None inside the
        # manager, which the gate maps to "unknown" (non-system).
        with pytest.raises(PolicyRejected):
            await manager.forward_request(
                "public",
                "agent-b",
                {"role": "user"},
            )

        websocket.send_json.assert_not_awaited()


# ---------------------------------------------------------------------------
# 7. Subnet/agent existence checks still fire before policy (cheap-first)
# ---------------------------------------------------------------------------


class TestPreconditionsFireBeforePolicyCheck:
    """The two ``ValueError`` preconditions (unknown subnet, agent not
    connected) are pure dict lookups — much cheaper than a registry
    round-trip. Pinning their position so a future refactor doesn't
    silently make policy lookups happen for non-existent recipients
    (which would also produce confusing audit lines)."""

    @pytest.mark.asyncio
    async def test_unknown_subnet_raises_value_error_without_policy_lookup(
        self, mock_registry, fake_redis, policy_service
    ):
        mock_registry.get_agent = AsyncMock()
        manager = SubnetManager(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
        )

        with pytest.raises(ValueError, match="Subnet not found"):
            await manager.forward_request(
                "no-such-subnet",
                "agent-b",
                {"role": "user"},
                from_agent="agent-a",
            )

        mock_registry.get_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_not_connected_raises_value_error_without_policy_lookup(
        self, mock_registry, fake_redis, policy_service
    ):
        mock_registry.get_agent = AsyncMock()
        manager = SubnetManager(
            registry=mock_registry,
            redis_client=fake_redis,
            policy_service=policy_service,
        )

        # 'public' subnet exists by default; agent-b is not connected.
        with pytest.raises(ValueError, match="Agent not connected"):
            await manager.forward_request(
                "public",
                "agent-b",
                {"role": "user"},
                from_agent="agent-a",
            )

        mock_registry.get_agent.assert_not_called()
