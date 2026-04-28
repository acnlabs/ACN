"""Security audit H-erc8004: bind must not trust client-supplied ``chain``.

Threat model
------------
The pre-fix ``POST /onchain/agents/{id}/bind`` stored ``BindRequest.chain``
verbatim and served it back as ground truth via
``GET /onchain/agents/{id}``. An attacker who minted a token on a cheap
testnet (Base Sepolia, gas ~0.001 USD) could pass ``chain="eip155:1"``
and have ACN tell every downstream consumer the token lives on Ethereum
mainnet.

Two complementary defences are pinned here:
  1. ``body.chain``, when provided, must equal ``f"eip155:{chain_id}"``
     where ``chain_id`` is the server's own ``erc8004_chain_id``. Any
     divergence -> 422.
  2. The configured RPC endpoint must actually report the same chain_id
     that ACN expects. This catches the "operator swapped RPC URL" case
     and the "attacker controls the RPC node" case where ``tokenURI``
     responses can be forged. Mismatch / unreachable RPC -> 503
     (fail-closed by design — see ``ERC8004Client.verify_chain_id``).

The persisted ``agent.erc8004_chain`` is always the *server*-derived
value, never the client's. We assert that explicitly so a future
refactor that re-routes the client value through cannot regress
silently.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.routes.dependencies import get_agent_service, limiter, verify_agent_api_key
from acn.routes.onchain import get_erc8004_client


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    """Onchain bind isn't rate-limited today, but TestClient + slowapi
    can still trip on shared Redis state if other tests leave it primed.
    Belt-and-braces: turn the limiter off for this module."""
    was = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = was


@pytest.fixture
def stub_agent():
    """A bind-eligible agent whose API key matches the path agent_id."""
    agent = SimpleNamespace(
        agent_id="agent-1",
        wallet_address=None,
        erc8004_agent_id=None,
        erc8004_chain=None,
        erc8004_tx_hash=None,
        erc8004_registered_at=None,
    )
    return agent


@pytest.fixture
def stub_agent_service(stub_agent):
    svc = AsyncMock()
    svc.get_agent = AsyncMock(return_value=stub_agent)
    repo = MagicMock()
    repo.save = AsyncMock()
    repo.redis = AsyncMock()
    # No prior binding for this token id.
    repo.redis.get = AsyncMock(return_value=None)
    svc.repository = repo
    # routes/onchain reaches into ``agent_service.repository.redis`` for
    # the duplicate-binding reverse index; ``setex`` is hit by the
    # discover endpoint cache but not by bind.
    return svc


@pytest.fixture
def stub_erc():
    """A successful ERC-8004 client: chain id matches, tokenURI matches."""
    erc = AsyncMock()
    erc.verify_chain_id = AsyncMock(return_value=(True, 8453))
    erc.verify_registration = AsyncMock(return_value=True)
    erc.get_agent_wallet = AsyncMock(return_value="0xabc")
    return erc


def _override_deps(agent_svc, erc):
    """Wire the FastAPI dependency overrides for bind tests."""
    app.dependency_overrides[get_agent_service] = lambda: agent_svc
    app.dependency_overrides[get_erc8004_client] = lambda: erc
    # Bypass real API-key auth — H-erc8004 is about bind logic, not authn.
    app.dependency_overrides[verify_agent_api_key] = lambda: {"agent_id": "agent-1"}


def _clear_deps():
    app.dependency_overrides.clear()


# ─────────────────────────────────────────────
# Defence 1: client-supplied chain must match server config
# ─────────────────────────────────────────────


class TestClientChainValidation:
    def test_omitting_chain_uses_server_derived(self, stub_agent_service, stub_agent, stub_erc):
        """Default path: client doesn't send ``chain``. Server derives it
        from settings and persists the canonical value.
        """
        _override_deps(stub_agent_service, stub_erc)
        try:
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/onchain/agents/agent-1/bind",
                    json={"token_id": 42},
                )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["chain"] == "eip155:8453"
            assert stub_agent.erc8004_chain == "eip155:8453"
            stub_agent_service.repository.save.assert_awaited_once()
        finally:
            _clear_deps()

    def test_matching_chain_accepted(self, stub_agent_service, stub_agent, stub_erc):
        """Explicit, *matching* chain string is allowed (back-compat for
        clients that always send the field).

        Pin both the rpc-side guard *and* the registration check fired —
        otherwise a future refactor that drops one of the two RPC calls
        would silently weaken the bind path without failing this test.
        """
        _override_deps(stub_agent_service, stub_erc)
        try:
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/onchain/agents/agent-1/bind",
                    json={"token_id": 42, "chain": "eip155:8453"},
                )
            assert r.status_code == 200, r.text
            assert stub_agent.erc8004_chain == "eip155:8453"
            stub_erc.verify_chain_id.assert_awaited_once_with(8453)
            stub_erc.verify_registration.assert_awaited_once()
        finally:
            _clear_deps()

    def test_mismatching_chain_rejected_with_422(
        self, stub_agent_service, stub_agent, stub_erc
    ):
        """The actual H-erc8004 attack: client claims a different chain
        than the one ACN is configured for. Must be 422 with a precise
        diagnostic, and *no* state must be written.
        """
        _override_deps(stub_agent_service, stub_erc)
        try:
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/onchain/agents/agent-1/bind",
                    json={"token_id": 42, "chain": "eip155:1"},
                )
            assert r.status_code == 422
            detail = r.json()["detail"]
            assert "eip155:8453" in detail and "eip155:1" in detail, detail
            # Side-effect proof: the rejection must short-circuit before
            # any chain RPC, registration check, or save() can run.
            stub_erc.verify_chain_id.assert_not_awaited()
            stub_erc.verify_registration.assert_not_awaited()
            stub_agent_service.repository.save.assert_not_awaited()
            assert stub_agent.erc8004_chain is None
        finally:
            _clear_deps()


# ─────────────────────────────────────────────
# Defence 2: RPC node must actually be on the configured chain
# ─────────────────────────────────────────────


class TestRpcChainIdGuard:
    def test_rpc_chain_mismatch_returns_503(
        self, stub_agent_service, stub_agent, stub_erc
    ):
        """RPC node reports a different chain — server-side misconfig
        (or RPC swap attack). Must be 503, must not call verify_registration,
        must not save anything.

        503 is correct (not 422) because the client can't fix this by
        sending different data. The H4 global handler sanitises 5xx
        bodies (detail is logged server-side but stripped from the
        response), so we only assert on status_code here.

        Pin ``verify_chain_id`` is *actually invoked* — a refactor that
        accidentally short-circuits before the RPC call would otherwise
        produce a 503 by some other path and silently weaken the guard.
        """
        stub_erc.verify_chain_id = AsyncMock(return_value=(False, 1))  # eth mainnet
        _override_deps(stub_agent_service, stub_erc)
        try:
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/onchain/agents/agent-1/bind",
                    json={"token_id": 42},
                )
            assert r.status_code == 503
            stub_erc.verify_chain_id.assert_awaited_once_with(8453)
            stub_erc.verify_registration.assert_not_awaited()
            stub_agent_service.repository.save.assert_not_awaited()
        finally:
            _clear_deps()

    def test_rpc_unreachable_fails_closed(
        self, stub_agent_service, stub_agent, stub_erc
    ):
        """RPC unreachable -> verify_chain_id returns ``(False, None)``.

        We must fail-closed (503), not silently accept the bind. Without
        chain proof the binding is meaningless.
        """
        stub_erc.verify_chain_id = AsyncMock(return_value=(False, None))
        _override_deps(stub_agent_service, stub_erc)
        try:
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/onchain/agents/agent-1/bind",
                    json={"token_id": 42},
                )
            assert r.status_code == 503
            stub_erc.verify_chain_id.assert_awaited_once_with(8453)
            stub_erc.verify_registration.assert_not_awaited()
            stub_agent_service.repository.save.assert_not_awaited()
        finally:
            _clear_deps()


# ─────────────────────────────────────────────
# Persistence: persisted chain is server-derived even when client matches
# ─────────────────────────────────────────────


class TestPersistedChainAlwaysServerDerived:
    def test_client_value_never_persisted_directly(
        self, stub_agent_service, stub_agent, stub_erc
    ):
        """Even when the client sends the *correct* chain string, the
        value persisted to the agent record must trace back to the
        server's ``erc8004_chain_id`` setting — never to ``body.chain``.

        Note: a pure by-value assertion can't distinguish the two when
        the client happens to send a matching string (Python interns
        equal short strings, so the two literals are even ``is``-equal).
        We pin the by-value check here as the integration-level
        observation, and pair it with the static-analysis test below
        which catches the underlying assignment-site regression.
        """
        _override_deps(stub_agent_service, stub_erc)
        try:
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/onchain/agents/agent-1/bind",
                    json={"token_id": 42, "chain": "eip155:8453"},
                )
            assert r.status_code == 200, r.text
            assert stub_agent.erc8004_chain == "eip155:8453"
            from acn.config import get_settings
            assert (
                stub_agent.erc8004_chain
                == f"eip155:{get_settings().erc8004_chain_id}"
            )
        finally:
            _clear_deps()

    def test_assignment_site_uses_server_chain_not_body(self):
        """Static-analysis pin against the H-erc8004 root cause.

        The original bug was a single line: ``agent.erc8004_chain = body.chain``.
        Behavioural tests can't reliably catch its return because the
        client value usually equals the server-derived one (and Python
        interns equal short strings, so by-value/by-identity assertions
        both pass). We therefore lock the assignment site itself.

        Yes, this test is brittle to variable renames — that brittleness
        is the point. Anyone touching this assignment will see the
        failure, read this docstring, and (re-)convince themselves the
        client-supplied value is staying out of persistence.
        """
        import inspect

        from acn.routes import onchain

        src = inspect.getsource(onchain.bind_onchain_identity)
        assert "agent.erc8004_chain = server_chain" in src, (
            "Implementation must persist the server-derived chain string."
        )
        assert "agent.erc8004_chain = body.chain" not in src, (
            "H-erc8004 regression: client-supplied chain must not be "
            "routed into agent.erc8004_chain."
        )


# ─────────────────────────────────────────────
# Helper: ERC8004Client.verify_chain_id semantics
# ─────────────────────────────────────────────


def _make_erc_client_with_chain_id(values):
    """Build a ``ERC8004Client`` whose ``self._w3.eth.chain_id`` yields
    awaitables resolving to / raising the items in ``values`` in order.

    Each item is either an ``int`` (resolved by an awaitable) or an
    ``Exception`` instance (re-raised inside the awaitable). The helper
    bypasses ``__init__`` so we don't have to spin up real Web3 plumbing
    just to test the chain_id guard.
    """
    from acn.services.erc8004_client import ERC8004Client

    class _Eth:
        def __init__(self, items):
            self._items = list(items)
            self.calls = 0

        @property
        def chain_id(self):
            self.calls += 1
            item = self._items.pop(0) if self._items else 0

            async def _coro():
                if isinstance(item, Exception):
                    raise item
                return item

            return _coro()

    class _W3:
        def __init__(self, items):
            self.eth = _Eth(items)

    import asyncio as _asyncio

    client = ERC8004Client.__new__(ERC8004Client)
    client._cached_chain_id = None
    # Match the post-P2 cold-start lock initialised in ``__init__``;
    # the test helper bypasses ``__init__`` to avoid Web3 plumbing.
    client._chain_id_lock = _asyncio.Lock()
    client._w3 = _W3(values)
    return client


class TestErc8004ClientVerifyChainId:
    """Direct unit tests for the helper, independent of the route layer."""

    @pytest.mark.asyncio
    async def test_match_returns_true_and_caches(self):
        client = _make_erc_client_with_chain_id([8453, 8453])

        ok, actual = await client.verify_chain_id(8453)
        assert ok is True and actual == 8453
        # First call consumed exactly one chain_id read.
        assert client._w3.eth.calls == 1

        # Second call must hit the cache — chain_id is a chain-level
        # invariant, paying an RPC round-trip per bind would be silly.
        ok, actual = await client.verify_chain_id(8453)
        assert ok is True and actual == 8453
        assert client._w3.eth.calls == 1, (
            "verify_chain_id must cache the RPC result; otherwise every "
            "bind pays an extra RPC round-trip"
        )

    @pytest.mark.asyncio
    async def test_mismatch_returns_false_with_actual(self):
        client = _make_erc_client_with_chain_id([1])
        ok, actual = await client.verify_chain_id(8453)
        assert ok is False
        assert actual == 1

    @pytest.mark.asyncio
    async def test_rpc_error_returns_false_none(self):
        """Unreachable RPC must return ``(False, None)`` so the route
        can fail-closed without a leaky 500."""
        client = _make_erc_client_with_chain_id([RuntimeError("connection refused")])
        ok, actual = await client.verify_chain_id(8453)
        assert ok is False
        assert actual is None

    @pytest.mark.asyncio
    async def test_concurrent_cold_start_coalesces_to_single_rpc(self):
        """P2-#2: concurrent cold-start callers must share one RPC roundtrip.

        Before the lock was added, N coroutines hitting ``get_chain_id`` at
        once each saw ``_cached_chain_id is None`` and fired their own
        ``eth_chainId`` call — burning RPC quota and tripping provider rate
        limits during deploy bursts.
        """
        import asyncio as _asyncio

        # ``_Eth.chain_id`` returns a fresh awaitable each call — give it
        # one slow item; the lock should make later concurrent waiters reuse
        # the result instead of triggering more pops.
        from acn.services.erc8004_client import ERC8004Client

        rpc_calls = 0
        rpc_release = _asyncio.Event()

        class _SlowEth:
            @property
            def chain_id(self):
                nonlocal rpc_calls
                rpc_calls += 1

                async def _coro():
                    await rpc_release.wait()
                    return 8453

                return _coro()

        class _SlowW3:
            def __init__(self):
                self.eth = _SlowEth()

        client = ERC8004Client.__new__(ERC8004Client)
        client._cached_chain_id = None
        client._chain_id_lock = _asyncio.Lock()
        client._w3 = _SlowW3()

        callers = [_asyncio.create_task(client.get_chain_id()) for _ in range(20)]
        # Yield long enough for every coroutine to enter ``get_chain_id``
        # and queue on the lock before we let the RPC resolve.
        await _asyncio.sleep(0)
        await _asyncio.sleep(0)
        rpc_release.set()
        results = await _asyncio.gather(*callers)

        assert results == [8453] * 20
        assert rpc_calls == 1, (
            f"thundering herd: cold-start should coalesce concurrent waiters "
            f"onto a single RPC roundtrip, got {rpc_calls}"
        )

    @pytest.mark.asyncio
    async def test_lock_acquired_only_once_after_cache_populated(self):
        """Steady-state callers must skip the lock entirely (lock-free fast path)."""
        import asyncio as _asyncio

        from acn.services.erc8004_client import ERC8004Client

        client = ERC8004Client.__new__(ERC8004Client)
        client._cached_chain_id = 8453  # pretend the cache was warmed already
        sentinel_lock = _asyncio.Lock()
        await sentinel_lock.acquire()  # hold the lock — would deadlock if used
        client._chain_id_lock = sentinel_lock

        # ``_w3`` left unset on purpose: any RPC attempt would AttributeError,
        # demonstrating the warm path neither acquires the lock nor calls RPC.
        result = await client.get_chain_id()
        assert result == 8453
        assert sentinel_lock.locked()  # we never released, lock untouched
        sentinel_lock.release()
