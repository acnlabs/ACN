"""Onchain (ERC-8004) routes — flat ACN error schema contract tests.

Phase 2 review v2 P1 #11 sprint row #7 — pin the 12 4xx sites in
``acn/routes/onchain.py`` to the canonical ``ACNHTTPError`` flat
schema after their migration from raw ``HTTPException``.

This file complements the two pre-existing onchain tests
(``test_onchain_chain_id_h_erc8004.py`` for the H-erc8004 audit
defences, ``test_onchain_token_id_corruption.py`` for the
corrupt-token-id behaviour) by asserting only the *response shape*
— the four-field contract SDK clients depend on:

* ``error_code``  — stable ASCII branch key
* ``message``     — human-readable prose, never to be string-matched
* ``details``     — code-specific structured context
* ``request_id``  — UUID echoed in ``X-Request-ID`` response header

Coverage matrix (12 sites, 1:1 raise-site coverage)
---------------------------------------------------
* ``ERC8004_TOKEN_ID_MISSING`` (×1) — defensive helper branch in
  ``_parse_token_id_or_422`` reached when ``value is None``. The
  reputation / validation routes both ``404`` upstream via
  ``ERC8004_NOT_BOUND`` so this branch is not currently
  reachable end-to-end through HTTP — pinned here as a *unit*
  test on the helper directly so the flat-schema contract is
  locked even though the route layer never surfaces it today. If
  a future refactor drops the upstream ``404 ERC8004_NOT_BOUND``
  guard, this branch becomes the load-bearing 404 → 422
  fallback and the test guards it.
* ``ERC8004_TOKEN_ID_CORRUPT`` (×1) — reputation with non-numeric
  stored ``erc8004_agent_id``. ``details = {agent_id}`` only;
  the corrupt ``stored_value`` is logged operator-side but
  *deliberately not* echoed to the client (it is potentially
  attacker-controlled DB content; logs hold the diagnostic).
* ``ERC8004_CHAIN_MISMATCH`` (×1) — bind with a ``chain`` body
  field that disagrees with ``settings.erc8004_chain_id``.
  ``details = {server_chain, client_chain}``.
* ``API_KEY_AGENT_MISMATCH`` (×1, REUSED) — bind path-key
  mismatch. Strict cross-sprint shape.
* ``AGENT_NOT_FOUND`` (×4, REUSED) — every route's
  ``except AgentNotFoundException`` branch (bind, get_identity,
  reputation, validation). Pinned at every site because each
  route surfaces it independently — a refactor that swaps one
  to a different code (e.g. promotes 404 to 410 for one route)
  would silently diverge from the other three without these
  per-site tests.
* ``ERC8004_TOKEN_ALREADY_BOUND`` (×1) — bind path with a
  token already bound to a *different* agent. ``details =
  {token_id, bound_agent_id, requesting_agent_id}``.
  ``bound_agent_id`` is *intentionally* echoed back (publicly
  resolvable on-chain via ``ownerOf(token_id)``; hiding it in
  the response would force SDK clients to round-trip through
  the chain for a piece of data the route already knows).
* ``ERC8004_REGISTRATION_MISMATCH`` (×1) — bind path where the
  on-chain ``tokenURI`` doesn't match the expected
  agent-registration URL. ``details = {token_id, expected_url}``
  with ``expected_url`` preserved verbatim so the caller can
  set it as the on-chain ``tokenURI`` without reconstructing it
  from gateway_base_url + agent_id.
* ``ERC8004_NOT_BOUND`` (×2) — reputation + validation when the
  agent exists but has no ``erc8004_agent_id``. Two separate
  sites, two separate tests for the same reason as
  ``AGENT_NOT_FOUND`` above.

Total: 12 tests (11 HTTP-level + 1 helper-level unit test).

Schema-bucket invariants
------------------------
The four reused codes (``API_KEY_AGENT_MISMATCH``,
``AGENT_NOT_FOUND``) live in the strict-schema bucket of
``tests/test_error_code_details_consistency.py``. This file does
not introduce new keys for either; it only exercises the existing
strict shapes from a new entry module. The six new ERC-8004
codes are added to the strict-schema bucket alongside their
``_DEFAULT_MESSAGES`` entry — see
``tests/test_error_code_details_consistency.py`` for the
post-#7 strict-bucket membership.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.errors import ACNHTTPError, ErrorCode
from acn.routes.dependencies import (
    _api_key_cache,
    get_agent_service,
    limiter,
    verify_agent_api_key,
)
from acn.routes.onchain import (
    _parse_token_id_or_422,
    get_erc8004_client,
)
from tests.routes.conftest import _assert_flat_shape


@pytest.fixture(autouse=True)
def _reset_state():
    limiter.enabled = False
    _api_key_cache.clear()
    yield
    limiter.enabled = True
    _api_key_cache.clear()
    app.dependency_overrides.clear()


def _stub_agent(*, token_id: str | None = None) -> SimpleNamespace:
    """Build a bind-eligible agent stub.

    Default ``token_id=None`` exercises the *unbound* state used
    by ``ERC8004_NOT_BOUND`` and the bind-path tests; the
    corruption test passes a non-numeric string explicitly.
    """
    return SimpleNamespace(
        agent_id="agent-target",
        wallet_address=None,
        erc8004_agent_id=token_id,
        erc8004_chain=None,
        erc8004_tx_hash=None,
        erc8004_registered_at=None,
    )


def _stub_agent_service(agent: SimpleNamespace | None) -> AsyncMock:
    """Wire ``get_agent`` to return ``agent`` (or raise
    ``AgentNotFoundException`` if the caller passed ``None``)."""
    from acn.core.exceptions import AgentNotFoundException

    svc = AsyncMock()
    if agent is None:
        svc.get_agent = AsyncMock(
            side_effect=AgentNotFoundException("agent-target")
        )
    else:
        svc.get_agent = AsyncMock(return_value=agent)

    repo = MagicMock()
    repo.save = AsyncMock()
    repo.redis = AsyncMock()
    repo.redis.get = AsyncMock(return_value=None)
    svc.repository = repo
    return svc


def _stub_erc(
    *,
    chain_id_matches: bool = True,
    registration_matches: bool = True,
    validation_available: bool = True,
) -> AsyncMock:
    erc = AsyncMock()
    erc.verify_chain_id = AsyncMock(
        return_value=(chain_id_matches, 8453 if chain_id_matches else 1)
    )
    erc.verify_registration = AsyncMock(return_value=registration_matches)
    erc.get_agent_wallet = AsyncMock(return_value="0xabc")
    erc.validation_available = validation_available
    erc.get_reputation_summary = AsyncMock(
        return_value={
            "token_id": 42,
            "count": 0,
            "avg_value": None,
            "by_tag": {},
        }
    )
    erc.get_validation_summary = AsyncMock(
        return_value={
            "token_id": 42,
            "available": True,
            "total": 0,
            "approved": 0,
            "rejected": 0,
            "pending": 0,
            "by_tag": {},
        }
    )
    return erc


def _wire(svc, erc, *, caller_agent_id: str = "agent-target") -> None:
    """Standard wiring: agent service + ERC-8004 client + auth bypass.

    Auth is bypassed via ``verify_agent_api_key`` override because
    sprint #7 is about *route-layer* error schema, not auth — the
    auth dep itself is already covered by sprint #10.
    """
    app.dependency_overrides[get_agent_service] = lambda: svc
    app.dependency_overrides[get_erc8004_client] = lambda: erc
    app.dependency_overrides[verify_agent_api_key] = lambda: {
        "agent_id": caller_agent_id
    }


# ============================================================================
# ERC8004_TOKEN_ID_MISSING — defensive helper branch (unit test)
# ============================================================================


class TestErc8004TokenIdMissingFlatShape:
    """The ``value is None`` branch of ``_parse_token_id_or_422``
    is unreachable from HTTP today (both reputation and validation
    routes 404 with ``ERC8004_NOT_BOUND`` upstream of the helper).
    We pin the flat-schema contract via a *helper-level* unit test
    so a future refactor that drops the upstream guard does not
    silently regress the response shape."""

    def test_helper_raises_acn_http_error_on_none(self):
        with pytest.raises(ACNHTTPError) as exc_info:
            _parse_token_id_or_422(None, "agent-target")

        err = exc_info.value
        assert err.code == ErrorCode.ERC8004_TOKEN_ID_MISSING
        assert err.status_code == 422
        assert err.details == {"agent_id": "agent-target"}


# ============================================================================
# ERC8004_TOKEN_ID_CORRUPT — reputation with non-numeric stored token id
# ============================================================================


class TestErc8004TokenIdCorruptFlatShape:
    """Reputation route with a stored ``erc8004_agent_id`` that
    cannot coerce to ``int``. The corrupt ``stored_value`` is
    *deliberately* not echoed to the client (logged operator-side
    only); the response surfaces only ``agent_id``."""

    def test_corrupt_stored_token_id_404_flat_shape(self):
        agent = _stub_agent(token_id="ens-name.eth")
        svc = _stub_agent_service(agent)
        erc = _stub_erc()
        _wire(svc, erc)

        with TestClient(app) as client:
            r = client.get("/api/v1/onchain/agents/agent-target/reputation")

        assert r.status_code == 422, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "erc8004_token_id_corrupt"
        assert body["details"] == {"agent_id": "agent-target"}
        assert "stored_value" not in body["details"], (
            "stored_value must NOT be echoed to the client — it is "
            "potentially attacker-controlled DB content; logs hold "
            "the diagnostic operator-side only."
        )
        assert r.headers.get("X-Request-ID") == body["request_id"]


# ============================================================================
# ERC8004_CHAIN_MISMATCH — bind with disagreeing chain field
# ============================================================================


class TestErc8004ChainMismatchFlatShape:
    """Bind path when ``body.chain`` is provided but disagrees
    with the server-derived ``eip155:{erc8004_chain_id}``. The
    H-erc8004 audit defence: client cannot fool ACN into
    persisting "eip155:1" for a token that lives on Base."""

    def test_chain_mismatch_422_flat_shape(self):
        agent = _stub_agent()
        svc = _stub_agent_service(agent)
        erc = _stub_erc()
        _wire(svc, erc)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/onchain/agents/agent-target/bind",
                json={"token_id": 42, "chain": "eip155:1"},
            )

        assert r.status_code == 422, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "erc8004_chain_mismatch"
        assert body["details"] == {
            "server_chain": "eip155:8453",
            "client_chain": "eip155:1",
        }
        # Side-effect proof: short-circuit before any chain RPC,
        # registration check, or save() can run — pinned here too
        # so a refactor that re-orders the guards is caught.
        erc.verify_chain_id.assert_not_awaited()
        erc.verify_registration.assert_not_awaited()
        svc.repository.save.assert_not_awaited()


# ============================================================================
# API_KEY_AGENT_MISMATCH — bind path-key mismatch
# ============================================================================


class TestApiKeyAgentMismatchFlatShape:
    """Bind path when the authenticated key's agent_id differs
    from the path agent_id. Strict cross-sprint shape
    ``{path_agent, key_agent}`` shared with sprints #1 / #5 /
    #6 / #10."""

    def test_path_key_mismatch_403_flat_shape(self):
        agent = _stub_agent()
        svc = _stub_agent_service(agent)
        erc = _stub_erc()
        _wire(svc, erc, caller_agent_id="agent-other")

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/onchain/agents/agent-target/bind",
                json={"token_id": 42},
            )

        assert r.status_code == 403, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "api_key_agent_mismatch"
        assert body["details"] == {
            "path_agent": "agent-target",
            "key_agent": "agent-other",
        }


