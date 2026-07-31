"""Server-side agent performance aggregation from task history.

Writes into ``agent.metadata["performance"]`` via AgentService — never
accept client-supplied rates. Rules aligned with
``examples/auto-collab-pull/completion.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# Judged outcomes only — open / in-progress do not move the rate.
_SUCCESS = frozenset({"completed"})
_FAIL = frozenset({"rejected"})
_SETTLED = _SUCCESS | _FAIL

DEFAULT_MIN_SAMPLES = 3
# Settled-task window for refresh / settle hooks — last N history rows,
# not lifetime. Keep TaskService._refresh_agent_performance and
# POST …/performance/refresh in sync (both import this constant).
DEFAULT_HISTORY_LIMIT = 50


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
        dt = dt.replace(tzinfo=UTC)
    return dt


def aggregate_performance_from_history(
    items: list[dict[str, Any]],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate settled task outcomes into a performance dict.

    Always includes ``settled`` / ``success`` / ``updated_at``.
    ``completion_rate`` is omitted when ``settled < min_samples``.
    Optional ``response_score`` from median joined→submitted latency.
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
            latencies_h.append((submitted - joined).total_seconds() / 3600.0)

    ts = now or datetime.now(UTC)
    out: dict[str, Any] = {
        "settled": settled,
        "success": success,
        "updated_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if settled >= min_samples:
        out["completion_rate"] = round(success / settled, 4)

    if latencies_h:
        latencies_h.sort()
        med = latencies_h[len(latencies_h) // 2]
        out["median_response_hours"] = round(med, 3)
        out["response_score"] = round(
            max(0.0, min(1.0, 1.0 / (1.0 + med / 24.0))),
            4,
        )
    return out
