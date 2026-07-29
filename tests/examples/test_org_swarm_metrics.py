"""Unit tests for examples/org-orchestrator/swarm_metrics.py (M0)."""

from __future__ import annotations

import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "org-orchestrator"
sys.path.insert(0, str(EXAMPLES))

from swarm_metrics import evaluate, score_wave  # noqa: E402


def test_serial_collapse_alert() -> None:
    wave = {
        "wave_id": "wv_serial",
        "root_work_id": "work_root",
        "root_status": "done",
        "children": [
            {
                "work_id": "a",
                "status": "done",
                "started_at": "2026-07-29T10:00:00+00:00",
                "ended_at": "2026-07-29T10:10:00+00:00",
            },
            {
                "work_id": "b",
                "status": "done",
                "started_at": "2026-07-29T10:10:00+00:00",
                "ended_at": "2026-07-29T10:20:00+00:00",
            },
        ],
    }
    r = score_wave(wave)
    assert r["P"] == 1
    assert r["P_norm"] == 0.5
    assert "SERIAL_COLLAPSE" in r["alerts"]
    assert r["R"] == 1.0
    assert r["C"] == 1.0


def test_duration_only_no_serial_alert() -> None:
    """duration_sec without absolute timeline must not false-positive SERIAL."""
    wave = {
        "wave_id": "wv_dur",
        "root_status": "done",
        "children": [
            {"work_id": "a", "status": "done", "duration_sec": 10},
            {"work_id": "b", "status": "done", "duration_sec": 10},
        ],
    }
    r = score_wave(wave)
    assert "SERIAL_COLLAPSE" not in r["alerts"]


def test_true_parallel_no_serial_alert() -> None:
    wave = {
        "wave_id": "wv_par",
        "root_status": "done",
        "children": [
            {
                "work_id": "a",
                "status": "done",
                "started_at": "2026-07-29T10:00:00+00:00",
                "ended_at": "2026-07-29T10:10:00+00:00",
            },
            {
                "work_id": "b",
                "status": "done",
                "started_at": "2026-07-29T10:00:00+00:00",
                "ended_at": "2026-07-29T10:12:00+00:00",
            },
        ],
    }
    r = score_wave(wave)
    assert r["P"] == 2
    assert r["P_norm"] == 1.0
    assert "SERIAL_COLLAPSE" not in r["alerts"]
    assert r["K_sec"] == 12 * 60


def test_fake_parallel_alert() -> None:
    wave = {
        "wave_id": "wv_fake",
        "root_status": "todo",
        "children": [
            {"work_id": "a", "status": "cancelled", "duration_sec": 1},
            {"work_id": "b", "status": "cancelled", "duration_sec": 1},
            {"work_id": "c", "status": "cancelled", "duration_sec": 1},
            {"work_id": "d", "status": "done", "duration_sec": 1},
        ],
    }
    r = score_wave(wave)
    assert "FAKE_PARALLEL" in r["alerts"]
    assert r["R"] == 0.0


def test_window_kind_never_alerts() -> None:
    wave = {
        "wave_id": "wv_win",
        "kind": "window",
        "root_status": "done",
        "children": [
            {"work_id": "a", "status": "cancelled", "duration_sec": 1},
            {"work_id": "b", "status": "cancelled", "duration_sec": 1},
            {"work_id": "c", "status": "cancelled", "duration_sec": 1},
        ],
    }
    r = score_wave(wave)
    assert r["kind"] == "window"
    assert r["alerts"] == []


def test_evaluate_batch() -> None:
    doc = {
        "waves": [
            {
                "wave_id": "w1",
                "root_status": "done",
                "children": [
                    {
                        "work_id": "a",
                        "status": "done",
                        "started_at": "2026-07-29T10:00:00+00:00",
                        "ended_at": "2026-07-29T10:10:00+00:00",
                    },
                    {
                        "work_id": "b",
                        "status": "done",
                        "started_at": "2026-07-29T10:10:00+00:00",
                        "ended_at": "2026-07-29T10:20:00+00:00",
                    },
                ],
            }
        ]
    }
    out = evaluate(doc)
    assert out["wave_count"] == 1
    assert out["alert_count"] >= 1
