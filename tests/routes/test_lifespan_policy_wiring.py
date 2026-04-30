"""Wiring regression for Step 2.7 of the communication-policy rollout.

The Phase 1 gateway requires that **the same** ``PolicyCheckService``
instance be threaded into both ``MessageRouter`` (HTTP / DLQ /
broadcast paths) and ``SubnetManager`` (WebSocket subnet paths) when
the application boots. The two paths must share an instance because:

1. **Drift safety** — a future caller mutating policy state on one
   instance must not leave the other path applying stale rules.
   Phase 1 has no such mutators, but the invariant is cheap to pin
   now and expensive to recover later.
2. **Single source of truth** — operators reasoning about "did the
   gate let this through" should not need to ask "which gate?".

These tests fail fast at boot if a refactor accidentally produces
two ``PolicyCheckService()`` calls or forgets the kwarg on either
collaborator.

v2 review R2 also pins the **anti-misconfiguration** assert: if a
future change forgets to thread ``policy_service=`` into
``init_services`` (silently dropping it via default-None), startup
must crash loudly rather than fail-open on the proxy / A2A paths.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from acn import api as api_module
from acn.routes import dependencies as deps_module
from tests.routes.test_lifespan_teardown import _enter_common_patches


@pytest.mark.asyncio
async def test_lifespan_constructs_policy_service_exactly_once():
    """Two instances would let policy state drift between the HTTP
    and WebSocket gateways. Pinning ``call_count == 1`` so a future
    refactor that "helpfully" instantiates a separate one for the
    subnet path fails loudly here."""
    ws_stub = AsyncMock()
    webhook_stub = AsyncMock()

    with ExitStack() as stack:
        _enter_common_patches(stack, ws_stub, webhook_stub)
        stack.enter_context(
            patch.object(
                api_module, "PolicyCheckService", return_value=MagicMock()
            )
        )

        async with api_module.lifespan(api_module.app):
            assert api_module.PolicyCheckService.call_count == 1, (
                "PolicyCheckService must be instantiated exactly once "
                "and shared across MessageRouter + SubnetManager — see "
                "Step 2.7 in docs/features/acn-communication-economic-model.md"
            )


@pytest.mark.asyncio
async def test_lifespan_threads_same_policy_into_router_and_subnet_manager():
    """The router and the subnet manager must receive the SAME
    instance — not two equal copies. We assert identity (``is``)
    rather than equality so a future refactor that calls
    ``PolicyCheckService()`` twice and produces two structurally-
    equal-but-distinct objects still fails this test."""
    ws_stub = AsyncMock()
    webhook_stub = AsyncMock()

    sentinel_policy = MagicMock(name="policy-service-sentinel")

    with ExitStack() as stack:
        _enter_common_patches(stack, ws_stub, webhook_stub)
        stack.enter_context(
            patch.object(
                api_module, "PolicyCheckService", return_value=sentinel_policy
            )
        )

        async with api_module.lifespan(api_module.app):
            router_call = api_module.MessageRouter.call_args
            subnet_call = api_module.SubnetManager.call_args

            assert router_call is not None, (
                "MessageRouter was never constructed during lifespan — "
                "patching layer drifted out of sync with api.py"
            )
            assert subnet_call is not None, (
                "SubnetManager was never constructed during lifespan — "
                "patching layer drifted out of sync with api.py"
            )

            assert router_call.kwargs.get("policy_service") is sentinel_policy, (
                "MessageRouter must receive the shared PolicyCheckService "
                f"via kwargs; got kwargs={router_call.kwargs}"
            )
            assert subnet_call.kwargs.get("policy_service") is sentinel_policy, (
                "SubnetManager must receive the shared PolicyCheckService "
                f"via kwargs; got kwargs={subnet_call.kwargs}"
            )


@pytest.mark.asyncio
async def test_lifespan_router_receives_policy_via_keyword_argument():
    """Defensive contract: the wiring must use the ``policy_service``
    keyword, not positional args. A positional pass would still work
    today but couples the wiring to ``MessageRouter.__init__``'s
    parameter order — any future ctor reorder would silently swap
    the policy with another optional collaborator."""
    ws_stub = AsyncMock()
    webhook_stub = AsyncMock()

    with ExitStack() as stack:
        _enter_common_patches(stack, ws_stub, webhook_stub)
        stack.enter_context(
            patch.object(
                api_module, "PolicyCheckService", return_value=MagicMock()
            )
        )

        async with api_module.lifespan(api_module.app):
            router_call = api_module.MessageRouter.call_args
            subnet_call = api_module.SubnetManager.call_args

        assert "policy_service" in router_call.kwargs, (
            "MessageRouter ctor wiring must pass policy_service as a "
            "keyword argument, not positionally"
        )
        assert "policy_service" in subnet_call.kwargs, (
            "SubnetManager ctor wiring must pass policy_service as a "
            "keyword argument, not positionally"
        )


# --------------------------------------------------------------------------- #
# v2 review R2: anti-misconfiguration assert
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_lifespan_asserts_policy_service_was_wired():
    """Pre-fix (v2 review R2):
        ``_proxy_to_agent`` and the A2A handlers all degrade to "no
        gate" when ``policy_service is None`` — a deliberate
        rollout-safety contract for unit tests / partial bring-ups.
        The flip side is that a misconfigured production lifespan
        (e.g. a future refactor that drops the
        ``policy_service=policy_service_instance`` line in
        ``dependencies.init_services(...)``) would silently
        fail-open: closed agents would start receiving traffic
        they explicitly opted out of, with zero startup signal.

    The guard in ``acn/api.py`` turns that silent failure into a
    noisy startup crash. We simulate the misconfiguration by
    making ``init_services`` swallow the ``policy_service`` kwarg
    so ``get_policy_service()`` returns ``None`` afterwards.
    Lifespan must then raise RuntimeError before yielding.

    Why RuntimeError specifically (v3 review R6):
        We deliberately do *not* use ``assert`` here. ``assert``
        bytecode is stripped under ``python -O`` /
        ``PYTHONOPTIMIZE=1`` — a reasonable production performance
        toggle that would silently re-introduce the fail-open this
        guard exists to prevent. ``raise RuntimeError`` is
        unconditional and survives -O. Pinning the exception type
        here so a future refactor that "helpfully" swaps it back
        to ``assert`` fails this test loudly.
    """
    ws_stub = AsyncMock()
    webhook_stub = AsyncMock()

    real_init_services = deps_module.init_services

    def init_services_dropping_policy(*args, **kwargs):
        # Strip policy_service to simulate the misconfiguration.
        kwargs.pop("policy_service", None)
        return real_init_services(*args, **kwargs)

    with ExitStack() as stack:
        _enter_common_patches(stack, ws_stub, webhook_stub)
        stack.enter_context(
            patch.object(
                api_module.dependencies,
                "init_services",
                side_effect=init_services_dropping_policy,
            )
        )

        with pytest.raises(RuntimeError, match="PolicyCheckService is not wired"):
            async with api_module.lifespan(api_module.app):
                pytest.fail(
                    "lifespan must crash before yielding when "
                    "policy_service is missing — proxy / A2A would "
                    "otherwise silently fail-open"
                )
