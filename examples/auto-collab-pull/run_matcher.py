#!/usr/bin/env python3
"""MVP-2a/2b: tag + semantic recall → L1 invites → MVP-1 puller.

External sidecar — does NOT live in ACN Kernel.
See docs/auto-collab-pull-mvp-v0.md §3.
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

# Path used below for PERF_CACHE_PATH default

from acn_client import (  # noqa: E402
    agents_me,
    get_task,
    invite_agent,
    normalize_base,
    search_agents,
)
from effective_cap import effective_cap_from_task  # noqa: E402
from completion import PerfCache  # noqa: E402
from match import (  # noqa: E402
    MatchEmptyError,
    MatchForbiddenError,
    MatchMode,
    plan_invites_for_task,
    recall_limit,
    required_tags_of,
)
from run_puller import process_task  # noqa: E402

_ORCH = _HERE.parent / "org-orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))
from idempotency import IdempotencyStore  # noqa: E402


def materialize_invites(
    *,
    base: str,
    api_key: str,
    task_id: str,
    invitee_ids: list[str],
    dry_run: bool,
) -> list[str]:
    """POST /invite for each id; return successfully invited ids."""
    ok: list[str] = []
    for aid in invitee_ids:
        if dry_run:
            print(f"  dry-run invite → {aid}", flush=True)
            ok.append(aid)
            continue
        try:
            invite_agent(base, api_key, task_id, aid)
            print(f"  invite → {aid} ok", flush=True)
            ok.append(aid)
        except urllib.error.HTTPError as e:
            # Already invited / self / etc. — continue others
            print(
                f"  invite → {aid} failed HTTP {e.code}: {e.reason}",
                file=sys.stderr,
            )
    return ok


def match_and_invite(
    *,
    base: str,
    api_key: str,
    task_id: str,
    dry_run: bool,
    status: str = "online",
    mode: MatchMode = "hybrid",
) -> dict:
    task = get_task(base, task_id, api_key)
    tags = required_tags_of(task)
    cap = effective_cap_from_task(task)
    k = recall_limit(cap)
    print(
        f"[auto-collab-match] task={task_id} mode={mode} tags={tags} "
        f"cap={cap} recall_k={k}",
        flush=True,
    )

    me = agents_me(base, api_key)
    my_id = str(me.get("agent_id") or "").strip()
    exclude = {my_id} if my_id else set()

    try:
        # Tag-prefilter when tags mode; hybrid/semantic widen the online pool.
        prefilter = tags if mode == "tags" and tags else None
        agents = search_agents(
            base,
            api_key,
            tags=prefilter,
            status=status,
            limit=k if mode == "tags" else min(200, max(k * 4, 32)),
        )
        if mode != "tags" or (tags and len(agents) < k):
            more = search_agents(
                base, api_key, tags=None, status=status, limit=min(200, k * 4)
            )
            seen = {str(a.get("agent_id")) for a in agents}
            for a in more:
                aid = str(a.get("agent_id") or "")
                if aid and aid not in seen:
                    agents.append(a)
                    seen.add(aid)

        # Kernel already exposes metadata.performance on list rows.
        # Optional local cache overlays when present (offline / backfill).
        cache_path = os.environ.get("PERF_CACHE_PATH") or str(
            _HERE / ".auto-collab-perf-cache.json"
        )
        if Path(cache_path).is_file():
            agents = PerfCache(cache_path).merge_into_agents(agents)
            print(
                f"[auto-collab-match] merged optional perf cache {cache_path}",
                flush=True,
            )

        picked = plan_invites_for_task(
            task, agents, exclude_ids=exclude, mode=mode
        )
    except MatchForbiddenError as e:
        print(f"[auto-collab-match] FORBIDDEN: {e}", file=sys.stderr)
        return {"invited": [], "error": "forbidden", "detail": str(e)}
    except MatchEmptyError as e:
        print(f"[auto-collab-match] EMPTY: {e}", file=sys.stderr)
        return {"invited": [], "error": "empty", "detail": str(e)}

    print(f"[auto-collab-match] selected {len(picked)}: {picked}", flush=True)
    invited = materialize_invites(
        base=base,
        api_key=api_key,
        task_id=task_id,
        invitee_ids=picked,
        dry_run=dry_run,
    )
    return {
        "invited": invited,
        "selected": picked,
        "tags": tags,
        "cap": cap,
        "k": k,
        "mode": mode,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="ACN auto-collab MVP-2 matcher sidecar")
    p.add_argument("--task-id", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--no-pull",
        action="store_true",
        help="Only materialize invites; do not run MVP-1 puller",
    )
    p.add_argument(
        "--status",
        default="online",
        help="Agent search status filter (default online)",
    )
    p.add_argument(
        "--mode",
        choices=("tags", "semantic", "hybrid"),
        default=None,
        help="Recall mode (default: MATCH_MODE env or hybrid)",
    )
    args = p.parse_args()

    base = normalize_base(os.environ.get("ACN_BASE_URL") or "http://127.0.0.1:8000")
    api_key = (os.environ.get("ACN_API_KEY") or "").strip()
    if not api_key:
        print("ACN_API_KEY required", file=sys.stderr)
        return 2

    mode_raw = (args.mode or os.environ.get("MATCH_MODE") or "hybrid").strip().lower()
    if mode_raw not in ("tags", "semantic", "hybrid"):
        print(f"invalid MATCH_MODE={mode_raw!r}", file=sys.stderr)
        return 2
    mode: MatchMode = mode_raw  # type: ignore[assignment]

    result = match_and_invite(
        base=base,
        api_key=api_key,
        task_id=args.task_id,
        dry_run=args.dry_run,
        status=args.status,
        mode=mode,
    )
    if result.get("error") == "forbidden":
        return 3
    if result.get("error") == "empty":
        return 4
    if not result.get("invited") and not args.dry_run:
        print("[auto-collab-match] no invites materialized", file=sys.stderr)
        return 4

    if args.no_pull:
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0

    idem_path = os.environ.get("PULLER_IDEM_PATH") or str(
        _HERE / ".auto-collab-pull-idem.json"
    )
    store = IdempotencyStore(idem_path)
    me = agents_me(base, api_key)
    from_agent = str(me.get("agent_id") or "")
    pull = process_task(
        base=base,
        api_key=api_key,
        from_agent=from_agent,
        task_id=args.task_id,
        store=store,
        dry_run=args.dry_run,
    )
    out = {"match": result, "pull": pull}
    print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
