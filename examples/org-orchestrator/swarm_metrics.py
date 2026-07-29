#!/usr/bin/env python3
"""Org swarm quality metrics (M0) — observe waves; no Kernel / no auto fan-out.

Reads a JSON fixture or stdin of wave snapshots, computes R/P/C/K and
anti-pattern alerts (SERIAL_COLLAPSE / FAKE_PARALLEL).

Fixture shape:
{
  "waves": [
    {
      "wave_id": "wv_1",
      "root_work_id": "work_root",
      "root_status": "done",
      "children": [
        {"work_id": "w1", "status": "done",
         "started_at": "…ISO", "ended_at": "…ISO"},
        ...
      ]
    }
  ]
}

Env:
  SWARM_FAKE_PARALLEL_MIN_CHILDREN (default 3)
  SWARM_FAKE_PARALLEL_MAX_COMPLETION (default 0.3)
  SWARM_SERIAL_MIN_CHILDREN (default 2)

Exit 0 always when input parses (metrics are observational).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any


def _parse_ts(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw))
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _duration_sec(child: dict[str, Any]) -> float | None:
    if "duration_sec" in child and child["duration_sec"] is not None:
        try:
            return float(child["duration_sec"])
        except (TypeError, ValueError):
            return None
    start = _parse_ts(child.get("started_at"))
    end = _parse_ts(child.get("ended_at"))
    if start and end:
        return max(0.0, (end - start).total_seconds())
    return None


def _peak_parallelism(children: list[dict[str, Any]]) -> int:
    """Peak concurrent in_progress count using interval overlap (or status-only)."""
    intervals: list[tuple[float, float]] = []
    for c in children:
        start = _parse_ts(c.get("started_at"))
        end = _parse_ts(c.get("ended_at"))
        if start and end:
            intervals.append((start.timestamp(), end.timestamp()))
    if intervals:
        events: list[tuple[float, int]] = []
        for a, b in intervals:
            events.append((a, 1))
            events.append((b, -1))
        # Same timestamp: process ends (-1) before starts (+1) so back-to-back
        # serial intervals do not count as overlap.
        events.sort(key=lambda x: (x[0], x[1]))
        cur = peak = 0
        for _, d in events:
            cur += d
            peak = max(peak, cur)
        return peak
    # Fallback: count children that were ever in_progress / done as parallel=1 each if no times
    n_active = sum(
        1
        for c in children
        if str(c.get("status") or "") in ("in_progress", "done", "todo")
    )
    return 1 if n_active else 0


def _critical_path_sec(children: list[dict[str, Any]]) -> float | None:
    """M0: single phase → max(child durations). Multi-phase later."""
    durs = [d for d in (_duration_sec(c) for c in children) if d is not None]
    if not durs:
        return None
    return max(durs)


def _wall_clock_sec(children: list[dict[str, Any]]) -> float | None:
    starts: list[datetime] = []
    ends: list[datetime] = []
    for c in children:
        s = _parse_ts(c.get("started_at"))
        e = _parse_ts(c.get("ended_at"))
        if s:
            starts.append(s)
        if e:
            ends.append(e)
    if starts and ends:
        return max(0.0, (max(ends) - min(starts)).total_seconds())
    durs = [d for d in (_duration_sec(c) for c in children) if d is not None]
    if durs:
        return sum(durs)  # no absolute timeline → cannot prove overlap
    return None


def score_wave(wave: dict[str, Any]) -> dict[str, Any]:
    kind = str(wave.get("kind") or "wave").strip().lower()
    children = list(wave.get("children") or [])
    n = len(children)
    done_n = sum(1 for c in children if str(c.get("status") or "") == "done")
    cancelled_n = sum(
        1 for c in children if str(c.get("status") or "") == "cancelled"
    )
    created_n = n  # denominator includes cancelled
    root_status = str(wave.get("root_status") or "")
    r = 1.0 if root_status == "done" else 0.0
    allow_alerts = kind != "window"

    if n == 0:
        p = 1.0 if root_status in ("done", "in_progress") else 0.0
        c = 1.0 if root_status == "done" else 0.0
        return {
            "wave_id": wave.get("wave_id"),
            "root_work_id": wave.get("root_work_id"),
            "kind": kind,
            "R": r,
            "P": p,
            "C": c,
            "K_sec": None,
            "child_count": 0,
            "peak_parallelism": int(p),
            "alerts": [],
            "score": round(0.5 * r + 0.25 * p + 0.25 * c, 4),
        }

    c_rate = (done_n / created_n) if created_n else 0.0
    peak = _peak_parallelism(children)
    p_norm = (peak / n) if n else 0.0
    k = _critical_path_sec(children)
    wall = _wall_clock_sec(children)
    sum_dur = 0.0
    have_all_dur = True
    for ch in children:
        d = _duration_sec(ch)
        if d is None:
            have_all_dur = False
            break
        sum_dur += d

    alerts: list[str] = []
    if allow_alerts:
        min_serial = int(os.environ.get("SWARM_SERIAL_MIN_CHILDREN", "2"))
        serial_ratio = float(os.environ.get("SWARM_SERIAL_WALL_RATIO", "0.85"))
        # Doc §4.2: n≥2, peak P=1, wall ≥ 0.85 × Σ child durations
        if (
            n >= min_serial
            and peak <= 1
            and have_all_dur
            and wall is not None
            and sum_dur > 0
            and wall >= serial_ratio * sum_dur
        ):
            alerts.append("SERIAL_COLLAPSE")

        min_fake = int(os.environ.get("SWARM_FAKE_PARALLEL_MIN_CHILDREN", "3"))
        max_comp = float(os.environ.get("SWARM_FAKE_PARALLEL_MAX_COMPLETION", "0.3"))
        if n >= min_fake and (c_rate < max_comp or (cancelled_n / n) > 0.5):
            alerts.append("FAKE_PARALLEL")

    score = round(0.5 * r + 0.25 * p_norm + 0.25 * c_rate, 4)
    return {
        "wave_id": wave.get("wave_id"),
        "root_work_id": wave.get("root_work_id"),
        "kind": kind,
        "R": r,
        "P": round(p_norm, 4),
        "C": round(c_rate, 4),
        "K_sec": k,
        "wall_sec": wall,
        "child_count": n,
        "done_count": done_n,
        "cancelled_count": cancelled_n,
        "peak_parallelism": peak,
        "alerts": sorted(set(alerts)),
        "score": score,
    }


def evaluate(doc: dict[str, Any]) -> dict[str, Any]:
    waves = doc.get("waves") or []
    results = [score_wave(w) for w in waves if isinstance(w, dict)]
    alert_count = sum(len(r.get("alerts") or []) for r in results)
    return {
        "wave_count": len(results),
        "alert_count": alert_count,
        "waves": results,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Org swarm metrics M0")
    p.add_argument(
        "path",
        nargs="?",
        default="-",
        help="fixture JSON path or - for stdin",
    )
    args = p.parse_args(argv)
    if args.path == "-":
        raw = sys.stdin.read()
    else:
        raw = open(args.path, encoding="utf-8").read()
    doc = json.loads(raw)
    out = evaluate(doc)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
