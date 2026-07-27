#!/usr/bin/env python3
"""C2: poll open Org work, run spawnCommand, governance PATCH → done on success."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error

from acn_org import fetch_open_work, normalize_base, patch_work_status, work_id


def expand_command(template: str, org_id: str, item: dict) -> str:
    wid = work_id(item)
    replacements = {
        "{{work_id}}": wid,
        "{{title}}": str(item.get("title") or ""),
        "{{org_id}}": org_id,
        "{{status}}": str(item.get("status") or ""),
    }
    cmd = template
    for needle, value in replacements.items():
        cmd = cmd.replace(needle, value)
    return cmd


def process_item(
    *,
    base: str,
    org_id: str,
    api_key: str,
    item: dict,
    spawn_command: str,
    dry_run: bool,
    mark_in_progress: bool,
) -> bool:
    wid = work_id(item)
    if not wid:
        print("[org-loop-spawn] skip item without work_id", file=sys.stderr)
        return False

    cmd = expand_command(spawn_command, org_id, item)
    title = item.get("title") or ""
    print(f"[org-loop-spawn] work={wid} title={title!r}", flush=True)

    if dry_run:
        print(f"  dry-run command: {cmd}", flush=True)
        return True

    if mark_in_progress and item.get("status") == "todo":
        try:
            patch_work_status(base, org_id, wid, api_key, "in_progress")
            print("  status → in_progress", flush=True)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            print(f"  PATCH in_progress failed HTTP {e.code}: {body}", file=sys.stderr)
            return False

    print(f"  spawn: {cmd}", flush=True)
    proc = subprocess.run(cmd, shell=True)
    if proc.returncode != 0:
        print(f"  spawn exit {proc.returncode}; work left open", file=sys.stderr)
        return False

    try:
        patch_work_status(base, org_id, wid, api_key, "done")
        print("  status → done", flush=True)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"  PATCH done failed HTTP {e.code}: {body}", file=sys.stderr)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Single poll cycle then exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print spawn commands only; no subprocess or PATCH",
    )
    parser.add_argument(
        "--no-mark-in-progress",
        action="store_true",
        help="Skip PATCH in_progress before spawn",
    )
    args = parser.parse_args()

    base_url = os.environ.get("ACN_BASE_URL", "").strip()
    org_id = os.environ.get("ACN_ORG_ID", "").strip()
    api_key = os.environ.get("ACN_API_KEY", "").strip()
    spawn_command = os.environ.get("SPAWN_COMMAND", "").strip()
    interval = int(os.environ.get("POLL_INTERVAL_SEC", "30"))

    if not base_url or not org_id or not api_key:
        print("Need ACN_BASE_URL, ACN_ORG_ID, ACN_API_KEY", file=sys.stderr)
        return 2
    if not spawn_command and not args.dry_run:
        print("Need SPAWN_COMMAND (governance key required for PATCH)", file=sys.stderr)
        return 2

    base = normalize_base(base_url)
    mark_in_progress = not args.no_mark_in_progress
    in_flight: set[str] = set()

    while True:
        try:
            payload = fetch_open_work(base, org_id, api_key)
            items = payload.get("work") or []
            print(
                f"[org-loop-spawn] org={org_id} open_count={payload.get('count', len(items))}",
                flush=True,
            )
            for item in items:
                wid = work_id(item)
                if not wid or wid in in_flight:
                    continue
                in_flight.add(wid)
                process_item(
                    base=base,
                    org_id=org_id,
                    api_key=api_key,
                    item=item,
                    spawn_command=spawn_command or "echo work={{work_id}}",
                    dry_run=args.dry_run,
                    mark_in_progress=mark_in_progress,
                )
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            print(f"[org-loop-spawn] HTTP {e.code}: {body}", file=sys.stderr)
            if args.once:
                return 1
        except Exception as e:
            print(f"[org-loop-spawn] error: {e}", file=sys.stderr)
            if args.once:
                return 1

        if args.once:
            return 0
        time.sleep(max(1, interval))


if __name__ == "__main__":
    raise SystemExit(main())
