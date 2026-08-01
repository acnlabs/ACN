#!/usr/bin/env python3
"""Org work observation log (§3.3) — append-only diff on each list_work poll.

Does not wake, patch, or fan-out. Enable from run_orchestrator via
ORG_METRICS_OBSERVE_PATH; unset = metrics off (M0-S3).

Event shape (doc):
  { ts, work_id, status, assignee_agent_id, observed_at }

Files:
  <path>            JSONL events (append-only; SoT for last-seen)
  <path>.state.json mirror of last-seen (inspect / cache)
  <path>.lock       flock for multi-process + crash-safe batches
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from swarm_metrics import evaluate, score_wave


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_ts(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
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


def work_id(item: dict[str, Any]) -> str:
    return str(item.get("work_id") or item.get("id") or "").strip()


def assignee_id(item: dict[str, Any]) -> str:
    return str(item.get("assignee_agent_id") or item.get("assignee") or "").strip()


class ObservationStore:
    """Diff poll snapshots → append-only JSONL + last-seen mirror.

    Last-seen is rebuilt from the JSONL under flock so a crash after append
    (before state mirror) does not duplicate the same transition.
    """

    def __init__(self, events_path: str | Path) -> None:
        self.events_path = Path(events_path)
        self.state_path = Path(str(self.events_path) + ".state.json")
        self.lock_path = Path(str(self.events_path) + ".lock")

    def _with_lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        return open(self.lock_path, "a+", encoding="utf-8")

    def _save_state_unlocked(self, state: dict[str, dict[str, str]]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.state_path)

    def _read_events_unlocked(self) -> list[dict[str, Any]]:
        if not self.events_path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("work_id"):
                out.append(row)
        return out

    def _last_seen_from_events_unlocked(self) -> dict[str, dict[str, str]]:
        """JSONL is SoT — survives crash between append and state mirror."""
        last: dict[str, dict[str, str]] = {}
        for ev in self._read_events_unlocked():
            wid = str(ev.get("work_id") or "")
            if not wid:
                continue
            last[wid] = {
                "status": str(ev.get("status") or ""),
                "assignee_agent_id": str(ev.get("assignee_agent_id") or ""),
            }
        return last

    def observe(
        self,
        items: list[dict[str, Any]],
        *,
        observed_at: str | None = None,
    ) -> list[dict[str, Any]]:
        """Write one event per work whose status or assignee changed (incl. first sight)."""
        ts = observed_at or _utc_now_iso()
        with self._with_lock() as lockf:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
            try:
                state = self._last_seen_from_events_unlocked()
                pending: list[dict[str, Any]] = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    wid = work_id(item)
                    if not wid:
                        continue
                    status = str(item.get("status") or "")
                    assignee = assignee_id(item)
                    prev = state.get(wid)
                    if (
                        prev is not None
                        and prev.get("status") == status
                        and prev.get("assignee_agent_id") == assignee
                    ):
                        continue
                    event = {
                        "ts": ts,
                        "work_id": wid,
                        "status": status,
                        "assignee_agent_id": assignee,
                        "observed_at": ts,
                    }
                    pending.append(event)
                    state[wid] = {
                        "status": status,
                        "assignee_agent_id": assignee,
                    }
                if pending:
                    self.events_path.parent.mkdir(parents=True, exist_ok=True)
                    chunk = "".join(
                        json.dumps(ev, ensure_ascii=False) + "\n" for ev in pending
                    )
                    with self.events_path.open("a", encoding="utf-8") as ef:
                        ef.write(chunk)
                        ef.flush()
                        os.fsync(ef.fileno())
                    self._save_state_unlocked(state)
                return pending
            finally:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)

    def read_events(self) -> list[dict[str, Any]]:
        with self._with_lock() as lockf:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_SH)
            try:
                return self._read_events_unlocked()
            finally:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def timeline_from_events(
    events: list[dict[str, Any]],
    work_ids: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Per work_id: started_at (first in_progress), ended_at (first terminal)."""
    by_id: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        wid = str(ev.get("work_id") or "")
        if not wid:
            continue
        if work_ids is not None and wid not in work_ids:
            continue
        by_id.setdefault(wid, []).append(ev)

    out: dict[str, dict[str, Any]] = {}
    for wid, rows in by_id.items():
        def _sort_key(r: dict[str, Any]) -> datetime:
            return _parse_ts(r.get("observed_at") or r.get("ts")) or datetime(
                1970, 1, 1, tzinfo=timezone.utc
            )

        rows.sort(key=_sort_key)
        started: str | None = None
        ended: str | None = None
        last_status = ""
        last_assignee = ""
        for r in rows:
            status = str(r.get("status") or "")
            last_status = status
            last_assignee = str(r.get("assignee_agent_id") or "")
            obs = str(r.get("observed_at") or r.get("ts") or "")
            if started is None and status == "in_progress":
                started = obs
            if ended is None and status in ("done", "cancelled"):
                ended = obs
        out[wid] = {
            "started_at": started,
            "ended_at": ended,
            "status": last_status,
            "assignee_agent_id": last_assignee,
        }
    return out


