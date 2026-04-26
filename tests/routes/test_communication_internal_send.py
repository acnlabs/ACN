"""Tests for ``POST /api/v1/communication/internal/send`` (14.5-1 fix).

Background — why this endpoint exists:
    The agentplanet backend dispatches chat-mention notifications to ACN
    agents. Pre-fix, it called the public ``/communication/send`` with no
    auth at all → 401. The proper public path requires a per-agent API key
    AND enforces ``agent_info.agent_id == body.from_agent`` (anti-spoofing),
    which forces the backend to either (a) register a "ghost agent"
    (option A — pollutes ACN's agent pool) or (b) introduce a dedicated
    internal channel guarded by ``X-Internal-Token`` and a reserved
    ``system:<slug>`` namespace for ``from_agent`` (option B — chosen).

    These tests pin the security-critical invariants of the new endpoint:
      * Token-gated: missing/wrong ``X-Internal-Token`` is rejected.
      * Namespace-gated: ``from_agent`` outside ``system:<slug>`` is rejected.
      * Audit-tagged: traffic on this channel is tagged ``actor_type="system"``
        so analysts can distinguish it from peer-agent traffic on the same
        ``MESSAGE_SENT`` event stream.
      * Functional parity: when both gates pass, the service-layer call is
        identical to the public ``/send`` (so we don't accidentally
        bypass message persistence, fan-out, or metrics).

Why we don't test rate-limiting here:
    The 600/min limit is exercised by the same SlowAPI machinery already
    covered for /send and /broadcast — duplicating that test would just
    re-validate slowapi rather than this endpoint's auth/namespace logic.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException
from acn.routes.dependencies import (
    get_audit,
    get_message_service,
    get_metrics,
    limiter,
)

# A token that satisfies the ≥32-char rule enforced by Settings validators —
# we patch it in via ``settings.internal_api_token`` so the real
# ``verify_internal_token`` dependency runs end-to-end (we deliberately do
# NOT override that dep — we want to test the token comparison itself).
VALID_INTERNAL_TOKEN = "test-internal-token-min-32-chars-padding"


@pytest.fixture
def stub_metrics():
    m = AsyncMock()
    m.inc_message_count = AsyncMock()
    return m


@pytest.fixture
def stub_message_service():
    svc = AsyncMock()
    svc.send_message = AsyncMock(
        return_value={"message_id": "msg-internal-1", "status": "sent"}
    )
    return svc


@pytest.fixture
def stub_audit():
    a = AsyncMock()
    a.log_event = AsyncMock()
    return a


@pytest.fixture(autouse=True)
def _reset_overrides_and_limiter():
    """Each test runs against a clean dependency-override slate, and we
    disable slowapi's rate limiter so a flaky CI doesn't trip 429 from
    cross-test request bursts on the same key."""
    # SlowAPI looks at ``limiter.enabled`` for the kill-switch.
    limiter.enabled = False
    yield
    limiter.enabled = True
    app.dependency_overrides.clear()


def _wire_dep_overrides(metrics, message_service, audit) -> None:
    app.dependency_overrides[get_metrics] = lambda: metrics
    app.dependency_overrides[get_message_service] = lambda: message_service
    app.dependency_overrides[get_audit] = lambda: audit


def _good_body(from_agent: str = "system:agentplanet-backend") -> dict:
    return {
        "from_agent": from_agent,
        "target_agent": "agent-target-uuid",
        "message": {"text": "hello from backend"},
        "priority": "normal",
    }


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


class TestHappyPath:
    def test_valid_token_and_namespace_succeeds(
        self, stub_metrics, stub_message_service, stub_audit
    ):
        """The 'normal' case the backend exercises every chat mention."""
        _wire_dep_overrides(stub_metrics, stub_message_service, stub_audit)

        # Patch Message at the route's import site to skip a2a Pydantic
        # validation — we're testing the route's contract with the
        # message_service, not the message envelope.
        stub_msg = MagicMock()
        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ), patch("acn.routes.communication.Message", return_value=stub_msg):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/internal/send",
                    json=_good_body(),
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        assert r.status_code == 200, r.text
        assert r.json()["message_id"] == "msg-internal-1"

        # Service-level call must be identical in shape to /send so we
        # don't accidentally bypass any persistence/routing path.
        stub_message_service.send_message.assert_awaited_once()
        kwargs = stub_message_service.send_message.await_args.kwargs
        assert kwargs["from_agent_id"] == "system:agentplanet-backend"
        assert kwargs["to_agent_id"] == "agent-target-uuid"
        assert kwargs["priority"] == "normal"

    def test_audit_records_actor_type_system(
        self, stub_metrics, stub_message_service, stub_audit
    ):
        """SECURITY-CRITICAL: internal-channel traffic must be tagged
        ``actor_type='system'`` so analysts can distinguish it from
        peer-agent traffic. If this regresses to 'agent', incident
        forensics will conflate backend service calls with real
        agent-to-agent messages and miss anomalies."""
        _wire_dep_overrides(stub_metrics, stub_message_service, stub_audit)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ), patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/internal/send",
                    json=_good_body(),
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        assert r.status_code == 200
        stub_audit.log_event.assert_awaited_once()
        audit_kwargs = stub_audit.log_event.await_args.kwargs
        assert audit_kwargs["actor_type"] == "system", (
            "internal channel must record actor_type='system' so "
            "audit analysts can separate backend service traffic from "
            "peer-agent traffic"
        )
        assert audit_kwargs["actor_id"] == "system:agentplanet-backend"
        assert audit_kwargs["target_id"] == "agent-target-uuid"


# --------------------------------------------------------------------------- #
# Token gate (X-Internal-Token)
# --------------------------------------------------------------------------- #


class TestTokenGate:
    def test_missing_header_rejected(self, stub_metrics, stub_message_service, stub_audit):
        """No ``X-Internal-Token`` at all → FastAPI's required-header
        machinery returns 422 (header is declared as ``Header(...)`` =
        required). Service must not be touched."""
        _wire_dep_overrides(stub_metrics, stub_message_service, stub_audit)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/internal/send",
                    json=_good_body(),
                )

        assert r.status_code == 422
        stub_message_service.send_message.assert_not_awaited()

    def test_wrong_token_rejected_with_403(
        self, stub_metrics, stub_message_service, stub_audit
    ):
        """Invalid token → 403 from ``verify_internal_token``. We
        explicitly assert the 403 (not 401) because audit dashboards
        already alert on 403 for this code path."""
        _wire_dep_overrides(stub_metrics, stub_message_service, stub_audit)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/internal/send",
                    json=_good_body(),
                    headers={"X-Internal-Token": "wrong-token-but-long-enough-padding"},
                )

        assert r.status_code == 403
        stub_message_service.send_message.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Namespace gate (from_agent must match ^system:[A-Za-z0-9_-]{1,64}$)
# --------------------------------------------------------------------------- #


class TestNamespaceGate:
    """The whole point of the internal channel is that it bypasses the
    per-agent spoofing check — so the ``from_agent`` namespace gate
    is the *only* thing keeping a leaked internal token from being
    used to forge messages from any registered agent.

    These tests pin every "almost valid but actually bad" shape we
    could think of, on the principle that auth bypasses fail open
    and the failure modes here are existential.
    """

    @pytest.mark.parametrize(
        "bad_from_agent,reason",
        [
            ("agent-a", "no system: prefix at all"),
            ("system:", "empty slug"),
            ("system:foo bar", "space in slug"),
            ("system:foo:bar", "extra colon (would let attacker hint at sub-namespaces)"),
            ("system:" + "x" * 65, "slug exceeds 64-char cap"),
            ("System:agentplanet", "wrong case on prefix (regex is case-sensitive on purpose)"),
            ("Sys:agentplanet", "abbreviated prefix"),
            ("acn:550e8400-e29b-41d4-a716-446655440000", "real-looking ACN UUID id"),
            (" system:agentplanet", "leading whitespace"),
            ("system:agentplanet ", "trailing whitespace"),
            ("system:foo/bar", "slash — would smuggle a path component"),
            ("system:foo.bar", "dot — would smuggle an FQDN-style identity"),
        ],
    )
    def test_rejects_bad_from_agent(
        self,
        bad_from_agent: str,
        reason: str,
        stub_metrics,
        stub_message_service,
        stub_audit,
    ):
        _wire_dep_overrides(stub_metrics, stub_message_service, stub_audit)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/internal/send",
                    json=_good_body(from_agent=bad_from_agent),
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        assert r.status_code == 422, (
            f"namespace gate should have rejected {bad_from_agent!r} "
            f"({reason}), got {r.status_code}: {r.text}"
        )
        stub_message_service.send_message.assert_not_awaited(), (
            "service must not be called when namespace gate rejects — "
            "even a single send_message call past the gate could be a "
            "spoofing primitive if the gate ever silently degrades"
        )

    @pytest.mark.parametrize(
        "good_from_agent",
        [
            "system:agentplanet-backend",
            "system:a",
            "system:" + "x" * 64,
            "system:my_service",
            "system:my-service-v2",
            "system:Foo123_Bar-Baz",
        ],
    )
    def test_accepts_well_formed_system_slugs(
        self,
        good_from_agent: str,
        stub_metrics,
        stub_message_service,
        stub_audit,
    ):
        _wire_dep_overrides(stub_metrics, stub_message_service, stub_audit)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ), patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/internal/send",
                    json=_good_body(from_agent=good_from_agent),
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        assert r.status_code == 200, (
            f"namespace gate should have accepted {good_from_agent!r}, "
            f"got {r.status_code}: {r.text}"
        )


# --------------------------------------------------------------------------- #
# Service-layer error propagation
# --------------------------------------------------------------------------- #


class TestErrorPropagation:
    def test_target_agent_not_found_returns_404(
        self, stub_metrics, stub_message_service, stub_audit
    ):
        """Standard agent-not-found path — we map AgentNotFoundException to
        404 *and* increment metrics with status='not_found' so SLO
        dashboards can break out missing-target errors from generic 5xx."""
        stub_message_service.send_message = AsyncMock(
            side_effect=AgentNotFoundException("target-uuid")
        )
        _wire_dep_overrides(stub_metrics, stub_message_service, stub_audit)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ), patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/internal/send",
                    json=_good_body(),
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        assert r.status_code == 404
        stub_metrics.inc_message_count.assert_awaited_once()
        assert (
            stub_metrics.inc_message_count.await_args.kwargs["status"] == "not_found"
        )

    def test_unexpected_service_error_returns_500_with_metrics(
        self, stub_metrics, stub_message_service, stub_audit
    ):
        stub_message_service.send_message = AsyncMock(
            side_effect=RuntimeError("simulated downstream blowup")
        )
        _wire_dep_overrides(stub_metrics, stub_message_service, stub_audit)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ), patch("acn.routes.communication.Message", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/internal/send",
                    json=_good_body(),
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        assert r.status_code == 500
        stub_metrics.inc_message_count.assert_awaited_once()
        assert stub_metrics.inc_message_count.await_args.kwargs["status"] == "error"
