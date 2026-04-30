"""Phase 1 L418 — wallet-dimension rate limiting (dual-bucket).

The L418 design adds a second ``@limiter.limit`` to every public inbound
write endpoint that uses ``_wallet_rate_limit_key`` instead of the
default per-agent key. This file pins the contract on three layers so a
future refactor can't silently undo the protection:

  1. **Key derivation (unit)** — ``_wallet_rate_limit_key`` returns the
     correct bucket for walleted, un-walleted, and case-variant wallet
     addresses. Catches "we forgot to lower-case" and "we returned None
     and broke slowapi" regressions.

  2. **Auth-side state propagation (unit)** — the auth dependencies
     (``verify_agent_api_key``, ``verify_proxy_caller``) actually
     populate ``request.state.wallet_address``. The key_func above is
     useless if the wallet never makes it onto the request.

  3. **Endpoint decorator coverage (static contract)** — every endpoint
     in the L418 protection list carries TWO ``@limiter.limit`` entries
     in its slowapi route record, one of which uses
     ``_wallet_rate_limit_key``. Catches "decorator removed during
     refactor" and "added a new public inbound endpoint without the
     wallet bucket" regressions.

Layer 4 (actually saturating both buckets via TestClient) is left to
e2e/load testing — it requires a running Redis-backed limiter and the
exhaust-then-429 ergonomics make the unit test slow + flaky for what's
essentially a slowapi correctness check, not our logic.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from acn.api import app
from acn.routes.dependencies import (
    WALLET_RATE_LIMIT,
    _wallet_rate_limit_key,
    limiter,
    verify_agent_api_key,
    verify_proxy_caller,
)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — _wallet_rate_limit_key derives the right bucket
# ─────────────────────────────────────────────────────────────────────────────


class TestWalletRateLimitKey:
    """``_wallet_rate_limit_key`` must produce stable, attack-resistant keys."""

    def _make_request(self, wallet_address: str | None | object = ...) -> SimpleNamespace:
        """Build a stub request whose ``.state`` mirrors what FastAPI's
        Starlette Request exposes after the auth dependency has run.

        ``wallet_address=...`` (sentinel) means ``state`` doesn't carry
        the attribute at all — distinct from ``None`` (attribute set
        but explicitly empty).
        """
        state = SimpleNamespace()
        if wallet_address is not ...:
            state.wallet_address = wallet_address
        return SimpleNamespace(state=state)

    def test_walleted_agent_uses_per_wallet_bucket(self) -> None:
        req = self._make_request(wallet_address="0xAbCdEf0123456789")
        # Lowercased so case variants of the same EVM address share one
        # bucket — otherwise the same wallet using mixed-case across
        # signers would leak budget across two slowapi keys.
        assert _wallet_rate_limit_key(req) == "wallet:0xabcdef0123456789"

    def test_unwalleted_agent_uses_global_nowallet_bucket(self) -> None:
        # Un-walleted agents share one global ceiling. This is intentional
        # (see ``_wallet_rate_limit_key`` docstring) — falling back to
        # per-agent or per-IP would let an attacker opt out of the
        # wallet ceiling simply by not binding a wallet.
        req = self._make_request(wallet_address=None)
        assert _wallet_rate_limit_key(req) == "wallet:none"

    def test_state_missing_wallet_address_attribute_is_treated_as_unwalleted(
        self,
    ) -> None:
        # An auth path that hasn't been migrated to set
        # ``request.state.wallet_address`` should fail safe (route into
        # the global nowallet bucket), not crash the request.
        req = self._make_request()  # no wallet_address attribute at all
        assert _wallet_rate_limit_key(req) == "wallet:none"

    def test_empty_string_wallet_treated_as_missing(self) -> None:
        # Defensive: a repository row with an empty-string wallet
        # (legacy / partial migration) must not produce ``wallet:`` as
        # a real bucket — that bucket would be shared by every empty-
        # string row and behaves indistinguishably from "no wallet".
        # Make the equivalence explicit instead.
        req = self._make_request(wallet_address="")
        assert _wallet_rate_limit_key(req) == "wallet:none"


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — auth dependencies propagate wallet_address onto request.state
# ─────────────────────────────────────────────────────────────────────────────


class _StubAgent:
    """Minimal stand-in for ``acn.core.entities.Agent`` — the auth flow
    only reads ``agent_id``, ``name``, ``wallet_address``."""

    def __init__(self, agent_id: str, name: str, wallet_address: str | None) -> None:
        self.agent_id = agent_id
        self.name = name
        self.wallet_address = wallet_address


def _make_request_for_auth() -> SimpleNamespace:
    """Stub Request usable by the verify_* dependencies. They write to
    ``state`` and read ``client``/``url``/``headers`` only via the
    audit helper — for state-propagation tests we just need ``state``."""
    return SimpleNamespace(
        state=SimpleNamespace(),
        client=SimpleNamespace(host="127.0.0.1"),
        url=SimpleNamespace(path="/test"),
        headers={},
        method="POST",
    )


class TestAuthPropagatesWalletAddress:
    """Both auth flows must seed ``request.state.wallet_address`` so the
    L418 key_func has something to read on every protected route."""

    @pytest.fixture(autouse=True)
    def _clear_api_key_cache(self) -> None:
        # The in-memory API key cache spans tests. Clear it so each
        # test sees a fresh ``_resolve_agent_by_bearer`` lookup with
        # whatever wallet the test wired into the stub agent service.
        from acn.routes import dependencies

        dependencies._api_key_cache.clear()
        yield
        dependencies._api_key_cache.clear()

    @pytest.mark.asyncio
    async def test_verify_agent_api_key_seeds_wallet_address(self) -> None:
        request = _make_request_for_auth()
        agent_service = AsyncMock()
        agent_service.get_agent_by_api_key.return_value = _StubAgent(
            agent_id="agent-1",
            name="alpha",
            wallet_address="0xWallet1",
        )
        await verify_agent_api_key(
            request=request,
            authorization="Bearer test-api-key",
            agent_service=agent_service,
        )
        assert request.state.agent_id == "agent-1"
        assert request.state.rate_limit_key == "agent:agent-1"
        # Critical L418 invariant — the key_func will look here.
        assert request.state.wallet_address == "0xWallet1"

    @pytest.mark.asyncio
    async def test_verify_agent_api_key_handles_unwalleted_agent(self) -> None:
        request = _make_request_for_auth()
        agent_service = AsyncMock()
        agent_service.get_agent_by_api_key.return_value = _StubAgent(
            agent_id="agent-no-wallet",
            name="beta",
            wallet_address=None,
        )
        await verify_agent_api_key(
            request=request,
            authorization="Bearer test-api-key",
            agent_service=agent_service,
        )
        # Setting it to ``None`` (rather than leaving the attribute
        # missing) means ``_wallet_rate_limit_key`` sees an explicit
        # un-walleted signal and bucket into ``wallet:none`` — matches
        # the "fail-safe to global nowallet pool" branch tested above.
        assert request.state.wallet_address is None

    @pytest.mark.asyncio
    async def test_verify_proxy_caller_seeds_wallet_address(self) -> None:
        request = _make_request_for_auth()
        agent_service = AsyncMock()
        agent_service.get_agent_by_api_key.return_value = _StubAgent(
            agent_id="proxy-agent",
            name="gamma",
            wallet_address="0xProxyWallet",
        )
        await verify_proxy_caller(
            request=request,
            x_acn_authorization="Bearer proxy-api-key",
            agent_service=agent_service,
        )
        assert request.state.agent_id == "proxy-agent"
        # Proxy traffic is the highest-volume inbound surface; wallet
        # propagation here is the linchpin keeping the abuse pattern
        # from migrating off ``/communication/send`` onto the proxy.
        assert request.state.wallet_address == "0xProxyWallet"

    @pytest.mark.asyncio
    async def test_verify_agent_api_key_rejects_invalid_key_without_seeding(self) -> None:
        request = _make_request_for_auth()
        agent_service = AsyncMock()
        agent_service.get_agent_by_api_key.return_value = None
        with pytest.raises(HTTPException) as exc:
            await verify_agent_api_key(
                request=request,
                authorization="Bearer bogus",
                agent_service=agent_service,
            )
        assert exc.value.status_code == 401
        # No state should leak from a failed auth — otherwise a
        # downstream limiter could mistakenly bucket against a wallet
        # that the request never proved ownership of.
        assert not hasattr(request.state, "wallet_address")


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — every L418-protected route carries the wallet-bucket decorator
# ─────────────────────────────────────────────────────────────────────────────

# Endpoints that L418 must cover (public-internet inbound, authenticated
# via owner API key or proxy caller key). Listed as (path, method) so
# the static check matches what slowapi sees.
#
# NOT in this list, deliberately:
#   - ``POST /api/v1/communication/internal/send`` — internal-token
#     gated, not callable from the public internet, no wallet on the
#     calling identity.
#   - ``POST /api/v1/communication/history/{id}/ack`` — ack is an
#     outbound ergonomic, doesn't consume agent-recipient budget on
#     other agents.
#   - ``GET`` proxy paths / search — read-only and far cheaper for the
#     receiving agent than write/proxy paths.
L418_PROTECTED_ENDPOINTS: list[tuple[str, str]] = [
    ("/api/v1/communication/send", "POST"),
    ("/api/v1/communication/broadcast", "POST"),
    ("/api/v1/communication/broadcast-by-tag", "POST"),
    ("/api/v1/agents/{agent_id}", "POST"),
    ("/api/v1/agents/{agent_id}", "PUT"),
    ("/api/v1/agents/{agent_id}", "PATCH"),
    # Catch-all proxy — registered with ``api_route`` for multiple
    # methods. POST is the canonical write so we pin that one; the
    # decorator is shared across all methods on the same handler.
    # FastAPI keeps the path-converter suffix (``:path``) as part of
    # the route's stored path, so we match on it verbatim.
    ("/api/v1/agents/{agent_id}/{rest_path:path}", "POST"),
]


def _slowapi_route_key(route: APIRoute) -> str:
    """Reproduce slowapi's route-key calculation.

    SlowAPI keys ``_route_limits`` by ``f'{module}.{qualname}'`` of the
    *original* (un-decorated) endpoint. We unwrap the decorator chain
    by walking ``__wrapped__`` to find the innermost function.
    """
    fn = route.endpoint
    seen: set[int] = set()
    while getattr(fn, "__wrapped__", None) is not None and id(fn) not in seen:
        seen.add(id(fn))
        fn = fn.__wrapped__
    return f"{fn.__module__}.{fn.__qualname__}"


def _find_route(path: str, method: str) -> APIRoute | None:
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path == path and method in route.methods:
            return route
    return None


def _wallet_limit_for_route(route: APIRoute) -> object | None:
    """Return the slowapi ``Limit`` whose ``key_func`` is
    ``_wallet_rate_limit_key`` for this route, or ``None`` if no such
    limit is registered.

    SlowAPI stores limits per route in ``limiter._route_limits`` keyed
    by the function's ``module.qualname``. Each entry is a list of
    ``Limit`` objects, one per ``@limiter.limit`` decoration.
    """
    key = _slowapi_route_key(route)
    limits = limiter._route_limits.get(key, [])
    for lim in limits:
        if getattr(lim, "key_func", None) is _wallet_rate_limit_key:
            return lim
    return None


class TestL418EndpointDecoratorCoverage:
    """Static contract: every protected endpoint must carry the L418
    wallet-bucket decorator. Pure introspection — no HTTP calls."""

    @pytest.mark.parametrize(("path", "method"), L418_PROTECTED_ENDPOINTS)
    def test_route_has_wallet_bucket_decorator(self, path: str, method: str) -> None:
        route = _find_route(path, method)
        assert route is not None, f"route not found: {method} {path}"
        wallet_limit = _wallet_limit_for_route(route)
        assert wallet_limit is not None, (
            f"{method} {path} is missing the L418 wallet-bucket decorator. "
            f"Required: ``@limiter.limit(WALLET_RATE_LIMIT, "
            f"key_func=_wallet_rate_limit_key)``."
        )

    @pytest.mark.parametrize(("path", "method"), L418_PROTECTED_ENDPOINTS)
    def test_route_has_two_distinct_buckets(self, path: str, method: str) -> None:
        """Wallet bucket must be a SECOND limit, not a replacement.

        This is what makes L418 a dual-bucket protection: per-agent
        attribution + per-wallet ceiling. If a refactor accidentally
        drops the per-agent decorator and only the wallet one remains,
        single-agent abuse would pop straight to the (much larger)
        wallet budget.
        """
        route = _find_route(path, method)
        assert route is not None, f"route not found: {method} {path}"
        key = _slowapi_route_key(route)
        limits = limiter._route_limits.get(key, [])
        assert len(limits) >= 2, (
            f"{method} {path} has only {len(limits)} limit(s); L418 "
            f"requires both per-agent AND per-wallet buckets."
        )
        # At least one wallet limit and at least one non-wallet limit
        # (the per-agent default).
        wallet_count = sum(
            1 for lim in limits if getattr(lim, "key_func", None) is _wallet_rate_limit_key
        )
        agent_count = len(limits) - wallet_count
        assert wallet_count >= 1, f"{method} {path} missing wallet bucket"
        assert agent_count >= 1, f"{method} {path} missing per-agent bucket"


class TestWalletRateLimitConstant:
    """Sanity checks on the ``WALLET_RATE_LIMIT`` constant itself."""

    def test_wallet_rate_limit_is_parseable_by_slowapi(self) -> None:
        # If the constant is malformed (e.g. ``"600per minute"``),
        # slowapi raises at request time — late, generic, easy to miss.
        # ``parse_many`` validates the same way slowapi does internally.
        from limits import parse_many

        parsed = parse_many(WALLET_RATE_LIMIT)
        assert len(parsed) >= 1, "WALLET_RATE_LIMIT failed to parse"

    def test_wallet_rate_limit_is_a_per_minute_ceiling(self) -> None:
        # Documents the design intent: per-minute granularity (not
        # per-second / per-hour) so it composes cleanly with the
        # existing per-agent ``60/minute`` limits and the docstring
        # arithmetic in ``_wallet_rate_limit_key`` stays accurate.
        assert "/minute" in WALLET_RATE_LIMIT, (
            f"WALLET_RATE_LIMIT must be specified per-minute "
            f"(got {WALLET_RATE_LIMIT!r}); see L418 sizing rationale "
            f"in dependencies.py."
        )
