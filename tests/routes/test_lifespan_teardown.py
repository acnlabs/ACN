"""Regression tests for SCALE_AUDIT P2-1: lifespan was missing the
start/stop pairs for WebSocketManager and WebhookService.

Before the fix:
  - ws_manager.start() was never called; `_pubsub` stayed None; the
    Redis Pub/Sub listener never ran. Single-process WebSocket broadcast
    still worked via _broadcast_local(), but any deployment with more
    than one ACN instance silently dropped cross-node traffic.
  - webhook_service.start() was never called; first send() lazily spun
    up an httpx.AsyncClient, but shutdown never aclose()'d it, leaking
    the connection pool on every process restart.

The tests exercise lifespan directly and assert start() / stop() are
each awaited once in the right phase of the context manager. Every
other heavy dep (Redis, PG, httpx, A2A) is patched to an AsyncMock
because we're not testing them here.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from acn import api as api_module

# Names in `acn.api` whose construction we neutralize. Anything that
# opens a socket / file / child process lives in here.
_HEAVY_DEP_NAMES = (
    "MessageRouter",
    "BroadcastService",
    "SubnetManager",
    "MetricsCollector",
    "AuditLogger",
    "Analytics",
    "PaymentDiscoveryService",
    "PaymentTaskManager",
    "BillingService",
    "ActivityService",
    "AgentService",
    "SubnetService",
    "MessageService",
    "TaskPool",
    "TaskService",
    "RedisAgentRepository",
    "RedisSubnetRepository",
    "RedisTaskRepository",
)


def _enter_common_patches(stack: ExitStack, ws_stub, webhook_stub):
    """Apply the full set of patches needed to get `lifespan` through
    its body without touching real infrastructure.

    ExitStack keeps us under CPython's hard 20-level `with` nesting cap —
    we have ~25 patches to install.
    """

    def _closeable() -> AsyncMock:
        # MessageRouter.close() is awaited in teardown; make sure the
        # stub has an awaitable .close.
        m = AsyncMock()
        m.close = AsyncMock()
        return m

    specials = {
        "WebSocketManager": ws_stub,
        "WebhookService": webhook_stub,
        "MessageRouter": _closeable(),
    }

    for name, instance in specials.items():
        stack.enter_context(patch.object(api_module, name, return_value=instance))

    for name in _HEAVY_DEP_NAMES:
        if name in specials:
            continue
        stack.enter_context(patch.object(api_module, name, return_value=AsyncMock()))

    # ``redis_client`` replaces the legacy ``registry_instance.redis``;
    # ``aioredis.from_url`` is the constructor lifespan uses, so patch
    # it to return an AsyncMock whose ``aclose()`` is awaitable.
    stack.enter_context(
        patch.object(api_module.aioredis, "from_url", return_value=AsyncMock())
    )
    # Module-level helpers that would otherwise fan out further.
    stack.enter_context(patch.object(api_module, "create_a2a_app", return_value=AsyncMock()))
    stack.enter_context(
        patch.object(api_module, "create_webhook_config_from_settings", return_value=None)
    )
    # Neutralize escrow side-channel (hits HTTP on __init__).
    stack.enter_context(
        patch.object(api_module, "AgentPlanetEscrowProvider", return_value=AsyncMock())
    )
    # Force the Redis-fallback branch so we don't have to stand up PG.
    # (lifespan reads settings.database_url at the top; mutating settings
    # mid-test is cleaner than patching a dozen Postgres* classes.)
    stack.enter_context(patch.object(api_module.settings, "database_url", ""))


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_ws_and_webhook():
    """ws_manager.start/stop and webhook_service.start/stop must both
    bracket the yield. Without start(), cross-node ws broadcasts are
    dropped; without stop(), httpx clients leak on restart."""

    captured: list[str] = []

    ws_stub = AsyncMock()
    ws_stub.start.side_effect = lambda: captured.append("ws.start")
    ws_stub.stop.side_effect = lambda: captured.append("ws.stop")

    webhook_stub = AsyncMock()
    webhook_stub.start.side_effect = lambda: captured.append("webhook.start")
    webhook_stub.stop.side_effect = lambda: captured.append("webhook.stop")

    with ExitStack() as stack:
        _enter_common_patches(stack, ws_stub, webhook_stub)

        async with api_module.lifespan(api_module.app):
            # Mid-lifespan: start() has been called, stop() has not.
            ws_stub.start.assert_awaited_once()
            webhook_stub.start.assert_awaited_once()
            ws_stub.stop.assert_not_called()
            webhook_stub.stop.assert_not_called()

        # After exit: stop() has run for both.
        ws_stub.stop.assert_awaited_once()
        webhook_stub.stop.assert_awaited_once()

    # Phase ordering: all starts happen before any stop.
    starts = [i for i, c in enumerate(captured) if c.endswith(".start")]
    stops = [i for i, c in enumerate(captured) if c.endswith(".stop")]
    assert len(starts) == 2 and len(stops) == 2, captured
    assert max(starts) < min(stops), (
        f"start/stop phases must not interleave: {captured}"
    )


@pytest.mark.asyncio
async def test_lifespan_stop_failures_do_not_block_shutdown():
    """If ws_manager.stop() blows up, webhook still needs to be cleaned
    up. Lifespan must log and move on, not propagate the exception."""

    ws_stub = AsyncMock()
    ws_stub.stop.side_effect = RuntimeError("simulated ws teardown failure")

    webhook_stub = AsyncMock()

    with ExitStack() as stack:
        _enter_common_patches(stack, ws_stub, webhook_stub)

        async with api_module.lifespan(api_module.app):
            pass

    # ws.stop raised, but webhook.stop still ran.
    webhook_stub.stop.assert_awaited_once()
