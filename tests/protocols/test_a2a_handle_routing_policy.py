"""Tests for A2A protocol handlers ↔ structured PolicyRejected.

Phase 1 review finding P1-2:
    ``_handle_routing`` and ``_handle_subnet_routing`` caught
    PolicyRejected via the generic ``except Exception`` branch and
    funnelled it into ``TaskState.failed`` with a free-form message
    like "Routing failed: <repr>". Clients of the A2A protocol path
    therefore had to substring-match the failure message to tell
    "denied by recipient policy" apart from "upstream 500", and the
    operator-controlled ``reject_reason`` string was at the mercy
    of whatever ``str(PolicyRejected)`` produced.

    The fix introduces ``_send_policy_rejected_status`` which emits
    ``TaskState.rejected`` (a real A2A spec state, not a string)
    with a ``DataPart`` containing the same shape
    ``/communication/send`` returns over HTTP:

        {"detail": "communication_rejected",
         "reason": "...",
         "reject_reason": "...",
         "target_id": "..."}

    This pins the contract: emitted state, emitted shape, fields
    actually populated, plus regression guards proving generic
    failures still go to ``failed`` (so the carve-out is specific).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from a2a.types import (
    DataPart,
    Message,
    Role,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
)

from acn.core.exceptions import PolicyRejected
from acn.protocols.a2a.server import ACNAgentExecutor


def _make_executor(*, route_side_effect=None, subnet_side_effect=None, metrics=None):
    """Build an executor where each downstream service is stubbed
    so tests can drive the handlers in isolation.

    ``metrics`` defaults to ``None`` — most tests are unconcerned
    with the counter, which is best-effort and exercised by the
    dedicated ``TestPolicyRejectedIncrementsMetric`` cases below.
    Tests that need to assert on counter writes pass a MagicMock
    explicitly.
    """
    registry = MagicMock()
    router = MagicMock()
    broadcast = MagicMock()
    subnet_manager = MagicMock()

    if route_side_effect is not None:
        router.route = AsyncMock(side_effect=route_side_effect)
    else:
        router.route = AsyncMock(return_value={"status": "ok"})

    if subnet_side_effect is not None:
        subnet_manager.forward_request = AsyncMock(side_effect=subnet_side_effect)
    else:
        subnet_manager.forward_request = AsyncMock(return_value={"status": "ok"})

    return ACNAgentExecutor(
        registry=registry,
        router=router,
        broadcast=broadcast,
        subnet_manager=subnet_manager,
        metrics=metrics,
    )


def _make_context(metadata: dict | None = None) -> MagicMock:
    """Minimal RequestContext stand-in. The handler reads
    ``message``, ``metadata``, ``task_id``, ``context_id``."""
    ctx = MagicMock()
    ctx.metadata = metadata or {}
    ctx.task_id = "task-1"
    ctx.context_id = "ctx-1"
    return ctx


def _routing_message(*, target_agent: str, content: str = "hi") -> Message:
    """Build the kind of A2A message ``_handle_routing`` expects:
    a DataPart carrying ``target_agent`` plus a TextPart for
    ``message_content``. Mirrors the executor's
    ``_extract_data_from_message`` extraction logic."""
    return Message(
        role=Role.user,
        message_id="msg-1",
        parts=[
            DataPart(data={"target_agent": target_agent}),
            TextPart(text=content),
        ],
    )


def _subnet_routing_message(*, subnet_id: str, agent_id: str) -> Message:
    return Message(
        role=Role.user,
        message_id="msg-1",
        parts=[
            DataPart(
                data={
                    "subnet_id": subnet_id,
                    "agent_id": agent_id,
                    "message": {"text": "hi"},
                }
            )
        ],
    )


def _make_event_queue() -> MagicMock:
    """EventQueue captures every status update; tests then assert on
    the **final** event's state and structured payload."""
    q = MagicMock()
    q.enqueue_event = AsyncMock()
    return q


def _final_status_event(eq: MagicMock) -> TaskStatusUpdateEvent:
    """Return the last final=True status event the handler enqueued."""
    final_events = [
        call.args[0]
        for call in eq.enqueue_event.call_args_list
        if isinstance(call.args[0], TaskStatusUpdateEvent) and call.args[0].final
    ]
    assert final_events, "handler did not enqueue a final status event"
    return final_events[-1]


def _data_payload(event: TaskStatusUpdateEvent) -> dict[str, Any]:
    """Pull the structured DataPart payload out of the event's
    Message. The carved-out PolicyRejected path is supposed to use
    DataPart, not TextPart — that's part of the contract being pinned."""
    msg = event.status.message
    assert msg is not None, "policy-rejected status must carry a message"
    parts = msg.parts
    assert parts, "policy-rejected status message has no parts"
    actual = parts[0].root if hasattr(parts[0], "root") else parts[0]
    assert isinstance(actual, DataPart), (
        "policy rejection should use DataPart so consumers can parse the "
        "rejection deterministically — got TextPart instead"
    )
    return actual.data


# --------------------------------------------------------------------------- #
# _handle_routing — the original P1-2 site
# --------------------------------------------------------------------------- #


