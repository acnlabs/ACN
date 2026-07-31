"""Performance / freshness hooks for shortlist ranking (MVP post-2b).

Signals come from the agent list row first (no N+1). Missing data →
``None`` so hybrid ranking omits this term (weight≈0 when cold).

Optional metadata hooks (product/BFF may fill later):
  metadata.performance.completion_rate  # 0..1
  metadata.performance.load             # 0 idle .. 1 saturated
  metadata.performance.response_score   # 0..1
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _parse_ts(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _freshness(ts: datetime | None, *, half_life_hours: float = 24.0) -> float | None:
    """1.0 = just now, decays toward 0; None if unknown."""
    if ts is None:
        return None
    now = datetime.now(timezone.utc)
    age_h = max(0.0, (now - ts.astimezone(timezone.utc)).total_seconds() / 3600.0)
    # exponential-ish: half_life → 0.5
    score = 0.5 ** (age_h / max(half_life_hours, 1e-6))
    return float(max(0.0, min(1.0, score)))


def _clamp01(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return float(max(0.0, min(1.0, v)))


def _perf_meta(agent: dict[str, Any]) -> dict[str, Any]:
    md = agent.get("metadata") if isinstance(agent.get("metadata"), dict) else {}
    perf = md.get("performance") if isinstance(md, dict) else None
    return perf if isinstance(perf, dict) else {}


def extract_signals(agent: dict[str, Any]) -> dict[str, float | None]:
    """Named components in 0..1 or None."""
    meta = _perf_meta(agent)
    reach: float | None
    ir = agent.get("inbound_reachable")
    if ir is True:
        reach = 1.0
    elif ir is False:
        reach = 0.0
    else:
        reach = None

    fail_raw = agent.get("consec_push_failures")
    fail_pen: float | None = None
    if fail_raw is not None and fail_raw != "":
        try:
            n = max(0, int(fail_raw))
            # 0 failures → 1.0; 5+ → ~0
            fail_pen = max(0.0, 1.0 - n / 5.0)
        except (TypeError, ValueError):
            fail_pen = None

    return {
        "heartbeat_fresh": _freshness(_parse_ts(agent.get("last_heartbeat"))),
        "inbound_fresh": _freshness(_parse_ts(agent.get("last_inbound_ok_at"))),
        "reachable": reach,
        "push_health": fail_pen,
        "completion_rate": _clamp01(meta.get("completion_rate")),
        "response_score": _clamp01(meta.get("response_score")),
        "load_idle": (
            None
            if _clamp01(meta.get("load")) is None
            else 1.0 - float(_clamp01(meta.get("load")))
        ),
    }


def performance_score(agent: dict[str, Any]) -> tuple[float | None, dict[str, float]]:
    """Weighted mean of available signals. None if nothing usable."""
    signals = extract_signals(agent)
    # Weights: prefer completion when present; freshness/reach always useful.
    weights = {
        "completion_rate": 0.35,
        "response_score": 0.15,
        "load_idle": 0.15,
        "reachable": 0.15,
        "heartbeat_fresh": 0.10,
        "inbound_fresh": 0.05,
        "push_health": 0.05,
    }
    num = 0.0
    den = 0.0
    used: dict[str, float] = {}
    for key, w in weights.items():
        v = signals.get(key)
        if v is None:
            continue
        num += w * float(v)
        den += w
        used[key] = float(v)
    if den <= 0:
        return None, {}
    return float(num / den), used


def performance_scores(agents: list[dict[str, Any]]) -> list[float | None]:
    return [performance_score(a)[0] for a in agents]


def _self_test() -> None:
    cold = {"agent_id": "c", "status": "online", "metadata": {}}
    assert performance_score(cold)[0] is None

    hot = {
        "agent_id": "h",
        "status": "online",
        "inbound_reachable": True,
        "consec_push_failures": 0,
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "performance": {
                "completion_rate": 0.9,
                "load": 0.1,
                "response_score": 0.8,
            }
        },
    }
    s, used = performance_score(hot)
    assert s is not None and s > 0.7, (s, used)
    assert "completion_rate" in used

    busy = {
        **hot,
        "metadata": {"performance": {"completion_rate": 0.9, "load": 0.95}},
    }
    s_busy, _ = performance_score(busy)
    assert s_busy is not None and s_busy < s

    print("performance self-test OK")


if __name__ == "__main__":
    _self_test()
