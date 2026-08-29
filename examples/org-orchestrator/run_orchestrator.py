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
    fetch_all_work,
    fetch_members,
    fetch_open_work,
    fetch_org,
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


def _knowledge_plugin_disabled() -> bool:
    """True only when runner explicitly mirrors ``plugins.knowledge=noop``."""
    return (os.environ.get("ORG_PLUGINS_KNOWLEDGE") or "").strip().lower() == "noop"


def _kb_refs_for_item(org_id: str, item: dict) -> list[dict]:
    """Optional kb_refs: work field → ORG_KB_REFS_JSON → ORG_KB_ATTACH_DEFAULTS.

    When ``ORG_PLUGINS_KNOWLEDGE=noop``, never attach kb_refs. Unset env keeps
    prior sidecar behavior (opt-in via ORG_KB_*).
    """
    if _knowledge_plugin_disabled():
        return []

    raw = item.get("kb_refs")
    if isinstance(raw, list) and raw:
        return [x for x in raw if isinstance(x, dict) and x.get("uri")]

    env_json = os.environ.get("ORG_KB_REFS_JSON", "").strip()
    if env_json:
        try:
            parsed = json.loads(env_json)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list) and parsed:
            return [x for x in parsed if isinstance(x, dict) and x.get("uri")]
        if isinstance(parsed, dict) and isinstance(parsed.get("kb_refs"), list):
            return [
                x
                for x in parsed["kb_refs"]
                if isinstance(x, dict) and x.get("uri")
            ]

    attach = os.environ.get("ORG_KB_ATTACH_DEFAULTS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if attach:
        return [{"uri": f"orgkb://{org_id}/charter.md", "title": "charter.md"}]
    return []


def build_envelope(
    org_id: str,
    item: dict,
    execution_env: dict | None = None,
) -> dict:
    wid = work_id(item)
    assignee = assignee_id(item)
    envelope = {
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
    kb_refs = _kb_refs_for_item(org_id, item)
    if kb_refs:
        envelope["kb_refs"] = kb_refs
    env = execution_env if isinstance(execution_env, dict) else None
    kind = (env or {}).get("kind")
    if env and kind and kind != "none":
        envelope["execution_env"] = env
        ws_id = env.get("workspace_id")
        if isinstance(ws_id, str) and ws_id:
            envelope["workspace_id"] = ws_id
    return envelope


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
    execution_env: dict | None = None,
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
    envelope = build_envelope(org_id, item, execution_env)
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

    # Metrics observe (§3.3): unset path = off; wake/PATCH path unchanged (M0-S3).
    observe_path = os.environ.get("ORG_METRICS_OBSERVE_PATH", "").strip()
    observe_store = None
    if observe_path:
        from work_observe import ObservationStore

        observe_store = ObservationStore(observe_path)

    print(
        f"[org-orchestrator] from_agent={from_agent} org={org_id} idem={store_path}"
        + (f" observe={observe_path}" if observe_store else ""),
        flush=True,
    )

    while True:
        try:
            members = active_member_ids(fetch_members(base, org_id, api_key))
            try:
                org_view = fetch_org(base, org_id, api_key)
                execution_env = org_view.get("execution_env")
            except urllib.error.HTTPError as org_err:
                print(
                    f"[org-orchestrator] GET org failed HTTP {org_err.code}: "
                    f"{org_err.reason} — waking without execution_env",
                    file=sys.stderr,
                )
                execution_env = None
            payload = fetch_open_work(base, org_id, api_key)
            items = payload.get("work") or []
            print(
                f"[org-orchestrator] open_count={payload.get('count', len(items))} "
                f"members={len(members)}",
                flush=True,
            )
            if observe_store is not None:
                try:
                    all_payload = fetch_all_work(base, org_id, api_key)
                    wrote = observe_store.observe(all_payload.get("work") or [])
                    if wrote:
                        print(
                            f"[org-orchestrator] observe wrote={len(wrote)}",
                            flush=True,
                        )
                except Exception as obs_err:
                    print(
                        f"[org-orchestrator] observe skipped: {obs_err}",
                        file=sys.stderr,
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
                    execution_env=execution_env if isinstance(execution_env, dict) else None,
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