class TestHandleRoutingPolicyRejected:
    @pytest.mark.asyncio
    async def test_emits_taskstate_rejected_not_failed(self):
        """Pinning the spec-state choice. ``failed`` would mix
        denied-by-policy into the same bucket as upstream 500s,
        which is exactly the symptom the fix exists to address."""
        executor = _make_executor(
            route_side_effect=PolicyRejected(
                recipient_id="closed-agent",
                reason="policy_closed",
                reject_reason="DND",
            )
        )
        eq = _make_event_queue()

        await executor._handle_routing(
            _routing_message(target_agent="closed-agent"),
            _make_context(),
            eq,
        )

        final = _final_status_event(eq)
        assert final.status.state == TaskState.rejected, (
            "PolicyRejected must surface as TaskState.rejected, not failed"
        )

    @pytest.mark.asyncio
    async def test_emits_structured_data_payload(self):
        executor = _make_executor(
            route_side_effect=PolicyRejected(
                recipient_id="closed-agent",
                reason="policy_closed",
                reject_reason="On vacation",
            )
        )
        eq = _make_event_queue()

        await executor._handle_routing(
            _routing_message(target_agent="closed-agent"),
            _make_context(),
            eq,
        )

        payload = _data_payload(_final_status_event(eq))
        # The shape mirrors `/communication/send` 403 detail body.
        assert payload == {
            "detail": "communication_rejected",
            "reason": "policy_closed",
            "reject_reason": "On vacation",
            "target_id": "closed-agent",
        }

    @pytest.mark.asyncio
    async def test_no_reject_reason_yields_none_in_payload(self):
        """Operators are not required to set a reject_reason. Pin
        that ``None`` is preserved (rather than coerced to ``""``
        or omitted) so consumers have a single, predictable shape."""
        executor = _make_executor(
            route_side_effect=PolicyRejected(
                recipient_id="closed-agent",
                reason="policy_closed",
                reject_reason=None,
            )
        )
        eq = _make_event_queue()

        await executor._handle_routing(
            _routing_message(target_agent="closed-agent"),
            _make_context(),
            eq,
        )

        payload = _data_payload(_final_status_event(eq))
        assert payload["reject_reason"] is None
        assert payload["reason"] == "policy_closed"

    @pytest.mark.asyncio
    async def test_generic_exception_still_goes_to_failed(self):
        """Regression guard: the carve-out must be **specific to
        PolicyRejected**. A real upstream 500 / timeout still
        warrants ``TaskState.failed`` so dashboards keep
        differentiating denied vs broken."""
        executor = _make_executor(route_side_effect=RuntimeError("upstream blew up"))
        eq = _make_event_queue()

        await executor._handle_routing(
            _routing_message(target_agent="some-agent"),
            _make_context(),
            eq,
        )

        final = _final_status_event(eq)
        assert final.status.state == TaskState.failed
        # Free-form text path is fine here — no DataPart contract
        # for generic failures.
        msg = final.status.message
        assert msg is not None
        text_parts = [p.root if hasattr(p, "root") else p for p in msg.parts]
        assert any(isinstance(p, TextPart) and "Routing failed" in p.text for p in text_parts)


# --------------------------------------------------------------------------- #
# _handle_subnet_routing — same contract, different code path
# --------------------------------------------------------------------------- #


class TestHandleSubnetRoutingPolicyRejected:
    """Subnet routing is the WebSocket fan-in path; pre-fix it shared
    the same generic-Exception handling as point-to-point routing.
    The structured rejection contract must apply uniformly across
    both — otherwise A2A clients have to special-case which action
    they invoked when interpreting failures."""

    @pytest.mark.asyncio
    async def test_emits_taskstate_rejected_with_structured_payload(self):
        executor = _make_executor(
            subnet_side_effect=PolicyRejected(
                recipient_id="closed-agent",
                reason="policy_closed",
                reject_reason="WS off",
            )
        )
        eq = _make_event_queue()

        await executor._handle_subnet_routing(
            _subnet_routing_message(subnet_id="net-1", agent_id="closed-agent"),
            _make_context(),
            eq,
        )

        final = _final_status_event(eq)
        assert final.status.state == TaskState.rejected
        payload = _data_payload(final)
        assert payload == {
            "detail": "communication_rejected",
            "reason": "policy_closed",
            "reject_reason": "WS off",
            "target_id": "closed-agent",
        }

    @pytest.mark.asyncio
    async def test_unknown_mode_reason_propagates(self):
        """Pin that ``policy_unknown_mode`` (the fail-closed branch
        for malformed policy dicts) round-trips faithfully. This
        is the second documented ``reason`` value besides
        ``policy_closed`` and clients may want to surface it
        differently."""
        executor = _make_executor(
            subnet_side_effect=PolicyRejected(
                recipient_id="closed-agent",
                reason="policy_unknown_mode",
                reject_reason=None,
            )
        )
        eq = _make_event_queue()

        await executor._handle_subnet_routing(
            _subnet_routing_message(subnet_id="net-1", agent_id="closed-agent"),
            _make_context(),
            eq,
        )

        payload = _data_payload(_final_status_event(eq))
        assert payload["reason"] == "policy_unknown_mode"

    @pytest.mark.asyncio
    async def test_generic_exception_still_goes_to_failed(self):
        executor = _make_executor(
            subnet_side_effect=RuntimeError("ws disconnected")
        )
        eq = _make_event_queue()

        await executor._handle_subnet_routing(
            _subnet_routing_message(subnet_id="net-1", agent_id="some-agent"),
            _make_context(),
            eq,
        )

        final = _final_status_event(eq)
        assert final.status.state == TaskState.failed


