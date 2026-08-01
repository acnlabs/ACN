"""Unit tests for examples/org-orchestrator/work_observe.py (§3.3)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "org-orchestrator"
sys.path.insert(0, str(EXAMPLES))

from work_observe import (  # noqa: E402
    ObservationStore,
    build_true_waves_from_graph,
    report,
    timeline_from_events,
    window_proxies,
)


def test_observe_diffs_only(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "events.jsonl")
    snap1 = [
        {"work_id": "w1", "status": "todo", "assignee_agent_id": "agt_a"},
        {"work_id": "w2", "status": "todo", "assignee_agent_id": "agt_b"},
    ]
    w1 = store.observe(snap1, observed_at="2026-08-01T10:00:00+00:00")
    assert len(w1) == 2

    # unchanged → no write
    w2 = store.observe(snap1, observed_at="2026-08-01T10:01:00+00:00")
    assert w2 == []

    snap2 = [
        {"work_id": "w1", "status": "in_progress", "assignee_agent_id": "agt_a"},
        {"work_id": "w2", "status": "todo", "assignee_agent_id": "agt_b"},
    ]
    w3 = store.observe(snap2, observed_at="2026-08-01T10:02:00+00:00")
    assert len(w3) == 1
    assert w3[0]["work_id"] == "w1"
    assert w3[0]["status"] == "in_progress"

    events = store.read_events()
    assert len(events) == 3
    assert all("observed_at" in e and "ts" in e for e in events)


def test_timeline_and_window_no_alerts(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "events.jsonl")
    polls = [
        (
            "2026-08-01T10:00:00+00:00",
            [
                {"work_id": "a", "status": "todo", "assignee_agent_id": "x"},
                {"work_id": "b", "status": "todo", "assignee_agent_id": "y"},
            ],
        ),
        (
            "2026-08-01T10:05:00+00:00",
            [
                {"work_id": "a", "status": "in_progress", "assignee_agent_id": "x"},
                {"work_id": "b", "status": "in_progress", "assignee_agent_id": "y"},
            ],
        ),
        (
            "2026-08-01T10:15:00+00:00",
            [
                {"work_id": "a", "status": "done", "assignee_agent_id": "x"},
                {"work_id": "b", "status": "done", "assignee_agent_id": "y"},
            ],
        ),
    ]
    items = polls[-1][1]
    for ts, snap in polls:
        store.observe(snap, observed_at=ts)

    tl = timeline_from_events(store.read_events())
    assert tl["a"]["started_at"] == "2026-08-01T10:05:00+00:00"
    assert tl["a"]["ended_at"] == "2026-08-01T10:15:00+00:00"

    out = report(items, store.read_events(), org_id="org_t")
    assert out["window"]["kind"] == "window"
    assert out["window"]["alerts"] == []
    assert out["window"]["P"] == 2
    assert out["window"]["R_window"] == 1.0
    assert out["window"]["P_proxy"] == 0  # no open tickets
    assert out["alert_count"] == 0


def test_true_wave_from_graph_can_alert(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "events.jsonl")
    # serial: a then b
    store.observe(
        [
            {"work_id": "root", "status": "in_progress", "assignee_agent_id": "g"},
            {"work_id": "a", "status": "in_progress", "assignee_agent_id": "x"},
            {"work_id": "b", "status": "todo", "assignee_agent_id": "y"},
        ],
        observed_at="2026-08-01T10:00:00+00:00",
    )
    store.observe(
        [
            {"work_id": "root", "status": "in_progress", "assignee_agent_id": "g"},
            {"work_id": "a", "status": "done", "assignee_agent_id": "x"},
            {"work_id": "b", "status": "in_progress", "assignee_agent_id": "y"},
        ],
        observed_at="2026-08-01T10:10:00+00:00",
    )
    items = [
        {"work_id": "root", "status": "done", "assignee_agent_id": "g"},
        {"work_id": "a", "status": "done", "assignee_agent_id": "x"},
        {"work_id": "b", "status": "done", "assignee_agent_id": "y"},
    ]
    store.observe(items, observed_at="2026-08-01T10:20:00+00:00")

    graph = {
        "waves": [
            {
                "wave_id": "wv_live",
                "root_work_id": "root",
                "child_work_ids": ["a", "b"],
            }
        ]
    }
    waves = build_true_waves_from_graph(graph, items, store.read_events())
    assert waves[0]["root_status"] == "done"
    out = report(items, store.read_events(), org_id="org_t", wave_graph=graph)
    assert out["wave_count"] == 1
    assert "SERIAL_COLLAPSE" in out["waves"][0]["alerts"]


def test_window_proxies_open_assignees() -> None:
    children = [
        {"work_id": "a", "status": "in_progress", "assignee_agent_id": "x"},
        {"work_id": "b", "status": "todo", "assignee_agent_id": "y"},
        {"work_id": "c", "status": "done", "assignee_agent_id": "x"},
    ]
    p = window_proxies(children)
    assert p["P_proxy"] == 2
    assert p["R_window"] == 1.0
    assert p["terminal_count"] == 1


def test_cli_observe_and_report(tmp_path: Path) -> None:
    from work_observe import main

    events = tmp_path / "e.jsonl"
    snap = tmp_path / "snap.json"
    snap.write_text(
        json.dumps(
            {
                "work": [
                    {
                        "work_id": "w1",
                        "status": "todo",
                        "assignee_agent_id": "a",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert main(["observe", "--events", str(events), "--snapshot", str(snap)]) == 0
    assert events.is_file()
    assert main(
        [
            "report",
            "--events",
            str(events),
            "--snapshot",
            str(snap),
            "--org-id",
            "org_x",
        ]
    ) == 0