# ============================================================================
# AGENT_NOT_FOUND — every route's ``except AgentNotFoundException`` branch
# ============================================================================


class TestAgentNotFoundFlatShape:
    """``AgentNotFoundException`` from ``agent_service.get_agent``
    surfaces in four routes (bind, identity, reputation,
    validation). Pinned per-site because a refactor that swaps
    one route to a different code would silently diverge from
    the others without these tests."""

    def test_bind_agent_not_found_flat_shape(self):
        svc = _stub_agent_service(None)
        erc = _stub_erc()
        _wire(svc, erc)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/onchain/agents/agent-target/bind",
                json={"token_id": 42},
            )

        assert r.status_code == 404, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "agent_not_found"
        assert body["details"] == {"agent_id": "agent-target"}

    def test_get_identity_agent_not_found_flat_shape(self):
        svc = _stub_agent_service(None)
        erc = _stub_erc()
        _wire(svc, erc)

        with TestClient(app) as client:
            r = client.get("/api/v1/onchain/agents/agent-target")

        assert r.status_code == 404, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "agent_not_found"
        assert body["details"] == {"agent_id": "agent-target"}

    def test_reputation_agent_not_found_flat_shape(self):
        svc = _stub_agent_service(None)
        erc = _stub_erc()
        _wire(svc, erc)

        with TestClient(app) as client:
            r = client.get("/api/v1/onchain/agents/agent-target/reputation")

        assert r.status_code == 404, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "agent_not_found"
        assert body["details"] == {"agent_id": "agent-target"}

    def test_validation_agent_not_found_flat_shape(self):
        svc = _stub_agent_service(None)
        erc = _stub_erc()
        _wire(svc, erc)

        with TestClient(app) as client:
            r = client.get("/api/v1/onchain/agents/agent-target/validation")

        assert r.status_code == 404, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "agent_not_found"
        assert body["details"] == {"agent_id": "agent-target"}


