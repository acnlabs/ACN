"""HTTP broadcast → BroadcastService convergence contract.

Phase 2 Group C #9 / review v2 P1 #7 (see
``docs/features/acn-communication-economic-model.md`` L608–L614)
collapsed the previous double-track:

* HTTP ``/communication/broadcast`` and ``/broadcast-by-tag`` used
  ``MessageService.broadcast_message`` (sequential, no
  ``broadcast_id``, no Redis log persistence).
* A2A protocol entry used ``BroadcastService.send`` /
  ``send_by_tag`` (real ``asyncio.gather`` parallelism +
  Redis-persisted ``broadcast_id`` + aggregated stats).

After the convergence, both HTTP and A2A entries flow through
``BroadcastService``. This file pins three layers of contract so
the convergence cannot regress:

1. **Architectural**: ``MessageService.broadcast_message`` no longer
   exists, and the HTTP routes call ``BroadcastService.broadcast``
   with the correct kwargs (regression guard against re-introducing
   the dead path or wiring the wrong service).
2. **Wire-level**: HTTP responses include the new top-level
   ``broadcast_id`` *and* keep the legacy ``responses[]`` shape
   that existing SDK clients parse (backward-compat).
3. **Adapter**: the ``_broadcast_result_to_http_responses`` helper
   correctly maps every per-target shape ``BroadcastService``
   produces back to the historical ``status`` taxonomy
   (``success`` / ``rejected`` / ``failed``) so the wire contract
   stays stable across this refactor.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.infrastructure.messaging.broadcast_service import BroadcastResult
from acn.routes.communication import _broadcast_result_to_http_responses
from acn.routes.dependencies import (
    get_broadcast,
    get_metrics,
    limiter,
    verify_agent_api_key,
)
from acn.services.message_service import MessageService

# --------------------------------------------------------------------------- #
# Architectural guard — the deleted method must not come back
# --------------------------------------------------------------------------- #


class TestMessageServiceNoLongerHasBroadcast:
    """If anything in ``MessageService`` ever re-grows a
    ``broadcast_message`` attribute, the convergence has been
    silently undone — fail fast and loud."""

    def test_message_service_class_has_no_broadcast_message(self):
        assert not hasattr(MessageService, "broadcast_message"), (
            "MessageService.broadcast_message was removed by Phase 2 "
            "Group C #9. HTTP broadcast routes must call "
            "BroadcastService.broadcast instead. Re-adding this method "
            "would re-introduce the dead double-track."
        )

    def test_message_service_class_has_no_strategy_field_handler(self):
        """The ``strategy`` field on the legacy method was a known
        no-op (only ``best_effort`` had real semantics, the others
        were aliases). Pinning that no resurrected helper accepts
        a ``strategy`` kwarg either."""
        for attr in dir(MessageService):
            if "broadcast" in attr.lower():
                pytest.fail(
                    f"MessageService.{attr} should not exist after "
                    f"Phase 2 Group C #9 convergence — broadcast "
                    f"belongs in BroadcastService."
                )


# --------------------------------------------------------------------------- #
# Wire-level guards — HTTP responses + service call kwargs
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    """Bypass slowapi for the contract tests — we don't care about
    rate limiting here, we care about the routing contract."""
    was = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = was


def _make_agent_info(agent_id: str = "agent-sender") -> dict:
    return {"agent_id": agent_id, "owner": "user-1"}


def _stub_broadcast_service(
    *, broadcast_return: BroadcastResult,
):
    """Build an AsyncMock that mirrors the BroadcastService surface
    relevant to the HTTP routes.

    Only the unified ``broadcast()`` method is wired with a return
    value — the route should never call ``send`` / ``send_by_tag``
    / ``send_to_project`` directly (those are A2A-internal). If a
    future regression makes the route reach into a different
    method, the resulting AttributeError or AsyncMock-default
    return value will surface in the test assertions."""
    svc = AsyncMock()
    svc.broadcast = AsyncMock(return_value=broadcast_return)
    return svc


def _stub_metrics():
    m = AsyncMock()
    m.inc_counter = AsyncMock()
    m.inc_message_count = AsyncMock()
    return m


class TestBroadcastRouteUsesBroadcastService:
    """HTTP ``/communication/broadcast`` must call
    ``BroadcastService.broadcast`` with the correct kwargs and
    return ``broadcast_id`` at the top level."""

    def test_subnet_broadcast_calls_broadcast_with_subnet_id(self):
        broadcast_svc = _stub_broadcast_service(
            broadcast_return=BroadcastResult(
                broadcast_id="bcast-001",
                total=2,
                success=2,
                failed=0,
                results={
                    "agent-b": {"status": "inbox", "route_id": "r1"},
                    "agent-c": {"status": "inbox", "route_id": "r2"},
                },
            )
        )
        metrics = _stub_metrics()

        app.dependency_overrides[get_broadcast] = lambda: broadcast_svc
        app.dependency_overrides[get_metrics] = lambda: metrics
        app.dependency_overrides[verify_agent_api_key] = lambda: _make_agent_info("agent-a")

        try:
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/broadcast",
                    json={
                        "from_agent": "agent-a",
                        "message": {"text": "hello"},
                        "target_subnet": "subnet-eu",
                    },
                )
        finally:
            app.dependency_overrides.clear()

        assert r.status_code == 200
        body = r.json()
        # 1. Top-level broadcast_id is the convergence's headline change.
        assert body["broadcast_id"] == "bcast-001", (
            "HTTP /broadcast must surface the BroadcastService-issued "
            "broadcast_id at the response root for traceability."
        )
        # 2. Legacy ``responses[]`` list-shape is preserved.
        assert isinstance(body["responses"], list)
        assert len(body["responses"]) == 2
        for entry in body["responses"]:
            assert "agent_id" in entry, (
                "responses[] must keep the agent_id-IN-item shape "
                "for backward-compat with existing SDK parsers."
            )
        # 3. The route called BroadcastService.broadcast, not anything else.
        broadcast_svc.broadcast.assert_awaited_once()
        kwargs = broadcast_svc.broadcast.await_args.kwargs
        assert kwargs["from_agent"] == "agent-a"
        assert kwargs["subnet_id"] == "subnet-eu"
        assert kwargs.get("tags") is None
        assert kwargs.get("target_agents") is None

    def test_tag_broadcast_calls_broadcast_with_tags(self):
        broadcast_svc = _stub_broadcast_service(
            broadcast_return=BroadcastResult(
                broadcast_id="bcast-tag-001",
                total=1,
                success=1,
                failed=0,
                results={"agent-x": {"status": "inbox", "route_id": "rx"}},
            )
        )
        metrics = _stub_metrics()

        app.dependency_overrides[get_broadcast] = lambda: broadcast_svc
        app.dependency_overrides[get_metrics] = lambda: metrics
        app.dependency_overrides[verify_agent_api_key] = lambda: _make_agent_info("agent-a")

        try:
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/broadcast-by-tag",
                    json={
                        "from_agent": "agent-a",
                        "tags": ["frontend", "review"],
                        "message": {"text": "design review please"},
                    },
                )
        finally:
            app.dependency_overrides.clear()

        assert r.status_code == 200
        body = r.json()
        assert body["broadcast_id"] == "bcast-tag-001"
        assert body["tags"] == ["frontend", "review"]
        broadcast_svc.broadcast.assert_awaited_once()
        kwargs = broadcast_svc.broadcast.await_args.kwargs
        assert kwargs["tags"] == ["frontend", "review"]
        assert kwargs.get("subnet_id") is None
        assert kwargs.get("target_agents") is None

    def test_strategy_is_case_insensitive(self):
        """SDKs occasionally send uppercase strategies (matches the
        Python enum *member* name rather than its *value*). The
        deleted ``MessageService.broadcast_message`` was silently
        permissive about case (``if strategy != "best_effort"``
        treated ``"BEST_EFFORT"`` as non-best-effort), and the
        convergence's strict ``BroadcastStrategy(body.strategy)``
        would have made that a wire break. The route normalises
        via ``.lower()`` so any of these spellings work — covers
        SDKs that used the old shape and any new ones that copy
        the StrEnum member name verbatim. P2-2 in the 9fb38b9 audit.
        """
        broadcast_svc = _stub_broadcast_service(
            broadcast_return=BroadcastResult(
                broadcast_id="bcast-case", total=0, success=0, failed=0, results={},
            )
        )
        metrics = _stub_metrics()

        app.dependency_overrides[get_broadcast] = lambda: broadcast_svc
        app.dependency_overrides[get_metrics] = lambda: metrics
        app.dependency_overrides[verify_agent_api_key] = lambda: _make_agent_info("agent-a")

        try:
            with TestClient(app) as client:
                # Mixed-case + all-caps both work; the route's .lower()
                # normalisation feeds either into BroadcastStrategy.
                for strategy_input in ("PARALLEL", "Best_Effort", "Sequential"):
                    r = client.post(
                        "/api/v1/communication/broadcast",
                        json={
                            "from_agent": "agent-a",
                            "message": {"text": "x"},
                            "strategy": strategy_input,
                        },
                    )
                    assert r.status_code == 200, (
                        f"strategy {strategy_input!r} should be accepted "
                        f"after .lower() normalisation; got {r.status_code} "
                        f"with detail {r.json()}"
                    )
        finally:
            app.dependency_overrides.clear()

        # All three calls reached the service (stub records every call).
        assert broadcast_svc.broadcast.await_count == 3

    def test_unknown_strategy_returns_422_before_calling_service(self):
        """Strategy validation happens at the route layer, not at the
        service layer. An unknown strategy must short-circuit to
        422 *before* the BroadcastService call so the operator
        sees a clean validation failure rather than a 500 from a
        downstream ``BroadcastStrategy(...)`` ValueError."""
        broadcast_svc = _stub_broadcast_service(
            broadcast_return=BroadcastResult(
                broadcast_id="never", total=0, success=0, failed=0, results={},
            )
        )
        metrics = _stub_metrics()

        app.dependency_overrides[get_broadcast] = lambda: broadcast_svc
        app.dependency_overrides[get_metrics] = lambda: metrics
        app.dependency_overrides[verify_agent_api_key] = lambda: _make_agent_info("agent-a")

        try:
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/broadcast",
                    json={
                        "from_agent": "agent-a",
                        "message": {"text": "x"},
                        "strategy": "definitely-not-a-strategy",
                    },
                )
        finally:
            app.dependency_overrides.clear()

        assert r.status_code == 422
        broadcast_svc.broadcast.assert_not_awaited()

    def test_from_agent_mismatch_returns_403_before_calling_service(self):
        """The authenticated agent must match ``from_agent``. The
        route checks this BEFORE calling BroadcastService — pinning
        that the spoofing guard is route-layer (where it belongs),
        not buried inside the service."""
        broadcast_svc = _stub_broadcast_service(
            broadcast_return=BroadcastResult(
                broadcast_id="never", total=0, success=0, failed=0, results={},
            )
        )
        metrics = _stub_metrics()

        app.dependency_overrides[get_broadcast] = lambda: broadcast_svc
        app.dependency_overrides[get_metrics] = lambda: metrics
        # Authenticate as a DIFFERENT agent than the body claims.
        app.dependency_overrides[verify_agent_api_key] = lambda: _make_agent_info("agent-impersonator")

        try:
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/broadcast",
                    json={
                        "from_agent": "agent-victim",
                        "message": {"text": "x"},
                    },
                )
        finally:
            app.dependency_overrides.clear()

        assert r.status_code == 403
        broadcast_svc.broadcast.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Adapter — _broadcast_result_to_http_responses shape mapping
# --------------------------------------------------------------------------- #


class TestResultToHttpResponseAdapter:
    """``_broadcast_result_to_http_responses`` is the only seam that
    bridges BroadcastService's ``dict[agent_id, per_target]`` output
    to the legacy HTTP ``responses[]`` list shape. Pin every
    per-target shape mapping so changing one branch can't silently
    break wire compat."""

    def test_inbox_short_circuit_becomes_success(self):
        """``router.route()`` returns ``{"status": "inbox", ...}``
        when the recipient is offline. The adapter wraps it under
        ``status: "success"`` (delivery accepted) — the actual
        delivery status lives in ``response.status``."""
        result = BroadcastResult(
            broadcast_id="b1", total=1, success=1, failed=0,
            results={"agent-x": {"status": "inbox", "route_id": "r1"}},
        )

        out = _broadcast_result_to_http_responses(result)

        assert out == [
            {
                "agent_id": "agent-x",
                "status": "success",
                "response": {"status": "inbox", "route_id": "r1"},
            }
        ]

    def test_policy_rejected_keeps_reason_and_reject_reason(self):
        """``status == "rejected"`` is forwarded verbatim with the
        ``reason`` / ``reject_reason`` fields that policy denials
        carry. Adapter must not strip or rewrite those."""
        result = BroadcastResult(
            broadcast_id="b1", total=1, success=0, failed=1,
            results={
                "agent-closed": {
                    "status": "rejected",
                    "reason": "policy_closed",
                    "reject_reason": "agent does not accept inbound",
                }
            },
        )

        out = _broadcast_result_to_http_responses(result)

        assert out == [
            {
                "agent_id": "agent-closed",
                "status": "rejected",
                "reason": "policy_closed",
                "reject_reason": "agent does not accept inbound",
            }
        ]

    def test_error_dict_becomes_failed(self):
        """Network / 5xx delivery failure maps to ``status: "failed"``
        (the historical shape) — distinct from policy rejection so
        existing dashboards keep their semantics."""
        result = BroadcastResult(
            broadcast_id="b1", total=1, success=0, failed=1,
            results={"agent-flake": {"error": "upstream gone"}},
        )

        out = _broadcast_result_to_http_responses(result)

        assert out == [
            {
                "agent_id": "agent-flake",
                "status": "failed",
                "error": "upstream gone",
            }
        ]

    def test_pydantic_model_response_dumps_via_model_dump(self):
        """Successful in-line delivery returns a Pydantic
        ``SendMessageResponse``. Adapter must ``model_dump()`` so
        the wire body is plain JSON, not a Pydantic-only repr."""
        sent = MagicMock()
        sent.model_dump = MagicMock(return_value={"message_id": "m1", "status": "ok"})

        result = BroadcastResult(
            broadcast_id="b1", total=1, success=1, failed=0,
            results={"agent-x": sent},
        )

        out = _broadcast_result_to_http_responses(result)

        assert out == [
            {
                "agent_id": "agent-x",
                "status": "success",
                "response": {"message_id": "m1", "status": "ok"},
            }
        ]
        sent.model_dump.assert_called_once()

    def test_mixed_fanout_aggregates_correctly(self):
        """Realistic mix: success + rejected + failed in one
        broadcast. Pin the union shape so the metric helper
        downstream still sees its expected ``status`` taxonomy."""
        sent = MagicMock()
        sent.model_dump = MagicMock(return_value={"message_id": "ok"})

        result = BroadcastResult(
            broadcast_id="bmix", total=3, success=1, failed=2,
            results={
                "agent-ok": sent,
                "agent-closed": {
                    "status": "rejected",
                    "reason": "policy_closed",
                    "reject_reason": "x",
                },
                "agent-down": {"error": "timeout"},
            },
        )

        out = _broadcast_result_to_http_responses(result)

        by_id = {entry["agent_id"]: entry for entry in out}
        assert by_id["agent-ok"]["status"] == "success"
        assert by_id["agent-closed"]["status"] == "rejected"
        assert by_id["agent-closed"]["reason"] == "policy_closed"
        assert by_id["agent-down"]["status"] == "failed"
        assert by_id["agent-down"]["error"] == "timeout"
