#!/usr/bin/env python3
"""Refresh agent performance stats for auto-collab matching.

Preferred path (ACN Kernel SoT):
  POST /agents/{id}/performance/refresh  → writes metadata.performance

Fallback (offline / old servers):
  --fixture / --local-cache → local PERF_CACHE_PATH for matcher merge

Usage:
  ACN_API_KEY=acn_… python3 run_perf_enrich.py --self
  ACN_INTERNAL_API_TOKEN=… python3 run_perf_enrich.py --agent-id UUID
  python3 run_perf_enrich.py --fixture history.json --agent-id agt_demo --local-cache
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from acn_client import agents_me, normalize_base, refresh_performance  # noqa: E402
from completion import (  # noqa: E402
    DEFAULT_MIN_SAMPLES,
    PerfCache,
    aggregate_history,
    performance_patch_from_aggregate,
)


def enrich_local(
    *,
    cache: PerfCache,
    agent_id: str,
    items: list[dict],
    min_samples: int,
) -> dict:
    agg = aggregate_history(items, min_samples=min_samples)
    patch = performance_patch_from_aggregate(agg)
    if not patch:
        print(
            f"[perf-enrich] {agent_id}: insufficient settled samples "
            f"({agg.get('settled', 0)} < {min_samples}) — cache not updated",
            flush=True,
        )
        return {"agent_id": agent_id, "updated": False, "aggregate": agg, "via": "local"}
    cache.upsert(
        agent_id,
        patch,
        meta={"settled": agg.get("settled"), "success": agg.get("success")},
    )
    print(
        f"[perf-enrich] {agent_id}: local cache {patch} (settled={agg.get('settled')})",
        flush=True,
    )
    return {
        "agent_id": agent_id,
        "updated": True,
        "performance": patch,
        "aggregate": agg,
        "via": "local",
    }


def main() -> int:
    p = argparse.ArgumentParser(description="ACN agent performance enricher")
    p.add_argument("--self", action="store_true", help="Refresh caller's own agent")
    p.add_argument("--agent-id", action="append", dest="agent_ids")
    p.add_argument("--fixture", help="JSON history (forces --local-cache)")
    p.add_argument(
        "--local-cache",
        action="store_true",
        help="Write PERF_CACHE instead of calling Kernel refresh API",
    )
    p.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    p.add_argument(
        "--cache",
        default=os.environ.get("PERF_CACHE_PATH")
        or str(_HERE / ".auto-collab-perf-cache.json"),
    )
    args = p.parse_args()

    results: list[dict] = []

    if args.fixture:
        aids = list(args.agent_ids or [])
        if len(aids) != 1:
            print("--fixture requires exactly one --agent-id", file=sys.stderr)
            return 2
        raw = json.loads(Path(args.fixture).read_text())
        items = raw.get("items") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            print("fixture must be list or {items:[…]}", file=sys.stderr)
            return 2
        cache = PerfCache(args.cache)
        results.append(
            enrich_local(
                cache=cache,
                agent_id=aids[0],
                items=[i for i in items if isinstance(i, dict)],
                min_samples=args.min_samples,
            )
        )
        print(json.dumps({"cache": args.cache, "results": results}, indent=2))
        return 0

    base = normalize_base(os.environ.get("ACN_BASE_URL") or "http://127.0.0.1:8000")
    api_key = (os.environ.get("ACN_API_KEY") or "").strip()
    internal = (
        os.environ.get("ACN_INTERNAL_API_TOKEN")
        or os.environ.get("INTERNAL_API_TOKEN")
        or ""
    ).strip()

    targets: list[str] = []
    if args.self:
        if not api_key:
            print("ACN_API_KEY required for --self", file=sys.stderr)
            return 2
        me = agents_me(base, api_key)
        mid = str(me.get("agent_id") or "").strip()
        if not mid:
            print("agents/me missing agent_id", file=sys.stderr)
            return 2
        targets.append(mid)
    targets.extend(args.agent_ids or [])

    deduped: list[str] = []
    seen: set[str] = set()
    for t in targets:
        if t and t not in seen:
            seen.add(t)
            deduped.append(t)
    targets = deduped
    if not targets:
        print("Provide --self and/or --agent-id (or --fixture)", file=sys.stderr)
        return 2

    use_local = args.local_cache
    for aid in targets:
        if use_local:
            print(
                f"[perf-enrich] {aid}: --local-cache without --fixture "
                "needs history fetch; prefer Kernel refresh",
                file=sys.stderr,
            )
            results.append({"agent_id": aid, "updated": False, "error": "use_kernel"})
            continue
        try:
            if internal:
                body = refresh_performance(
                    base, api_key or internal, aid, internal_token=internal
                )
            elif api_key:
                body = refresh_performance(base, api_key, aid)
            else:
                print("Need ACN_API_KEY or ACN_INTERNAL_API_TOKEN", file=sys.stderr)
                return 2
            perf = body.get("performance") if isinstance(body, dict) else None
            print(f"[perf-enrich] {aid}: kernel refresh → {perf}", flush=True)
            results.append(
                {
                    "agent_id": aid,
                    "updated": True,
                    "performance": perf,
                    "via": "kernel",
                }
            )
        except urllib.error.HTTPError as e:
            print(
                f"[perf-enrich] {aid} refresh HTTP {e.code}: {e.reason}",
                file=sys.stderr,
            )
            results.append(
                {"agent_id": aid, "updated": False, "error": str(e.reason), "via": "kernel"}
            )

    print(json.dumps({"results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
