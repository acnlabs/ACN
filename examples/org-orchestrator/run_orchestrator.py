#!/usr/bin/env python3
"""P2: poll open Org work → wake assignee via communication/send → optional in_progress."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error

from acn_client_min import (
    active_member_ids,
    agents_me,
    assignee_id,
    fetch_members,
    fetch_open_work,
    normalize_base,
    patch_work_status,
    send_message,
    work_id,
)
from idempotency import IdempotencyStore

WAKE_TYPE = "acn.org.work_wake"
WAKE_GENERATION = 1


def wake_key(org_id: str, wid: str, assignee: str) -> str:
    """Include assignee so reassignment wakes the new member."""
    return f"{org_id}:{wid}:wake:{WAKE_GENERATION}:{assignee}"


def build_envelope(org_id: str, item: dict) -> dict:
    wid = work_id(item)
    assignee = assignee_id(item)
    return {
        "type": WAKE_TYPE,
        "schema_version": 1,
        "idempotency_key": wake_key(org_id, wid, assignee),
        "org_id": org_id,
        "work_id": wid,
        "title": str(item.get("title") or ""),
        "status": str(item.get("status") or ""),
        "assignee": assignee,
        "hint": (
            "Fetch work with acn org work show; complete then ask governance to mark done."
        ),
    }


def process_item(
    *,
    base: str,
    org_id: str,
    api_key: str,
    from_agent: str,
    item: dict,
    members: set[str],
    store: IdempotencyStore,
    dry_run: bool,
    mark_in_progress: bool,
) -> None:
    wid = work_id(item)
    if not wid:
        print("[org-orchestrator] skip item without work_id", flush=True)
        return

    assignee = assignee_id(item)
    if not assignee:
        print(f"[org-orchestrator] skip {wid}: no assignee", flush=True)
        return
    if assignee not in members:
        print(
            f"[org-orchestrator] skip {wid}: assignee {assignee} not active member",
            flush=True,
        )
        return

    key = wake_key(org_id, wid, assignee)
    envelope = build_envelope(org_id, item)
    text = json.dumps(envelope, ensure_ascii=False)
    title = item.get("title") or ""

    if dry_run:
        # Dry-run does not claim; still surface skip if already recorded.
        if store.has(key):
            print(f"[org-orchestrator] skip {wid}: already sent {key}", flush=True)
            return
        print(
            f"[org-orchestrator] wake work={wid} assignee={assignee} title={title!r}",
            flush=True,
        )
        print(f"  dry-run payload: {text}", flush=True)
        return

    try:
        claimed = store.try_claim(key, work_id=wid, assignee=assignee)
    except OSError as e:
        print(f"[org-orchestrator] idempotency claim failed: {e}", file=sys.stderr)
        return
    if not claimed:
        print(f"[org-orchestrator] skip {wid}: already sent {key}", flush=True)
        return

    print(
        f"[org-orchestrator] wake work={wid} assignee={assignee} title={title!r}",
        flush=True,
    )

    try:
        send_message(
            base,
            api_key,
            from_agent=from_agent,
            target_agent=assignee,
            text=text,
        )
        print("  send → ok", flush=True)
    except urllib.error.HTTPError as e:
        print(f"  send failed HTTP {e.code}: {e.reason}", file=sys.stderr)
        try:
            store.release(key)
        except OSError as release_err:
            print(
                f"  idempotency release failed: {release_err}",
                file=sys.stderr,
            )
        return

    try:
        store.confirm(key)
    except OSError as e:
        print(
            f"  idempotency confirm failed: {e} (wake already sent)",
            file=sys.stderr,
        )

    if mark_in_progress and item.get("status") == "todo":
        try:
            patch_work_status(base, org_id, wid, api_key, "in_progress")
            print("  status → in_progress", flush=True)
        except urllib.error.HTTPError as e:
            print(
                f"  PATCH in_progress failed HTTP {e.code}: {e.reason} "
                "(wake already sent; need governance key?)",
                file=sys.stderr,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Single poll then exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print wake payloads only; no send / PATCH",
    )
    parser.add_argument(
        "--no-mark-in-progress",
        action="store_true",
        help="Do not PATCH todo → in_progress after successful send",
    )
    args = parser.parse_args()

    base_url = os.environ.get("ACN_BASE_URL", "").strip()
    org_id = os.environ.get("ACN_ORG_ID", "").strip()
    api_key = os.environ.get("ACN_API_KEY", "").strip()
    interval = int(os.environ.get("POLL_INTERVAL_SEC", "30"))
    store_path = os.environ.get(
        "ORCHESTRATOR_IDEM_PATH",
        os.path.join(os.getcwd(), ".org-orchestrator-idem.json"),
    )

    if not base_url or not org_id or not api_key:
        print("Need ACN_BASE_URL, ACN_ORG_ID, ACN_API_KEY", file=sys.stderr)
        return 2

    base = normalize_base(base_url)
    store = IdempotencyStore(store_path)
    mark_in_progress = not args.no_mark_in_progress

    try:
        me = agents_me(base, api_key)
        from_agent = str(me.get("agent_id") or "")
    except Exception as e:
        print(f"agents/me failed: {e}", file=sys.stderr)
        return 2
    if not from_agent:
        print("agents/me missing agent_id", file=sys.stderr)
        return 2

    print(
        f"[org-orchestrator] from_agent={from_agent} org={org_id} idem={store_path}",
        flush=True,
    )

    while True:
        try:
            members = active_member_ids(fetch_members(base, org_id, api_key))
            payload = fetch_open_work(base, org_id, api_key)
            items = payload.get("work") or []
            print(
                f"[org-orchestrator] open_count={payload.get('count', len(items))} "
                f"members={len(members)}",
                flush=True,
            )
            for item in items:
                process_item(
                    base=base,
                    org_id=org_id,
                    api_key=api_key,
                    from_agent=from_agent,
                    item=item,
                    members=members,
                    store=store,
                    dry_run=args.dry_run,
                    mark_in_progress=mark_in_progress,
                )
        except urllib.error.HTTPError as e:
            print(f"[org-orchestrator] HTTP {e.code}: {e.reason}", file=sys.stderr)
            if args.once:
                return 1
        except Exception as e:
            print(f"[org-orchestrator] error: {e}", file=sys.stderr)
            if args.once:
                return 1

        if args.once:
            return 0
        time.sleep(max(1, interval))


if __name__ == "__main__":
    raise SystemExit(main())
