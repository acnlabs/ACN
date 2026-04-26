"""Pre-launch audit backlog #1: tolerate corrupt ``erc8004_agent_id``.

Token IDs are persisted as ``str`` for forward-compat. A manually-edited or
extremely old DB row could hold a non-numeric value; before the fix every
ERC-8004 read endpoint would unwrap that with bare ``int(...)`` and 5xx.

Three defences are pinned here:
  1. ``GET /onchain/agents/{id}/reputation`` returns 422 on corrupt token id
  2. ``GET /onchain/agents/{id}/validation`` returns 422 on corrupt token id
  3. ``build_erc8004_registration_file`` (used by ``.well-known/agent-
     registration.json``) skips the optional ``registrations`` field rather
     than raising — the spec allows omitting it, so the .well-known endpoint
     stays 200.

Why 422, not 400 / 500?
  - 400 implies "bad client request", but the client did nothing wrong;
    the corruption is server-side.
  - 500 hides the cause from the operator AND tells the client "ACN is
    broken", which is misleading.
  - 422 (Unprocessable Entity) is the closest semantic match for "the
    stored data cannot be processed" without claiming a generic outage.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.routes.dependencies import get_agent_service, limiter
from acn.routes.onchain import get_erc8004_client
from acn.services.agent_service import build_erc8004_registration_file


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    was = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = was


def _stub_agent(token_id: str | None = "42"):
    return SimpleNamespace(
        agent_id="agent-1",
        name="Test Agent",
        description="desc",
        accepts_payment=False,
        status=SimpleNamespace(value="ONLINE"),
        wallet_address=None,
        erc8004_agent_id=token_id,
        erc8004_chain=None,
        erc8004_tx_hash=None,
        erc8004_registered_at=None,
    )


def _stub_service(agent):
    svc = AsyncMock()
    svc.get_agent = AsyncMock(return_value=agent)
    return svc


def _stub_erc(*, validation_available: bool = True):
    erc = MagicMock()
    erc.validation_available = validation_available
    erc.get_reputation_summary = AsyncMock(
        return_value={"token_id": 42, "count": 0, "avg_value": None, "by_tag": {}}
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


def _wire(svc, erc):
    app.dependency_overrides[get_agent_service] = lambda: svc
    app.dependency_overrides[get_erc8004_client] = lambda: erc


def _clear():
    app.dependency_overrides.clear()


# ─────────────────────────────────────────────
# Reputation endpoint
# ─────────────────────────────────────────────


class TestReputationCorruptTokenId:
    def test_numeric_token_id_succeeds(self):
        """Sanity: the happy path still works after the helper refactor."""
        agent = _stub_agent(token_id="42")
        svc = _stub_service(agent)
        erc = _stub_erc()
        _wire(svc, erc)
        try:
            with TestClient(app) as client:
                r = client.get("/api/v1/onchain/agents/agent-1/reputation")
            assert r.status_code == 200, r.text
            erc.get_reputation_summary.assert_awaited_once_with(42)
        finally:
            _clear()

    @pytest.mark.parametrize(
        "corrupt_value",
        [
            "not-a-number",
            "ens-name.eth",
            "0xabc",  # hex string, not a decimal int
            "1.5",  # float-shaped
            "",  # empty (falsy — but exercise the 404 guard upstream)
        ],
    )
    def test_corrupt_token_id_returns_422_not_500(self, corrupt_value):
        """Non-numeric persisted token ids must surface as 422, never 5xx."""
        agent = _stub_agent(token_id=corrupt_value)
        svc = _stub_service(agent)
        erc = _stub_erc()
        _wire(svc, erc)
        try:
            with TestClient(app) as client:
                r = client.get("/api/v1/onchain/agents/agent-1/reputation")
            if corrupt_value == "":
                # Empty string is falsy — the upstream ``if not erc8004_agent_id``
                # 404 guard fires first. That's still safe (no 5xx), but the
                # contract is "no 500 from int() coercion", which holds.
                assert r.status_code in (404, 422), r.text
            else:
                assert r.status_code == 422, r.text
                assert "valid integer" in r.json()["detail"].lower()
            erc.get_reputation_summary.assert_not_awaited()
        finally:
            _clear()


# ─────────────────────────────────────────────
# Validation endpoint
# ─────────────────────────────────────────────


class TestValidationCorruptTokenId:
    def test_numeric_token_id_succeeds(self):
        agent = _stub_agent(token_id="42")
        svc = _stub_service(agent)
        erc = _stub_erc()
        _wire(svc, erc)
        try:
            with TestClient(app) as client:
                r = client.get("/api/v1/onchain/agents/agent-1/validation")
            assert r.status_code == 200, r.text
            erc.get_validation_summary.assert_awaited_once_with(42)
        finally:
            _clear()

    def test_corrupt_token_id_returns_422_not_500(self):
        agent = _stub_agent(token_id="not-a-number")
        svc = _stub_service(agent)
        erc = _stub_erc()
        _wire(svc, erc)
        try:
            with TestClient(app) as client:
                r = client.get("/api/v1/onchain/agents/agent-1/validation")
            assert r.status_code == 422, r.text
            erc.get_validation_summary.assert_not_awaited()
        finally:
            _clear()


# ─────────────────────────────────────────────
# build_erc8004_registration_file (.well-known)
# ─────────────────────────────────────────────


class TestRegistrationFileCorruptTokenId:
    """The .well-known endpoint must NOT 5xx on corrupt rows.

    The ``registrations`` field is optional in EIP-8004 v1, so we'd rather
    serve a partially-populated (but valid) registration file than tank
    the agent's discoverability over a single bad column.
    """

    def _settings(self):
        # Construct minimally — only the fields used by the function.
        return SimpleNamespace(
            gateway_base_url="https://acn.example",
            a2a_protocol_version="0.2.5",
            erc8004_chain_id=8453,
            erc8004_identity_contract="0xidentity",
        )

    def test_numeric_token_id_includes_registrations(self):
        agent = _stub_agent(token_id="42")
        agent.status = MagicMock()
        agent.status.value = "ONLINE"
        # Patch the AgentStatus equality check used inside the function:
        # the function does ``agent.status == AgentStatus.ONLINE``. Bypass
        # by giving status a dunder eq that returns True; or simpler, mock
        # the full call surface by importing the real enum.
        from acn.core.entities import AgentStatus

        agent.status = AgentStatus.ONLINE

        out = build_erc8004_registration_file(agent, self._settings())
        assert "registrations" in out
        assert out["registrations"][0]["agentId"] == 42
        assert out["registrations"][0]["agentRegistry"] == (
            "eip155:8453:0xidentity"
        )

    @pytest.mark.parametrize(
        "corrupt_value",
        [
            "not-a-number",
            "ens-name.eth",
            "0xabc",
            "1.5",
        ],
    )
    def test_corrupt_token_id_omits_registrations_field(self, corrupt_value):
        from acn.core.entities import AgentStatus

        agent = _stub_agent(token_id=corrupt_value)
        agent.status = AgentStatus.ONLINE

        out = build_erc8004_registration_file(agent, self._settings())
        assert "registrations" not in out, (
            "Corrupt token id should be silently omitted from registration "
            "file rather than crashing or producing a malformed entry."
        )
        # The rest of the file must still be well-formed.
        assert out["name"] == "Test Agent"
        assert out["services"][0]["name"] == "A2A"
