"""Unit tests for the broadcast policy-rejection metric helper.

Covers ``_record_broadcast_policy_rejections`` from
``acn.routes.communication`` in isolation — i.e. without spinning up
the FastAPI app / Redis lifespan. These tests pin two contracts:

1. Per-target rejections in a broadcast response set are summed
   into ``acn_messages_rejected_by_policy_total{path,reason}``,
   with one ``inc_counter`` call per ``reason`` bucket (not per
   rejected target). This is a deliberate batching choice — a
   fan-out of N closed recipients turns into O(unique_reasons)
   Redis ops rather than O(N).
2. Reasons that aren't part of the proposal's enumerated set
   (``policy_closed``, ``policy_unknown_mode``) collapse to
   ``policy_unknown_mode`` so an unexpected target response cannot
   inflate metric cardinality.

See "Phase 1 metrics + audit 落点 (Step 2.5)" in
docs/features/acn-communication-economic-model.md.
"""

from __future__ import annotations

from collections import Counter
from unittest.mock import AsyncMock

import pytest

from acn.routes.communication import _record_broadcast_policy_rejections


def _make_metrics() -> AsyncMock:
    m = AsyncMock()
    m.inc_counter = AsyncMock()
    return m


def _summarize_inc_counter_calls(metrics: AsyncMock) -> dict[tuple[str, str], int]:
    """Project ``inc_counter`` await args to a (path, reason) → value map.

    Lets each test assert against the *aggregated* effect rather
    than the call ordering, which is an implementation detail.
    """
    result: dict[tuple[str, str], int] = {}
    for call in metrics.inc_counter.await_args_list:
        if not call.args or call.args[0] != "messages_rejected_by_policy_total":
            continue
        labels = call.kwargs.get("labels", {})
        value = call.kwargs.get("value", 1)
        key = (labels["path"], labels["reason"])
        result[key] = result.get(key, 0) + value
    return result


@pytest.mark.asyncio
async def test_no_responses_does_nothing():
    """Empty list is the most common case (broadcast with zero
    targets, e.g. tag with no matches). Must be a no-op."""
    metrics = _make_metrics()
    await _record_broadcast_policy_rejections(metrics, [], "broadcast_target")
    metrics.inc_counter.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_rejections_does_nothing():
    """All-success fan-out must not touch the rejection metric.
    Pinning this guards against an off-by-one where the helper
    decides to record a zero-value increment 'for completeness' —
    which would make the metric series exist with value 0 and
    confuse sum() queries."""
    metrics = _make_metrics()
    responses = [
        {"agent_id": "a1", "status": "success"},
        {"agent_id": "a2", "status": "success"},
    ]
    await _record_broadcast_policy_rejections(metrics, responses, "broadcast_target")
    metrics.inc_counter.assert_not_awaited()


@pytest.mark.asyncio
async def test_aggregates_same_reason_into_single_call():
    """N targets rejected for the same reason → ONE inc_counter call
    with value=N. This is the cardinality / Redis-op-count win
    described in the helper docstring."""
    metrics = _make_metrics()
    responses = [
        {"agent_id": f"a{i}", "status": "rejected", "reason": "policy_closed"}
        for i in range(5)
    ]
    responses.append({"agent_id": "open", "status": "success"})

    await _record_broadcast_policy_rejections(metrics, responses, "broadcast_target")

    # Exactly one inc — proves the batching, not just the total.
    assert metrics.inc_counter.await_count == 1
    summary = _summarize_inc_counter_calls(metrics)
    assert summary == {("broadcast_target", "policy_closed"): 5}


@pytest.mark.asyncio
async def test_splits_by_distinct_reasons():
    """Mixed reasons get separate inc_counter calls — one per
    bucket — preserving the per-reason resolution operators need."""
    metrics = _make_metrics()
    responses = [
        {"agent_id": "a1", "status": "rejected", "reason": "policy_closed"},
        {"agent_id": "a2", "status": "rejected", "reason": "policy_unknown_mode"},
        {"agent_id": "a3", "status": "rejected", "reason": "policy_closed"},
    ]

    await _record_broadcast_policy_rejections(metrics, responses, "broadcast_target")

    summary = _summarize_inc_counter_calls(metrics)
    assert summary == {
        ("broadcast_target", "policy_closed"): 2,
        ("broadcast_target", "policy_unknown_mode"): 1,
    }


