#!/usr/bin/env python3
"""MVP-1 auto-collab-pull: known invite list → timely wake (summary-only).

External sidecar — does NOT live in ACN Kernel.
See docs/auto-collab-pull-mvp-v0.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ORCH = _HERE.parent / "org-orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from acn_client import (  # noqa: E402
    agents_me,
    get_task,
    list_participations,
    normalize_base,
    send_message,
)
from effective_cap import (  # noqa: E402
    active_agent_ids,
    candidates_to_wake,
    effective_cap_from_task,
    seats_taken,
)
from idempotency import IdempotencyStore  # noqa: E402
from summary import task_summary  # noqa: E402

PULL_TYPE = "acn.task.collab_pull"
PULL_GENERATION = 1
_TERMINAL_STATUS = frozenset(
    {"completed", "cancelled", "rejected", "closed", "expired"}
)


def pull_key(task_id: str, invitee: str) -> str:
    return f"{task_id}:collab_pull:{PULL_GENERATION}:{invitee}"


def notified_invitees(store: IdempotencyStore, task_id: str) -> set[str]:
    """Invitees already claimed/sent for this task (skip when filling next seats)."""
    prefix = f"{task_id}:collab_pull:{PULL_GENERATION}:"
    out: set[str] = set()
    for key in store.list_sent():
        if key.startswith(prefix):
            out.add(key[len(prefix) :])
    return out


def build_envelope(task: dict, invitee: str) -> dict:
    tid = str(task.get("task_id") or task.get("id") or "")
    return {
        "type": PULL_TYPE,
        "schema_version": 1,
        "idempotency_key": pull_key(tid, invitee),
        "task_id": tid,
        "invitee": invitee,
        "summary": task_summary(task),
        "hint": (
            "Fetch task via ACN GET /tasks/{id}; accept to take a seat. "
            "Full materials only after Active."
        ),
    }


def invited_ids(task: dict) -> list[str]:
    raw = task.get("invited_agent_ids") or []
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def process_task(
    *,
    base: str,
    api_key: str,
    from_agent: str,
    task_id: str,
    store: IdempotencyStore,
    dry_run: bool,
) -> dict:
    task = get_task(base, task_id, api_key)
    status = str(task.get("status") or "").lower()
    if status in _TERMINAL_STATUS:
        print(f"[auto-collab-pull] skip {task_id}: terminal status={status}")
        return {"woke": 0, "skipped": "terminal"}

    invited = invited_ids(task)
    if not invited:
        print(f"[auto-collab-pull] skip {task_id}: empty invited_agent_ids")
        return {"woke": 0, "skipped": "no_invites"}

    try:
        parts = list_participations(base, task_id, api_key)
    except urllib.error.HTTPError as e:
        print(
            f"[auto-collab-pull] participations HTTP {e.code}: {e.reason} "
            "(continuing with empty)",
            file=sys.stderr,
        )
        parts = []

    cap = effective_cap_from_task(task)
    used = seats_taken(task, parts)
    already = active_agent_ids(parts)
    aid = str(
        task.get("assignee_id")
        or task.get("assigned_agent_id")
        or task.get("assignee_agent_id")
        or ""
    ).strip()
    if aid:
        already.add(aid)

    notified = notified_invitees(store, task_id)
    targets = candidates_to_wake(
        invited=invited,
        already_active=already,
        cap=cap,
        seats_used=used,
        already_notified=notified,
    )
    print(
        f"[auto-collab-pull] task={task_id} cap={cap} seats_used={used} "
        f"invited={len(invited)} notified={len(notified)} to_wake={len(targets)}",
        flush=True,
    )
    if not targets:
        print("  nothing to wake (full, all active, or all notified)", flush=True)
        return {
            "woke": 0,
            "cap": cap,
            "seats_used": used,
            "notified": len(notified),
        }

    woke = 0
    for invitee in targets:
        key = pull_key(task_id, invitee)
        envelope = build_envelope(task, invitee)
        text = json.dumps(envelope, ensure_ascii=False)

        if dry_run:
            if store.has(key):
                print(f"  skip {invitee}: already sent", flush=True)
                continue
            print(f"  dry-run wake → {invitee}", flush=True)
            print(f"    {text}", flush=True)
            woke += 1
            continue

        try:
            claimed = store.try_claim(key, work_id=task_id, assignee=invitee)
        except OSError as e:
            print(f"  idempotency claim failed: {e}", file=sys.stderr)
            continue
        if not claimed:
            print(f"  skip {invitee}: already sent", flush=True)
            continue

        try:
            send_message(
                base,
                api_key,
                from_agent=from_agent,
                target_agent=invitee,
                text=text,
            )
            print(f"  send → {invitee} ok", flush=True)
            woke += 1
        except urllib.error.HTTPError as e:
            print(
                f"  send → {invitee} failed HTTP {e.code}: {e.reason}",
                file=sys.stderr,
            )
            try:
                store.release(key)
            except OSError as re:
                print(f"  release failed: {re}", file=sys.stderr)
            continue

        try:
            store.confirm(key)
        except OSError as e:
            print(f"  confirm failed: {e} (already sent)", file=sys.stderr)

    return {"woke": woke, "cap": cap, "seats_used": used, "notified": len(notified)}


def main() -> int:
    p = argparse.ArgumentParser(description="ACN auto-collab-pull MVP-1 sidecar")
    p.add_argument("--task-id", action="append", dest="task_ids", help="Task to pull for")
    p.add_argument("--once", action="store_true", help="Single pass then exit")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--interval", type=float, default=None)
    args = p.parse_args()

    base = normalize_base(os.environ.get("ACN_BASE_URL") or "http://127.0.0.1:8000")
    api_key = (os.environ.get("ACN_API_KEY") or "").strip()
    if not api_key:
        print(
            "ACN_API_KEY required (even for --dry-run; need to fetch task)",
            file=sys.stderr,
        )
        return 2

    task_ids = list(args.task_ids or [])
    env_tid = (os.environ.get("ACN_TASK_ID") or "").strip()
    if env_tid:
        task_ids.append(env_tid)
    deduped: list[str] = []
    seen: set[str] = set()
    for t in task_ids:
        if t and t not in seen:
            seen.add(t)
            deduped.append(t)
    task_ids = deduped
    if not task_ids:
        print("Provide --task-id or ACN_TASK_ID", file=sys.stderr)
        return 2

    idem_path = os.environ.get("PULLER_IDEM_PATH") or str(
        _HERE / ".auto-collab-pull-idem.json"
    )
    store = IdempotencyStore(idem_path)
    interval = args.interval
    if interval is None:
        interval = float(os.environ.get("POLL_INTERVAL_SEC") or "30")

    me = agents_me(base, api_key)
    from_agent = str(me.get("agent_id") or "")
    if not from_agent:
        print("agents/me missing agent_id", file=sys.stderr)
        return 2

    def tick() -> None:
        for tid in task_ids:
            process_task(
                base=base,
                api_key=api_key,
                from_agent=from_agent,
                task_id=tid,
                store=store,
                dry_run=args.dry_run,
            )

    if args.once or args.dry_run:
        tick()
        return 0

    print(f"[auto-collab-pull] polling every {interval}s tasks={task_ids}", flush=True)
    while True:
        tick()
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