def window_proxies(
    children: list[dict[str, Any]],
) -> dict[str, Any]:
    """Doc §4.1 coarse proxies (never used for SERIAL_*/FAKE_*)."""
    terminal = [
        c
        for c in children
        if str(c.get("status") or "") in ("done", "cancelled")
    ]
    done_n = sum(1 for c in terminal if str(c.get("status") or "") == "done")
    r_window = (done_n / len(terminal)) if terminal else None

    open_rows = [
        c
        for c in children
        if str(c.get("status") or "") in ("todo", "in_progress")
    ]
    assignees = {
        str(c.get("assignee_agent_id") or "").strip()
        for c in open_rows
        if str(c.get("assignee_agent_id") or "").strip()
    }
    p_proxy = len(assignees)

    # Doc §4.1: K_proxy = max(updated_at - created_at) on terminal tickets.
    # Observe-derived started_at/ended_at feed score_wave K_sec, not K_proxy.
    k_vals: list[float] = []
    for c in terminal:
        start = _parse_ts(c.get("created_at"))
        end = _parse_ts(c.get("updated_at"))
        if start and end:
            k_vals.append(max(0.0, (end - start).total_seconds()))
    k_proxy = max(k_vals) if k_vals else None
    return {
        "R_window": None if r_window is None else round(r_window, 4),
        "P_proxy": p_proxy,
        "K_proxy_sec": k_proxy,
        "terminal_count": len(terminal),
        "open_count": len(open_rows),
    }