@pytest.mark.asyncio
async def test_missing_reason_falls_back_to_unknown_mode():
    """Defensive: if a downstream layer ever produces a
    ``status: "rejected"`` entry without a reason field, the
    helper must NOT pass ``None`` (or an empty string) into the
    metric labels — that would either break the schema or
    silently degrade the dashboard. Falling back to
    ``policy_unknown_mode`` keeps the label set bounded."""
    metrics = _make_metrics()
    responses = [
        {"agent_id": "a1", "status": "rejected"},
        {"agent_id": "a2", "status": "rejected", "reason": None},
        {"agent_id": "a3", "status": "rejected", "reason": ""},
    ]

    await _record_broadcast_policy_rejections(metrics, responses, "broadcast_target")

    summary = _summarize_inc_counter_calls(metrics)
    assert summary == {("broadcast_target", "policy_unknown_mode"): 3}


@pytest.mark.asyncio
async def test_path_label_is_caller_supplied():
    """The same helper is reused by ``/broadcast`` and
    ``/broadcast-by-tag``; pinning that ``path`` is taken
    verbatim from the caller (not hardcoded) so future fan-out
    routes can attach a distinct label without forking the
    helper."""
    metrics = _make_metrics()
    responses = [{"agent_id": "a1", "status": "rejected", "reason": "policy_closed"}]

    await _record_broadcast_policy_rejections(metrics, responses, "tag_broadcast")

    summary = _summarize_inc_counter_calls(metrics)
    assert summary == {("tag_broadcast", "policy_closed"): 1}


@pytest.mark.asyncio
async def test_ignores_non_rejected_statuses():
    """``failed`` (existing best_effort failure shape) and
    ``success`` must NEVER touch the rejection metric — the new
    metric is reserved exclusively for policy denials, not for
    delivery errors. Mixing them would corrupt the operator's
    mental model and break alerts."""
    metrics = _make_metrics()
    responses = [
        {"agent_id": "a1", "status": "success", "message_id": "m1"},
        {"agent_id": "a2", "status": "failed", "error": "upstream gone"},
        {"agent_id": "a3", "status": "rejected", "reason": "policy_closed"},
    ]

    await _record_broadcast_policy_rejections(metrics, responses, "broadcast_target")

    summary = _summarize_inc_counter_calls(metrics)
    assert summary == {("broadcast_target", "policy_closed"): 1}


@pytest.mark.asyncio
async def test_realistic_fanout_summary():
    """Sanity-check shape: large fan-out with mixed outcomes
    aggregates correctly and stays bounded. Loosely models a
    `/broadcast` to a large subnet."""
    metrics = _make_metrics()
    responses: list[dict] = []
    for i in range(80):
        responses.append({"agent_id": f"open-{i}", "status": "success"})
    for i in range(15):
        responses.append(
            {"agent_id": f"closed-{i}", "status": "rejected", "reason": "policy_closed"}
        )
    for i in range(3):
        responses.append(
            {"agent_id": f"odd-{i}", "status": "rejected", "reason": "policy_unknown_mode"}
        )
    for i in range(2):
        responses.append({"agent_id": f"flake-{i}", "status": "failed", "error": "x"})

    await _record_broadcast_policy_rejections(metrics, responses, "broadcast_target")

    summary = _summarize_inc_counter_calls(metrics)
    expected = Counter(
        {
            ("broadcast_target", "policy_closed"): 15,
            ("broadcast_target", "policy_unknown_mode"): 3,
        }
    )
    assert Counter(summary) == expected
    # Bucket count = unique reasons, not unique targets.
    assert metrics.inc_counter.await_count == 2
