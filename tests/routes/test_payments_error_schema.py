"""Payments routes — flat ACN error schema contract tests.

Phase 2 review v2 P1 #11 sprint row #5 — pin the 13 4xx sites in
``acn/routes/payments.py`` to the canonical ``ACNHTTPError`` flat
schema after their migration from raw ``HTTPException``.

Behavioural assertions (auth gating, persistence side effects, etc.)
live elsewhere; this file complements them by asserting only the
*response shape* — the four-field contract SDK clients depend on:

* ``error_code``  — stable ASCII branch key
* ``message``     — human-readable prose, never to be string-matched
* ``details``     — code-specific structured context, keys documented
                    in ``docs/features/acn-error-schema.md``
* ``request_id``  — UUID echoed in the ``X-Request-ID`` response header

We also assert ``"detail"`` is **absent** from migrated responses —
its presence would indicate a leak of legacy ``HTTPException``
shape and SDK clients have an explicit branch that would
mis-route those.

Coverage matrix
---------------
13 4xx sites × 7 distinct error codes:

* ``API_KEY_AGENT_MISMATCH`` (×4 — `set_payment_capability`,
  `get_agent_payment_tasks`, `get_agent_payment_stats`,
  `set_token_pricing` path-mismatch sites). Each site is exercised
  individually — they all share the same gating pattern but live in
  different handlers, so a future refactor that breaks one without
  breaking the others would otherwise hide.
* ``AGENT_NOT_FOUND`` (×2 — `set_payment_capability`'s
  ``AgentNotFoundException`` catch + `set_token_pricing`'s
  ``registry.get_agent`` returning ``None``). Two distinct code
  paths to the same canonical 404, both worth pinning.
* ``FROM_AGENT_MISMATCH`` (×1 — `create_payment_task` body-field
  mismatch). The body-field flavour of agent-mismatch — distinct
  from ``API_KEY_AGENT_MISMATCH`` (which is path-field).
* ``PAYMENT_CAPABILITY_NOT_FOUND`` (×1 — `get_payment_capability`).
* ``PAYMENT_TASK_NOT_FOUND`` (×1 — `get_payment_task` internal lookup).
* ``TOKEN_PRICING_NOT_CONFIGURED`` (×3 — `get_token_pricing`,
  `estimate_cost`, `bill_usage`). Three flavours: agent_id from path
  (×1) vs body field on a public estimate endpoint (×1) vs body
  field on an internal-token billing endpoint (×1).
* ``BILLING_TRANSACTION_NOT_FOUND`` (×1 — `get_billing_transaction`).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException
from acn.routes.dependencies import (
    get_agent_service,
    get_billing_service,
    get_payment_discovery,
    get_payment_tasks,
    get_registry,
    verify_agent_api_key,
    verify_internal_token,
)
from tests.routes.conftest import _assert_flat_shape


def _agent_info(agent_id: str = "agent-self") -> dict:
    """Match the dict shape returned by ``verify_agent_api_key``."""
    return {"agent_id": agent_id, "owner": "user-1"}


@pytest.fixture
def stub_agent_service():
    svc = AsyncMock()
    svc.get_agent = AsyncMock(side_effect=AgentNotFoundException("missing"))
    svc.repository = MagicMock()
    svc.repository.save = AsyncMock()
    return svc


@pytest.fixture
def stub_payment_discovery():
    pd = AsyncMock()
    pd.get_agent_payment_capability = AsyncMock(return_value=None)
    pd.find_agents_accepting_payment = AsyncMock(return_value=[])
    pd.index_payment_capability = AsyncMock()
    return pd


@pytest.fixture
def stub_payment_tasks():
    pt = AsyncMock()
    pt.get_task = AsyncMock(return_value=None)
    pt.create_payment_task = AsyncMock()
    pt.get_tasks_by_agent = AsyncMock(return_value=[])
    pt.get_payment_stats = AsyncMock(return_value={})
    return pt


@pytest.fixture
def stub_registry():
    reg = AsyncMock()
    reg.get_agent = AsyncMock(return_value=None)
    return reg


@pytest.fixture
def stub_billing_service():
    bs = AsyncMock()
    bs.get_transaction = AsyncMock(return_value=None)
    return bs


def _wire_self_authed(agent_id: str = "agent-self") -> None:
    """Override ``verify_agent_api_key`` so the request appears to
    arrive from an authenticated agent with the given id."""
    app.dependency_overrides[verify_agent_api_key] = lambda: _agent_info(agent_id)


def _wire_internal_token() -> None:
    app.dependency_overrides[verify_internal_token] = lambda: None


# ============================================================================
# API_KEY_AGENT_MISMATCH (4 sites — path agent_id vs auth-key agent_id)
# ============================================================================


class TestApiKeyAgentMismatchFlatShape:
    """All four ``payments.py`` sites that gate on ``agent_info["agent_id"]
    != agent_id`` (path) emit the same code with documented details.

    The four endpoints share the gating pattern but live in distinct
    handlers; pinning each independently catches a refactor that
    breaks one without breaking the others.
    """

    EXPECTED_DETAILS = {
        "path_agent": "agent-target",
        "key_agent": "agent-other",
    }

    def test_set_payment_capability_403_flat_shape(self):
        _wire_self_authed("agent-other")
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/payments/agent-target/payment-capability",
                json={
                    "supported_methods": ["platform_credits"],
                    "supported_networks": ["ethereum"],
                },
            )
        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "api_key_agent_mismatch"
        assert body["details"] == self.EXPECTED_DETAILS
        assert r.headers.get("X-Request-ID") == body["request_id"]

    def test_get_agent_payment_tasks_403_flat_shape(self):
        _wire_self_authed("agent-other")
        with TestClient(app) as client:
            r = client.get("/api/v1/payments/tasks/agent/agent-target")
        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "api_key_agent_mismatch"
        assert body["details"] == self.EXPECTED_DETAILS

    def test_get_agent_payment_stats_403_flat_shape(self):
        _wire_self_authed("agent-other")
        with TestClient(app) as client:
            r = client.get("/api/v1/payments/stats/agent-target")
        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "api_key_agent_mismatch"
        assert body["details"] == self.EXPECTED_DETAILS

    def test_set_token_pricing_403_flat_shape(self):
        _wire_self_authed("agent-other")
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/payments/agent-target/token-pricing",
                json={
                    "input_price_per_million": 1.0,
                    "output_price_per_million": 2.0,
                },
            )
        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "api_key_agent_mismatch"
        assert body["details"] == self.EXPECTED_DETAILS


# ============================================================================
# AGENT_NOT_FOUND (2 sites — distinct code paths)
# ============================================================================


class TestAgentNotFoundFlatShape:
    """Two distinct code paths funnel into the same canonical 404:
    ``set_payment_capability`` catches ``AgentNotFoundException`` from
    ``agent_service.get_agent``; ``set_token_pricing`` checks
    ``registry.get_agent`` returning ``None``. Both must emit the
    same ``agent_not_found`` flat shape so SDK clients write one
    handler."""

    def test_set_payment_capability_404_flat_shape(self, stub_agent_service):
        _wire_self_authed("agent-x")
        app.dependency_overrides[get_agent_service] = lambda: stub_agent_service
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/payments/agent-x/payment-capability",
                json={
                    "supported_methods": ["platform_credits"],
                    "supported_networks": ["ethereum"],
                },
            )
        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "agent_not_found"
        assert body["details"] == {"agent_id": "agent-x"}

    def test_set_token_pricing_404_flat_shape(self, stub_registry):
        _wire_self_authed("agent-x")
        app.dependency_overrides[get_registry] = lambda: stub_registry
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/payments/agent-x/token-pricing",
                json={
                    "input_price_per_million": 1.0,
                    "output_price_per_million": 2.0,
                },
            )
        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "agent_not_found"
        assert body["details"] == {"agent_id": "agent-x"}


# ============================================================================
# FROM_AGENT_MISMATCH (1 site — body field, NOT path)
# ============================================================================


class TestFromAgentMismatchFlatShape:
    """``create_payment_task`` is the only payments site that gates on
    a *body field* (``request.from_agent``) rather than a path
    parameter. Distinct error code from ``API_KEY_AGENT_MISMATCH``
    so SDK clients can branch on the source of the mismatch."""

    def test_create_payment_task_403_flat_shape(self):
        _wire_self_authed("agent-other")
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/payments/tasks",
                json={
                    "from_agent": "agent-claimed",
                    "to_agent": "agent-receiver",
                    "amount": 1.5,
                    "currency": "USD",
                    "payment_method": "platform_credits",
                    "network": "ethereum",
                },
            )
        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "from_agent_mismatch"
        assert body["details"] == {
            "authenticated_as": "agent-other",
            "from_agent": "agent-claimed",
        }


# ============================================================================
# Resource-not-found codes (4 distinct codes, 6 sites)
# ============================================================================


class TestResourceNotFoundFlatShape:
    """The four payments-specific resource-existence error codes —
    each pinned at every raise site. ``TOKEN_PRICING_NOT_CONFIGURED``
    has three sites (path / public estimate / internal billing) that
    converge on the same code; the others have a single site each."""

    def test_get_payment_capability_404_flat_shape(self, stub_payment_discovery):
        app.dependency_overrides[get_payment_discovery] = lambda: stub_payment_discovery
        with TestClient(app) as client:
            r = client.get("/api/v1/payments/agent-x/payment-capability")
        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "payment_capability_not_found"
        assert body["details"] == {"agent_id": "agent-x"}

    def test_get_payment_task_404_flat_shape(self, stub_payment_tasks):
        _wire_internal_token()
        app.dependency_overrides[get_payment_tasks] = lambda: stub_payment_tasks
        with TestClient(app) as client:
            r = client.get("/api/v1/payments/tasks/task-ghost")
        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "payment_task_not_found"
        assert body["details"] == {"task_id": "task-ghost"}

    def test_get_token_pricing_404_flat_shape(self, stub_payment_discovery):
        app.dependency_overrides[get_payment_discovery] = lambda: stub_payment_discovery
        with TestClient(app) as client:
            r = client.get("/api/v1/payments/agent-x/token-pricing")
        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "token_pricing_not_configured"
        assert body["details"] == {"agent_id": "agent-x"}

    def test_estimate_cost_404_flat_shape(self, stub_payment_discovery):
        app.dependency_overrides[get_payment_discovery] = lambda: stub_payment_discovery
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/payments/billing/estimate",
                json={
                    "agent_id": "agent-x",
                    "estimated_input_tokens": 100,
                    "estimated_output_tokens": 50,
                },
            )
        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "token_pricing_not_configured"
        assert body["details"] == {"agent_id": "agent-x"}

    def test_bill_usage_404_flat_shape(self, stub_payment_discovery):
        _wire_internal_token()
        app.dependency_overrides[get_payment_discovery] = lambda: stub_payment_discovery
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/payments/billing/charge",
                json={
                    "user_id": "user-1",
                    "agent_id": "agent-x",
                    "input_tokens": 100,
                    "output_tokens": 50,
                },
            )
        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "token_pricing_not_configured"
        assert body["details"] == {"agent_id": "agent-x"}

    def test_get_billing_transaction_404_flat_shape(self, stub_billing_service):
        _wire_internal_token()
        app.dependency_overrides[get_billing_service] = lambda: stub_billing_service
        with TestClient(app) as client:
            r = client.get("/api/v1/payments/billing/transactions/txn-ghost")
        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "billing_transaction_not_found"
        assert body["details"] == {"transaction_id": "txn-ghost"}
