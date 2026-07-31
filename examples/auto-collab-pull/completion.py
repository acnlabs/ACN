"""Aggregate completion / response signals from task history items.

Canonical write path is ACN Kernel ``metadata.performance`` (see
``acn.services.agent_performance``). This module remains for offline
fixtures and optional local ``PERF_CACHE`` overlay in the matcher.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Judged outcomes only — open/in-progress do not move the rate.
_SUCCESS = frozenset({"completed"})
_FAIL = frozenset({"rejected"})
_SETTLED = _SUCCESS | _FAIL

DEFAULT_MIN_SAMPLES = 3


def _parse_ts(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def aggregate_history(
    items: list[dict[str, Any]],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> dict[str, Any]:
    """Return performance fields + debug counts.

    ``completion_rate`` omitted when settled samples < min_samples.
    """
    settled = 0
    success = 0
    latencies_h: list[float] = []

    for row in items:
        if not isinstance(row, dict):
            continue
        st = str(row.get("status") or "").lower()
        if st in _SETTLED:
            settled += 1
            if st in _SUCCESS:
                success += 1
        joined = _parse_ts(row.get("joined_at") or row.get("assigned_at"))
        submitted = _parse_ts(row.get("submitted_at"))
        if joined and submitted and submitted >= joined:
            latencies_h.append(
                (submitted - joined).total_seconds() / 3600.0
            )

    out: dict[str, Any] = {
        "settled": settled,
        "success": success,
        "fail": settled - success,
        "samples": settled,
        "min_samples": min_samples,
    }
    if settled >= min_samples:
        out["completion_rate"] = round(success / settled, 4)

    if latencies_h:
        # median hours → score: ≤1h ≈ 1.0, 24h ≈ 0.5, 72h+ → ~0.2
        latencies_h.sort()
        med = latencies_h[len(latencies_h) // 2]
        out["median_response_hours"] = round(med, 3)
        out["response_score"] = round(
            max(0.0, min(1.0, 1.0 / (1.0 + med / 24.0))), 4
        )

    return out


def performance_patch_from_aggregate(agg: dict[str, Any]) -> dict[str, float]:
    """Subset suitable for metadata.performance / cache merge."""
    patch: dict[str, float] = {}
    if "completion_rate" in agg:
        patch["completion_rate"] = float(agg["completion_rate"])
    if "response_score" in agg:
        patch["response_score"] = float(agg["response_score"])
    return patch


class PerfCache:
    """File-backed agent_id → performance dict (+ updated_at)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = {"agents": {}}

    def load(self) -> None:
        if not self.path.is_file():
            self._data = {"agents": {}}
            return
        try:
            raw = json.loads(self.path.read_text())
            if isinstance(raw, dict) and isinstance(raw.get("agents"), dict):
                self._data = raw
            else:
                self._data = {"agents": {}}
        except (OSError, json.JSONDecodeError):
            self._data = {"agents": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.path)

    def upsert(self, agent_id: str, performance: dict[str, float], *, meta: dict | None = None) -> None:
        self.load()
        entry: dict[str, Any] = {
            "performance": dict(performance),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if meta:
            entry["meta"] = meta
        self._data.setdefault("agents", {})[agent_id] = entry
        self.save()

    def get(self, agent_id: str) -> dict[str, float] | None:
        self.load()
        row = (self._data.get("agents") or {}).get(agent_id)
        if not isinstance(row, dict):
            return None
        perf = row.get("performance")
        return dict(perf) if isinstance(perf, dict) else None

    def merge_into_agents(self, agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return shallow-copied agents with cache performance merged."""
        self.load()
        out: list[dict[str, Any]] = []
        for row in agents:
            if not isinstance(row, dict):
                continue
            aid = str(row.get("agent_id") or "").strip()
            cached = self.get(aid) if aid else None
            if not cached:
                out.append(row)
                continue
            copy = dict(row)
            md = dict(copy.get("metadata") or {}) if isinstance(copy.get("metadata"), dict) else {}
            prev = md.get("performance") if isinstance(md.get("performance"), dict) else {}
            md["performance"] = {**prev, **cached}
            copy["metadata"] = md
            out.append(copy)
        return out


def _self_test() -> None:
    items = [
        {"status": "completed", "joined_at": "2026-07-01T00:00:00Z", "submitted_at": "2026-07-01T01:00:00Z"},
        {"status": "completed", "joined_at": "2026-07-02T00:00:00Z", "submitted_at": "2026-07-02T02:00:00Z"},
        {"status": "rejected", "joined_at": "2026-07-03T00:00:00Z", "submitted_at": "2026-07-04T00:00:00Z"},
        {"status": "open"},
    ]
    agg = aggregate_history(items, min_samples=3)
    assert agg["completion_rate"] == round(2 / 3, 4), agg
    assert "response_score" in agg
    patch = performance_patch_from_aggregate(agg)
    assert 0 < patch["completion_rate"] < 1

    cold = aggregate_history([{"status": "completed"}], min_samples=3)
    assert "completion_rate" not in cold

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        cache = PerfCache(Path(td) / "c.json")
        cache.upsert("agt_x", patch, meta={"settled": 3})
        agents = [{"agent_id": "agt_x", "metadata": {}}, {"agent_id": "agt_y"}]
        merged = cache.merge_into_agents(agents)
        assert merged[0]["metadata"]["performance"]["completion_rate"] == patch["completion_rate"]
        assert "performance" not in (merged[1].get("metadata") or {})

    print("completion self-test OK")


if __name__ == "__main__":
    _self_test()
