"""Phase 3 attention_fee — POST /communication/send wire contract.

Pins the route → service → router seam for the attention_fee field:

* Pydantic schema: ``amount`` is bounded ``[1, 1000]``, ``currency``
  defaults to ``"credits"``. Values outside the window surface as
  FastAPI 422 (Pydantic) so the SDK gets a structured field-level
  message before the request even reaches the handler body.
* ``AttentionFeeWrongModeError`` (router-side) → 400
  ``ATTENTION_FEE_REQUIRES_MANIFEST_MODE`` so the sender knows their
  funds were *not* locked.
* ``AttentionFeeLockError`` (dispatcher-side) → 400
  ``ATTENTION_FEE_LOCK_FAILED`` carrying the backend's failure
  reason in ``details`` so the SDK can branch on "top up wallet"
  vs. "drop the fee".
* Unsupported currency → 400 ``ATTENTION_FEE_INVALID``.
* Happy path: the fee dict is forwarded to ``send_message`` as a
  keyword argument so the router can flow it into the manifest
  dispatcher.

Service-layer coverage (HSETNX idempotency, escrow lock-before-write
ordering) lives in ``tests/services/test_attention_fee.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.infrastructure.messaging.manifest_dispatcher import AttentionFeeLockError
from acn.infrastructure.messaging.message_router import AttentionFeeWrongModeError
from acn.routes.dependencies import (
    get_audit,
    get_message_service,
    get_metrics,
    limiter,
    verify_agent_api_key,
)


@pytest.fixture
def stub_metrics():
    m = AsyncMock()
    m.inc_message_count = AsyncMock()
    m.inc_counter = AsyncMock()
    return m


@pytest.fixture
def stub_audit():
    a = AsyncMock()
    a.log_event = AsyncMock()
    return a


@pytest.fixture
def stub_message_service():
    """Default: returns a manifest-mode receipt with locked escrow id."""
    svc = AsyncMock()
    svc.send_message = AsyncMock(
        return_value={
            "status": "sent",
            "delivery_mode": "manifest",
            "route_id": "rt12345",
            "mid": "0" * 32,
            "ts": 1_700_000_000_000,
            "attention_fee": {
                "escrow_id": "esc_xyz",
                "amount": 50,
                "currency": "credits",
                "status": "locked",
            },
        }
    )
    return svc


@pytest.fixture(autouse=True)
def _reset_overrides_and_limiter():
    limiter.enabled = False
    yield
    limiter.enabled = True
    app.dependency_overrides.clear()


def _wire(metrics, message_service, audit) -> None:
    app.dependency_overrides[get_metrics] = lambda: metrics
    app.dependency_overrides[get_message_service] = lambda: message_service
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[verify_agent_api_key] = lambda: {"agent_id": "agent-sender"}


def _send_body(**overrides) -> dict:
    body = {
        "from_agent": "agent-sender",
        "target_agent": "agent-target",
        "message": {"text": "hello"},
        "priority": "normal",
        "attention_fee": {"amount": 50, "currency": "credits"},
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


class TestSendWithAttentionFee:
    def test_happy_path_forwards_fee_to_service(
        self, stub_metrics, stub_message_service, stub_audit
    ):
        _wire(stub_metrics, stub_message_service, stub_audit)

        with patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/send",
                    json=_send_body(),
                )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["delivery_mode"] == "manifest"
        assert body["attention_fee"]["escrow_id"] == "esc_xyz"

        # The route layer normalised currency to lowercase + forwarded
        # the dict shape ``{amount, currency}``.
        kwargs = stub_message_service.send_message.await_args.kwargs
        assert kwargs["attention_fee"] == {"amount": 50, "currency": "credits"}


# ---------------------------------------------------------------------------
# 2. Schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    @pytest.mark.parametrize("bad_amount", [0, -1, 1001, 999_999])
    def test_amount_outside_window_returns_422(
        self, stub_metrics, stub_message_service, stub_audit, bad_amount
    ):
        _wire(stub_metrics, stub_message_service, stub_audit)

        with patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/send",
                    json=_send_body(attention_fee={"amount": bad_amount}),
                )

        assert r.status_code == 422, r.text
        stub_message_service.send_message.assert_not_awaited()

    def test_unsupported_currency_returns_400(
        self, stub_metrics, stub_message_service, stub_audit
    ):
        _wire(stub_metrics, stub_message_service, stub_audit)

        with patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/send",
                    json=_send_body(
                        attention_fee={"amount": 50, "currency": "bitcoin"}
                    ),
                )

        assert r.status_code == 400, r.text
        body = r.json()
        assert body["error_code"] == "attention_fee_invalid"
        assert "bitcoin" in body["details"]["currency"]
        stub_message_service.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. Router / dispatcher errors → ACNHTTPError mapping
# ---------------------------------------------------------------------------


class TestRouterErrorMapping:
    def test_wrong_mode_returns_400_requires_manifest_mode(
        self, stub_metrics, stub_audit
    ):
        svc = AsyncMock()
        svc.send_message = AsyncMock(
            side_effect=AttentionFeeWrongModeError(
                recipient_id="agent-target",
                actual_route="inbox",
            )
        )
        _wire(stub_metrics, svc, stub_audit)

        with patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/send",
                    json=_send_body(),
                )

        assert r.status_code == 400, r.text
        body = r.json()
        assert body["error_code"] == "attention_fee_requires_manifest_mode"
        assert body["details"]["recipient_id"] == "agent-target"
        assert body["details"]["actual_route"] == "inbox"

    def test_lock_failure_returns_400_lock_failed(self, stub_metrics, stub_audit):
        svc = AsyncMock()
        svc.send_message = AsyncMock(
            side_effect=AttentionFeeLockError(reason="insufficient balance")
        )
        _wire(stub_metrics, svc, stub_audit)

        with patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/send",
                    json=_send_body(),
                )

        assert r.status_code == 400, r.text
        body = r.json()
        assert body["error_code"] == "attention_fee_lock_failed"
        assert body["details"]["reason"] == "insufficient balance"


# ---------------------------------------------------------------------------
# 4. Backwards compatibility
# ---------------------------------------------------------------------------


class TestBackwardsCompat:
    def test_request_without_attention_fee_still_works(
        self, stub_metrics, stub_audit
    ):
        """Phase 3 is purely additive — pre-Phase-3 SDK clients omit
        the field entirely and must continue to receive 200s with no
        escrow side effects."""
        svc = AsyncMock()
        svc.send_message = AsyncMock(
            return_value={"status": "sent", "delivery_mode": "inbox"}
        )
        _wire(stub_metrics, svc, stub_audit)

        with patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/send",
                    json={
                        "from_agent": "agent-sender",
                        "target_agent": "agent-target",
                        "message": {"text": "hi"},
                        "priority": "normal",
                    },
                )

        assert r.status_code == 200, r.text
        kwargs = svc.send_message.await_args.kwargs
        assert "attention_fee" not in kwargs