# ============================================================================
# ERC8004_TOKEN_ALREADY_BOUND — bind with already-bound token
# ============================================================================


class TestErc8004TokenAlreadyBoundFlatShape:
    """Bind path when ``_check_duplicate_token`` finds the
    requested ``token_id`` is already bound to a *different*
    agent. ``bound_agent_id`` IS echoed back deliberately —
    publicly resolvable on-chain via ``ownerOf(token_id)``;
    hiding it in the response would force SDK clients to
    round-trip through the chain for data the route already
    knows. Echoing it keeps the SDK contract honest and avoids
    a false sense of privacy."""

    def test_already_bound_409_flat_shape(self):
        agent = _stub_agent()
        svc = _stub_agent_service(agent)
        erc = _stub_erc()
        # Reverse-index says token 42 is already bound to agent-other.
        svc.repository.redis.get = AsyncMock(return_value="agent-other")
        _wire(svc, erc)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/onchain/agents/agent-target/bind",
                json={"token_id": 42},
            )

        assert r.status_code == 409, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "erc8004_token_already_bound"
        assert body["details"] == {
            "token_id": 42,
            "bound_agent_id": "agent-other",
            "requesting_agent_id": "agent-target",
        }
        # Persistence must short-circuit on duplicate.
        erc.verify_registration.assert_not_awaited()
        svc.repository.save.assert_not_awaited()


