#!/usr/bin/env python3
"""C1: poll Org open work and log titles (no spawn, no PATCH)."""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error

from acn_org import fetch_open_work, normalize_base, work_id


def log_batch(payload: dict) -> None:
    org_id = payload.get("org_id", "?")
    items = payload.get("work") or []
    count = payload.get("count", len(items))
    print(f"[org-loop-spawn] org={org_id} open_count={count}", flush=True)
    for w in items:
        wid = work_id(w) or "?"
        title = w.get("title") or ""
        status = w.get("status") or "?"
        print(f"  - {wid}  status={status}  {title}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Single poll then exit (default: loop)",
    )
    args = parser.parse_args()

    base_url = os.environ.get("ACN_BASE_URL", "").strip()
    org_id = os.environ.get("ACN_ORG_ID", "").strip()
    api_key = os.environ.get("ACN_API_KEY", "").strip()
    interval = int(os.environ.get("POLL_INTERVAL_SEC", "30"))

    if not base_url or not org_id or not api_key:
        print("Need ACN_BASE_URL, ACN_ORG_ID, ACN_API_KEY", file=sys.stderr)
        return 2

    base = normalize_base(base_url)
    while True:
        try:
            payload = fetch_open_work(base, org_id, api_key)
            log_batch(payload)
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