# --------------------------------------------------------------------------- #
# Metric inc on policy rejection (v2 review R1)
# --------------------------------------------------------------------------- #


class TestPolicyRejectedIncrementsMetric:
    """The A2A protocol entry is the second-highest-volume rejection
    surface (after the proxy paths). Without a metric inc, ops
    cannot tell ``policy_closed`` denials apart from real upstream
    failures using only Prometheus — they'd have to scrape logs.
    Pin the dimension contract: ``path="a2a"``, ``reason`` mirrors
    the PolicyRejected.reason."""

    @pytest.mark.asyncio
    async def test_handle_routing_inc_counter_on_rejection(self):
        metrics = MagicMock()
        metrics.inc_counter = AsyncMock()
        executor = _make_executor(
            route_side_effect=PolicyRejected(
                recipient_id="closed-agent",
                reason="policy_closed",
                reject_reason="DND",
            ),
            metrics=metrics,
        )
        eq = _make_event_queue()

        await executor._handle_routing(
            _routing_message(target_agent="closed-agent"),
            _make_context(),
            eq,
        )

        metrics.inc_counter.assert_awaited_once_with(
            "messages_rejected_by_policy_total",
            labels={"path": "a2a", "reason": "policy_closed"},
        )

    @pytest.mark.asyncio
    async def test_handle_subnet_routing_inc_counter_on_rejection(self):
        """Subnet routing shares the same ``path="a2a"`` label —
        operators almost never need to differentiate the
        sub-handler, and keeping label cardinality low is more
        valuable than per-action slicing."""
        metrics = MagicMock()
        metrics.inc_counter = AsyncMock()
        executor = _make_executor(
            subnet_side_effect=PolicyRejected(
                recipient_id="closed-agent",
                reason="policy_unknown_mode",
                reject_reason=None,
            ),
            metrics=metrics,
        )
        eq = _make_event_queue()

        await executor._handle_subnet_routing(
            _subnet_routing_message(subnet_id="net-1", agent_id="closed-agent"),
            _make_context(),
            eq,
        )

        metrics.inc_counter.assert_awaited_once_with(
            "messages_rejected_by_policy_total",
            labels={"path": "a2a", "reason": "policy_unknown_mode"},
        )

    @pytest.mark.asyncio
    async def test_metric_inc_failure_does_not_break_rejected_status(self):
        """Best-effort observability: even if the counter backend
        is unreachable, the rejection MUST still flow through to
        TaskState.rejected. Otherwise a Redis hiccup during a
        policy denial would manifest as a TaskState.failed mixed
        in with real upstream errors — exactly the noise we just
        fixed in P1-2."""
        metrics = MagicMock()
        metrics.inc_counter = AsyncMock(side_effect=RuntimeError("redis down"))
        executor = _make_executor(
            route_side_effect=PolicyRejected(
                recipient_id="closed-agent",
                reason="policy_closed",
            ),
            metrics=metrics,
        )
        eq = _make_event_queue()

        await executor._handle_routing(
            _routing_message(target_agent="closed-agent"),
            _make_context(),
            eq,
        )

        final = _final_status_event(eq)
        assert final.status.state == TaskState.rejected, (
            "metric inc failure must not derail the rejected wire shape"
        )

    @pytest.mark.asyncio
    async def test_no_metrics_instance_skips_inc_silently(self):
        """Backward compat: tests / partial-bring-up cases that
        don't wire metrics must not break. Production lifespan
        always installs one."""
        executor = _make_executor(
            route_side_effect=PolicyRejected(
                recipient_id="closed-agent",
                reason="policy_closed",
            ),
            metrics=None,
        )
        eq = _make_event_queue()

        # Should complete without AttributeError or TypeError.
        await executor._handle_routing(
            _routing_message(target_agent="closed-agent"),
            _make_context(),
            eq,
        )
        # And still produce the correct rejection status.
        final = _final_status_event(eq)
        assert final.status.state == TaskState.rejected

    @pytest.mark.asyncio
    async def test_generic_failure_does_not_inc_policy_metric(self):
        """Negative-side guard: the policy counter must only fire
        on PolicyRejected, not on every failure path. Otherwise
        operators would see ``policy_closed`` blowing up during a
        legitimate upstream outage and chase the wrong root cause."""
        metrics = MagicMock()
        metrics.inc_counter = AsyncMock()
        executor = _make_executor(
            route_side_effect=RuntimeError("upstream blew up"),
            metrics=metrics,
        )
        eq = _make_event_queue()

        await executor._handle_routing(
            _routing_message(target_agent="some-agent"),
            _make_context(),
            eq,
        )

        metrics.inc_counter.assert_not_called()