# ============================================================================
# ERC8004_REGISTRATION_MISMATCH — bind with mismatched on-chain tokenURI
# ============================================================================


class TestErc8004RegistrationMismatchFlatShape:
    """Bind path when ``erc8004.verify_registration`` returns
    ``False``. ``expected_url`` preserved verbatim so the caller
    can set it as the on-chain ``tokenURI`` without
    reconstructing it from gateway_base_url + agent_id."""

    def test_registration_mismatch_422_flat_shape(self):
        agent = _stub_agent()
        svc = _stub_agent_service(agent)
        erc = _stub_erc(registration_matches=False)
        _wire(svc, erc)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/onchain/agents/agent-target/bind",
                json={"token_id": 42},
            )

        assert r.status_code == 422, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "erc8004_registration_mismatch"
        details = body["details"]
        assert details["token_id"] == 42
        # ``expected_url`` is constructed from ``settings.gateway_base_url``,
        # which varies by deployment. We assert the *shape* (path tail)
        # rather than the absolute URL so the test is robust to the
        # default gateway changing in future env defaults.
        assert details["expected_url"].endswith(
            "/api/v1/agents/agent-target/.well-known/agent-registration.json"
        ), details["expected_url"]
        assert set(details.keys()) == {"token_id", "expected_url"}, (
            "details must contain exactly token_id + expected_url; any "
            "additional key is a contract widening — flip the strict-bucket "
            "membership in test_error_code_details_consistency.py first."
        )
        # Persistence must short-circuit when registration doesn't match.
        svc.repository.save.assert_not_awaited()


# ============================================================================
# ERC8004_NOT_BOUND — reputation + validation when agent has no token id
# ============================================================================


class TestErc8004NotBoundFlatShape:
    """Reputation and validation routes both 404 when the agent
    exists but has no ``erc8004_agent_id``. Same code, same
    ``details = {agent_id}`` shape — pinned at both sites because
    a refactor that splits them (e.g. introduces a new code for
    the validation-only path) would silently break the cross-site
    parity SDK consumers rely on."""

    def test_reputation_not_bound_404_flat_shape(self):
        agent = _stub_agent(token_id=None)
        svc = _stub_agent_service(agent)
        erc = _stub_erc()
        _wire(svc, erc)

        with TestClient(app) as client:
            r = client.get("/api/v1/onchain/agents/agent-target/reputation")

        assert r.status_code == 404, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "erc8004_not_bound"
        assert body["details"] == {"agent_id": "agent-target"}

    def test_validation_not_bound_404_flat_shape(self):
        agent = _stub_agent(token_id=None)
        svc = _stub_agent_service(agent)
        erc = _stub_erc()
        _wire(svc, erc)

        with TestClient(app) as client:
            r = client.get("/api/v1/onchain/agents/agent-target/validation")

        assert r.status_code == 404, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "erc8004_not_bound"
        assert body["details"] == {"agent_id": "agent-target"}