def children_from_snapshot(
    items: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    timelines = timeline_from_events(events)
    children: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        wid = work_id(item)
        if not wid:
            continue
        tl = timelines.get(wid) or {}
        children.append(
            {
                "work_id": wid,
                "status": str(item.get("status") or tl.get("status") or ""),
                "assignee_agent_id": assignee_id(item)
                or str(tl.get("assignee_agent_id") or ""),
                "started_at": tl.get("started_at"),
                "ended_at": tl.get("ended_at"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            }
        )
    return children


def build_window_wave(
    items: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    org_id: str = "",
    wave_id: str | None = None,
) -> dict[str, Any]:
    children = children_from_snapshot(items, events)
    return {
        "wave_id": wave_id or f"win_{org_id or 'org'}",
        "kind": "window",
        "root_work_id": None,
        "root_status": "",
        "children": children,
    }


def wave_graph_from_metadata(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Build sidecar wave graph from ``metadata.wave`` on list_work items.

    Expected shape (doc §3.2)::

      {"wave": {"role": "root"|"child", "wave_id": "wv_…",
                "root_work_id": "work_…", "shard_hint": "…"}}

    Kernel does not parse this; orchestration / report does.
    """
    waves: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata")
        if not isinstance(meta, dict):
            continue
        wave = meta.get("wave")
        if not isinstance(wave, dict):
            continue
        wv = str(wave.get("wave_id") or "").strip()
        if not wv:
            continue
        role = str(wave.get("role") or "").strip().lower()
        root_hint = str(wave.get("root_work_id") or "").strip()
        entry = waves.setdefault(
            wv,
            {"wave_id": wv, "root_work_id": "", "child_work_ids": []},
        )
        wid = work_id(item)
        if role == "root":
            entry["root_work_id"] = wid or root_hint
        elif role == "child":
            if wid and wid not in entry["child_work_ids"]:
                entry["child_work_ids"].append(wid)
            if root_hint and not entry["root_work_id"]:
                entry["root_work_id"] = root_hint
        else:
            # Unknown role: treat as child if not the declared root.
            if root_hint and wid == root_hint:
                entry["root_work_id"] = wid
            elif wid and wid not in entry["child_work_ids"]:
                entry["child_work_ids"].append(wid)
            if root_hint and not entry["root_work_id"]:
                entry["root_work_id"] = root_hint
    return {
        "waves": [
            w
            for w in waves.values()
            if w.get("root_work_id") or w.get("child_work_ids")
        ]
    }


def build_true_waves_from_graph(
    graph: dict[str, Any],
    items: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Optional sidecar wave graph → true waves scored with §4.2 alerts.

    graph shape:
      {"waves":[{"wave_id","root_work_id","child_work_ids":[…]}]}
    """
    by_id = {work_id(i): i for i in items if isinstance(i, dict) and work_id(i)}
    timelines = timeline_from_events(events)
    waves: list[dict[str, Any]] = []
    for w in graph.get("waves") or []:
        if not isinstance(w, dict):
            continue
        root_id = str(w.get("root_work_id") or "")
        child_ids = [str(x) for x in (w.get("child_work_ids") or []) if x]
        children: list[dict[str, Any]] = []
        for cid in child_ids:
            item = by_id.get(cid) or {}
            tl = timelines.get(cid) or {}
            children.append(
                {
                    "work_id": cid,
                    "status": str(item.get("status") or tl.get("status") or ""),
                    "assignee_agent_id": assignee_id(item)
                    if item
                    else str(tl.get("assignee_agent_id") or ""),
                    "started_at": tl.get("started_at"),
                    "ended_at": tl.get("ended_at"),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                }
            )
        root_item = by_id.get(root_id) or {}
        root_tl = timelines.get(root_id) or {}
        waves.append(
            {
                "wave_id": w.get("wave_id"),
                "kind": "wave",
                "root_work_id": root_id or None,
                "root_status": str(
                    root_item.get("status") or root_tl.get("status") or ""
                ),
                "children": children,
            }
        )
    return waves


def report(
    items: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    org_id: str = "",
    wave_graph: dict[str, Any] | None = None,
    from_metadata: bool = True,
) -> dict[str, Any]:
    """Window bundle always; true waves from graph file and/or metadata.wave."""
    window = build_window_wave(items, events, org_id=org_id)
    window_score = score_wave(window)
    proxies = window_proxies(window["children"])
    graph = wave_graph
    graph_source = "explicit" if graph else None
    if graph is None and from_metadata:
        derived = wave_graph_from_metadata(items)
        if derived.get("waves"):
            graph = derived
            graph_source = "metadata.wave"
    out: dict[str, Any] = {
        "kind": "observe_report",
        "org_id": org_id or None,
        "event_count": len(events),
        "window": {**window_score, **proxies},
        "waves": [],
        "wave_graph_source": graph_source,
    }
    if graph:
        true_waves = build_true_waves_from_graph(graph, items, events)
        scored = evaluate({"waves": true_waves})
        out["waves"] = scored["waves"]
        out["wave_count"] = scored["wave_count"]
        out["alert_count"] = scored["alert_count"]
    else:
        out["wave_count"] = 0
        out["alert_count"] = 0
    return out


def _load_json_path(path: str) -> Any:
    if path == "-":
        return json.loads(sys.stdin.read())
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Org work observation log (§3.3)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_obs = sub.add_parser("observe", help="Diff snapshot → append events")
    p_obs.add_argument(
        "--events",
        required=True,
        help="JSONL events path (state at <path>.state.json)",
    )
    p_obs.add_argument(
        "--snapshot",
        default="-",
        help="list_work JSON ({work:[…]}) or raw list; - = stdin",
    )
    p_obs.add_argument("--observed-at", default=None, help="ISO timestamp override")

    p_rep = sub.add_parser("report", help="Score window (+ optional true waves)")
    p_rep.add_argument("--events", required=True)
    p_rep.add_argument(
        "--snapshot",
        default="-",
        help="current list_work JSON for latest status",
    )
    p_rep.add_argument("--org-id", default=os.environ.get("ACN_ORG_ID", ""))
    p_rep.add_argument(
        "--wave-graph",
        default="",
        help="optional sidecar wave graph JSON (wins over metadata.wave)",
    )
    p_rep.add_argument(
        "--from-metadata",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="derive true waves from item metadata.wave (default: on)",
    )

    args = p.parse_args(argv)
    store = ObservationStore(args.events)

    if args.cmd == "observe":
        snap = _load_json_path(args.snapshot)
        if isinstance(snap, dict):
            items = list(snap.get("work") or snap.get("items") or [])
        elif isinstance(snap, list):
            items = snap
        else:
            print("snapshot must be object or list", file=sys.stderr)
            return 2
        written = store.observe(items, observed_at=args.observed_at)
        print(
            json.dumps(
                {"wrote": len(written), "events": written},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.cmd == "report":
        snap = _load_json_path(args.snapshot)
        if isinstance(snap, dict):
            items = list(snap.get("work") or snap.get("items") or [])
        elif isinstance(snap, list):
            items = snap
        else:
            print("snapshot must be object or list", file=sys.stderr)
            return 2
        graph = None
        if args.wave_graph:
            graph = _load_json_path(args.wave_graph)
            if not isinstance(graph, dict):
                print("wave-graph must be object", file=sys.stderr)
                return 2
        out = report(
            items,
            store.read_events(),
            org_id=args.org_id,
            wave_graph=graph,
            # Explicit file wins; otherwise derive from metadata.wave when enabled.
            from_metadata=bool(args.from_metadata) and not bool(args.wave_graph),
        )
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
