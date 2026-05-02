"""Tests for the routes-layer mapping of ``PolicyRejected``.

Phase 1 decision (see "Phase 1 网关执行点决策" in
docs/features/acn-communication-economic-model.md):

- ``POST /communication/send`` — single-send rejected by recipient's
  ``communication_policy`` returns **HTTP 403** with structured
  ``detail`` so clients can branch on a stable reason code without
  parsing free-form strings.
- ``POST /communication/internal/send`` — should be unreachable in
  practice (``system:*`` is exempt at PolicyCheckService), but keeps
  the same structured 403 as a defensive fallback. Never bubbles up
  as a 500.

The contract pinned here is the wire-level shape of the rejection —
breaking it would force every existing client to update their error
handling in lockstep.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import PolicyRejected
from acn.monitoring.audit import AuditEventType
from acn.routes.dependencies import (
    get_audit,
    get_message_service,
    get_metrics,
    limiter,
    verify_agent_api_key,
)

VALID_INTERNAL_TOKEN = "test-internal-token-min-32-chars-padding"


@pytest.fixture
def stub_metrics():
    m = AsyncMock()
    m.inc_message_count = AsyncMock()
    m.inc_counter = AsyncMock()
    return m


@pytest.fixture
def stub_message_service():
    """A service whose ``send_message`` raises PolicyRejected."""
    svc = AsyncMock()
    svc.send_message = AsyncMock(
        side_effect=PolicyRejected(
            reason="policy_closed",
            reject_reason="On vacation until 2026-05",
            recipient_id="agent-target",
        )
    )
    return svc


@pytest.fixture
def stub_audit():
    a = AsyncMock()
    a.log_event = AsyncMock()
    return a


@pytest.fixture(autouse=True)
def _reset_overrides_and_limiter():
    limiter.enabled = False
    yield
    limiter.enabled = True
    app.dependency_overrides.clear()


def _wire_send_dep_overrides(metrics, message_service, audit) -> None:
    """Mirror the override set used by the existing internal-send tests
    plus the per-agent API-key auth that the public ``/send`` route
    requires. The overridden ``verify_agent_api_key`` returns a stub
    that satisfies the route's spoofing check
    (``agent_info["agent_id"] == body.from_agent``)."""
    app.dependency_overrides[get_metrics] = lambda: metrics
    app.dependency_overrides[get_message_service] = lambda: message_service
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[verify_agent_api_key] = lambda: {"agent_id": "agent-sender"}


def _send_body() -> dict:
    return {
        "from_agent": "agent-sender",
        "target_agent": "agent-target",
        "message": {"text": "hello"},
        "priority": "normal",
    }


def _internal_send_body() -> dict:
    return {
        "from_agent": "system:agentplanet-backend",
        "target_agent": "agent-target",
        "message": {"text": "hello from backend"},
        "priority": "normal",
    }


# --------------------------------------------------------------------------- #
# /communication/send → 403 on PolicyRejected
# --------------------------------------------------------------------------- #


class TestPublicSendPolicyRejected:
    def test_returns_403(self, stub_metrics, stub_message_service, stub_audit):
        _wire_send_dep_overrides(stub_metrics, stub_message_service, stub_audit)

        with patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/send",
                    json=_send_body(),
                    # NOTE: header value must be pure ASCII — httpx
                    # encodes headers as latin-1, so any non-ASCII char
                    # (e.g. an em-dash in a comment-like string) raises
                    # UnicodeEncodeError before the request is sent.
                    # The actual value is irrelevant: the auth dependency
                    # is overridden in _wire_send_dep_overrides.
                    headers={"X-API-Key": "irrelevant-auth-overridden"},
                )

        assert r.status_code == 403, r.text

    def test_response_body_carries_structured_detail(
        self, stub_metrics, stub_message_service, stub_audit
    ):
        """Pinning the wire shape: clients branch on
        ``error_code == "communication_rejected"`` without parsing
        free-form strings; per-rejection structured context lives
        under ``details`` (``reason`` / ``reject_reason``).

        Phase 2 review v2 P1 #11 migrated this route from the legacy
        nested ``{"detail": {...}}`` shape to the flat ACN error
        schema (``{error_code, message, details, request_id}``) —
        SDKs that pinned the old nested shape need to upgrade per
        ``docs/features/acn-error-schema.md``.
        """
        _wire_send_dep_overrides(stub_metrics, stub_message_service, stub_audit)

        with patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/send",
                    json=_send_body(),
                    headers={"X-API-Key": "x"},
                )

        body = r.json()
        # Flat ACN error schema (P1 #11).
        assert {"error_code", "message", "details", "request_id"} <= body.keys()
        assert body["error_code"] == "communication_rejected"
        assert body["details"] == {
            "reason": "policy_closed",
            "reject_reason": "On vacation until 2026-05",
        }
        # Defensive: the legacy ``detail`` field is gone — no nested
        # wrapper carrying the same payload.
        assert "detail" not in body

    def test_metrics_record_rejected_status(
        self, stub_metrics, stub_message_service, stub_audit
    ):
        """``inc_message_count`` is the operator-facing signal for
        traffic shape. Pinning that policy rejections are tagged
        ``status="rejected"`` so dashboards can split them out from
        ``error`` and ``not_found`` without grepping logs."""
        _wire_send_dep_overrides(stub_metrics, stub_message_service, stub_audit)

        with patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                client.post(
                    "/api/v1/communication/send",
                    json=_send_body(),
                    headers={"X-API-Key": "x"},
                )

        stub_metrics.inc_message_count.assert_awaited_once()
        kwargs = stub_metrics.inc_message_count.await_args.kwargs
        assert kwargs["status"] == "rejected"

    def test_metrics_record_fine_grained_policy_rejection(
        self, stub_metrics, stub_message_service, stub_audit
    ):
        """Pin the new fine-grained metric introduced in Step 2.5:
        ``acn_messages_rejected_by_policy_total{path=single,reason=...}``.

        This is the dimension operators need to distinguish a single-
        send spike from a broadcast or internal-channel spike — the
        coarser ``messages_total{status=rejected}`` cannot tell those
        apart."""
        _wire_send_dep_overrides(stub_metrics, stub_message_service, stub_audit)

        with patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                client.post(
                    "/api/v1/communication/send",
                    json=_send_body(),
                    headers={"X-API-Key": "x"},
                )

        # Find the inc_counter call that targets the policy-rejected
        # series. There may be other inc_counter calls in the same
        # request (none today, but defensively don't assume call
        # ordering).
        rejection_calls = [
            c
            for c in stub_metrics.inc_counter.await_args_list
            if c.args and c.args[0] == "messages_rejected_by_policy_total"
        ]
        assert len(rejection_calls) == 1, (
            f"expected exactly one inc to messages_rejected_by_policy_total, "
            f"got {[c.args for c in stub_metrics.inc_counter.await_args_list]}"
        )
        labels = rejection_calls[0].kwargs["labels"]
        assert labels == {"path": "single", "reason": "policy_closed"}

    def test_audit_records_message_rejected_event(
        self, stub_metrics, stub_message_service, stub_audit
    ):
        """Pin the audit signal: single-send rejections must emit
        ``AuditEventType.MESSAGE_REJECTED`` with the recipient's
        ``reject_reason`` carried through verbatim. This is the
        forensic record analysts use to investigate
        "why did agent X stop receiving traffic from agent Y" after
        a policy change."""
        _wire_send_dep_overrides(stub_metrics, stub_message_service, stub_audit)

        with patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                client.post(
                    "/api/v1/communication/send",
                    json=_send_body(),
                    headers={"X-API-Key": "x"},
                )

        stub_audit.log_event.assert_awaited_once()
        kwargs = stub_audit.log_event.await_args.kwargs
        assert kwargs["event_type"] == AuditEventType.MESSAGE_REJECTED
        assert kwargs["actor_id"] == "agent-sender"
        assert kwargs["actor_type"] == "agent"
        assert kwargs["target_id"] == "agent-target"
        assert kwargs["details"]["reason"] == "policy_closed"
        assert kwargs["details"]["reject_reason"] == "On vacation until 2026-05"
        assert kwargs["details"]["path"] == "single"


# --------------------------------------------------------------------------- #
# /communication/internal/send → 403 (defensive — should be unreachable)
# --------------------------------------------------------------------------- #


class TestInternalSendPolicyRejectedDefensive:
    """The internal channel exempts ``system:*`` senders at the
    PolicyCheckService layer, so this path *should* never see a
    PolicyRejected. The test pins the defensive 403 mapping anyway —
    if a future refactor accidentally drops the exemption, the
    failure surfaces as a clear 403 with the same structured detail
    rather than masquerading as a 500."""

    def test_returns_structured_403(
        self, stub_metrics, stub_message_service, stub_audit
    ):
        app.dependency_overrides[get_metrics] = lambda: stub_metrics
        app.dependency_overrides[get_message_service] = lambda: stub_message_service
        app.dependency_overrides[get_audit] = lambda: stub_audit

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ), patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/internal/send",
                    json=_internal_send_body(),
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        assert r.status_code == 403, r.text
        body = r.json()
        # Flat ACN error schema (P1 #11) — same shape as the public
        # ``/send`` route to keep the SDK contract uniform across
        # the two send surfaces.
        assert {"error_code", "message", "details", "request_id"} <= body.keys()
        assert body["error_code"] == "communication_rejected"
        assert body["details"] == {
            "reason": "policy_closed",
            "reject_reason": "On vacation until 2026-05",
        }
        assert "detail" not in body

    def test_audit_marks_internal_path_unexpected(
        self, stub_metrics, stub_message_service, stub_audit
    ):
        """Internal channel hitting MESSAGE_REJECTED is *defensive*: it
        should never happen because PolicyCheckService exempts
        ``system:*`` senders. When it does happen — typically because
        someone changed the exemption rule and broke the invariant —
        the audit event must record both ``path="internal"`` AND
        ``unexpected=True`` so analysts can write a single alert that
        fires the moment the invariant breaks rather than waiting for
        a downstream symptom."""
        app.dependency_overrides[get_metrics] = lambda: stub_metrics
        app.dependency_overrides[get_message_service] = lambda: stub_message_service
        app.dependency_overrides[get_audit] = lambda: stub_audit

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ), patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                client.post(
                    "/api/v1/communication/internal/send",
                    json=_internal_send_body(),
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        stub_audit.log_event.assert_awaited_once()
        kwargs = stub_audit.log_event.await_args.kwargs
        assert kwargs["event_type"] == AuditEventType.MESSAGE_REJECTED
        assert kwargs["actor_type"] == "system"
        assert kwargs["details"]["path"] == "internal"
        assert kwargs["details"]["unexpected"] is True

        # Fine-grained metric label: path="internal" lets the operator
        # alert specifically on the system-channel invariant breaking.
        rejection_calls = [
            c
            for c in stub_metrics.inc_counter.await_args_list
            if c.args and c.args[0] == "messages_rejected_by_policy_total"
        ]
        assert len(rejection_calls) == 1
        assert rejection_calls[0].kwargs["labels"] == {
            "path": "internal",
            "reason": "policy_closed",
        }
