"""Regression tests for P2-#5: lifespan must pre-warm ERC-8004 chain_id.

Three branches matter:

1. **Match** — RPC reports the configured chain_id; lifespan logs success
   and hands the warmed client to the route singleton (so subsequent bind
   requests reuse the cached chain_id and skip a redundant RPC roundtrip).

2. **Mismatch** — RPC reports a different chain_id (config disaster:
   wrong RPC URL or wrong env). Lifespan must refuse to start so a
   misconfigured deploy fails loudly instead of silently producing
   ``503 Service Unavailable`` on the first bind.

3. **Unreachable** — RPC errored / timed out. Treated as transient
   operability concern, not a config bug, so lifespan continues; the
   per-bind verify check stays the safety net.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from acn import api as api_module
from acn.routes import onchain as onchain_module
from tests.routes.test_lifespan_teardown import _enter_common_patches


def _patch_lifespan_with_erc(stack: ExitStack, verify_return) -> MagicMock:
    """Apply common lifespan patches + a stub ``ERC8004Client``.

    ``verify_return`` is the value (or exception) ``verify_chain_id``
    should yield.  Returns the stub client instance for assertions.
    """
    ws_stub = AsyncMock()
    webhook_stub = AsyncMock()
    _enter_common_patches(stack, ws_stub, webhook_stub)

    erc_stub = MagicMock()
    if isinstance(verify_return, Exception):
        erc_stub.verify_chain_id = AsyncMock(side_effect=verify_return)
    else:
        erc_stub.verify_chain_id = AsyncMock(return_value=verify_return)

    stack.enter_context(patch.object(api_module, "ERC8004Client", return_value=erc_stub))
    # Force the verify path on (it's the default, but keep test independent of env).
    stack.enter_context(patch.object(api_module.settings, "erc8004_enabled", True))
    stack.enter_context(patch.object(api_module.settings, "erc8004_chain_id", 8453))
    # Reset the route-level singleton between tests.
    onchain_module.set_erc8004_client(None)
    return erc_stub


@pytest.mark.asyncio
async def test_startup_verify_match_logs_and_injects_client():
    """Happy path: verify_chain_id returns ``(True, expected)`` -> boot continues
    and the warmed client is handed to the route singleton."""
    with ExitStack() as stack:
        erc_stub = _patch_lifespan_with_erc(stack, (True, 8453))

        async with api_module.lifespan(api_module.app):
            erc_stub.verify_chain_id.assert_awaited_once_with(8453)
            # The lifespan must install the warmed instance, not lazy-init a fresh one.
            assert onchain_module._erc8004_client is erc_stub, (
                "warmed client must be reused by the route layer to preserve "
                "the chain_id cache"
            )

    # Cleanup: clear the singleton so we don't leak it into other tests.
    onchain_module.set_erc8004_client(None)


@pytest.mark.asyncio
async def test_startup_verify_mismatch_aborts_boot():
    """Config disaster: RPC reports the wrong chain_id -> lifespan must raise."""
    with ExitStack() as stack:
        _patch_lifespan_with_erc(stack, (False, 1))  # RPC reports Ethereum mainnet

        with pytest.raises(RuntimeError, match=r"chain_id=1 .*expects 8453"):
            async with api_module.lifespan(api_module.app):
                pass  # pragma: no cover — startup must abort before yield


@pytest.mark.asyncio
async def test_startup_verify_unreachable_continues_with_warning():
    """RPC unreachable (``(False, None)``) -> proceed with a warning log.

    Treated as a transient operability problem (RPC blip on rolling
    restart should not take the whole cluster down). The per-bind check
    inside the bind endpoint stays the safety net.
    """
    with ExitStack() as stack:
        erc_stub = _patch_lifespan_with_erc(stack, (False, None))

        async with api_module.lifespan(api_module.app):
            erc_stub.verify_chain_id.assert_awaited_once_with(8453)
            # Even on warning, the route singleton must still be set so
            # subsequent retries share state.
            assert onchain_module._erc8004_client is erc_stub

    onchain_module.set_erc8004_client(None)


@pytest.mark.asyncio
async def test_startup_verify_exception_treated_as_unreachable():
    """``verify_chain_id`` itself raising must not abort startup.

    Already-tolerated by the helper (it returns ``(False, None)`` for
    RPC errors), but the lifespan wrapper has its own ``except`` belt-
    and-braces in case an upstream library raises something the helper
    didn't anticipate (e.g. a TypeError during URL parsing).
    """
    with ExitStack() as stack:
        _patch_lifespan_with_erc(stack, RuntimeError("DNS resolution failed"))

        async with api_module.lifespan(api_module.app):
            assert onchain_module._erc8004_client is not None

    onchain_module.set_erc8004_client(None)


@pytest.mark.asyncio
async def test_startup_verify_skipped_when_disabled():
    """``ERC8004_ENABLED=false`` -> no chain_id RPC at all (clean off-chain mode)."""
    with ExitStack() as stack:
        ws_stub = AsyncMock()
        webhook_stub = AsyncMock()
        _enter_common_patches(stack, ws_stub, webhook_stub)
        erc_ctor = MagicMock()
        stack.enter_context(patch.object(api_module, "ERC8004Client", erc_ctor))
        stack.enter_context(patch.object(api_module.settings, "erc8004_enabled", False))
        onchain_module.set_erc8004_client(None)

        async with api_module.lifespan(api_module.app):
            erc_ctor.assert_not_called()
            assert onchain_module._erc8004_client is None

    onchain_module.set_erc8004_client(None)
