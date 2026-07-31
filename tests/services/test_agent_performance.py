"""Unit tests for server-side agent performance aggregation."""

from __future__ import annotations

from acn.services.agent_performance import aggregate_performance_from_history


def test_aggregate_omits_rate_below_min_samples() -> None:
    items = [
        {"status": "completed"},
        {"status": "rejected"},
    ]
    out = aggregate_performance_from_history(items, min_samples=3)
    assert out["settled"] == 2
    assert out["success"] == 1
    assert "completion_rate" not in out


def test_aggregate_completion_rate_two_of_three() -> None:
    items = [
        {"status": "completed"},
        {"status": "completed"},
        {"status": "rejected"},
        {"status": "open"},
    ]
    out = aggregate_performance_from_history(items, min_samples=3)
    assert out["settled"] == 3
    assert out["success"] == 2
    assert out["completion_rate"] == round(2 / 3, 4)


def test_aggregate_response_score_from_latency() -> None:
    items = [
        {
            "status": "completed",
            "joined_at": "2026-07-01T00:00:00Z",
            "submitted_at": "2026-07-01T01:00:00Z",
        },
        {
            "status": "completed",
            "joined_at": "2026-07-02T00:00:00Z",
            "submitted_at": "2026-07-02T01:00:00Z",
        },
        {
            "status": "completed",
            "joined_at": "2026-07-03T00:00:00Z",
            "submitted_at": "2026-07-03T01:00:00Z",
        },
    ]
    out = aggregate_performance_from_history(items, min_samples=3)
    assert out["completion_rate"] == 1.0
    assert "response_score" in out
    assert out["response_score"] > 0.9
    assert "updated_at" in out
