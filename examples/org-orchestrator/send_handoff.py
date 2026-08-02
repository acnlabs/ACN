#!/usr/bin/env python3
"""Send acn.org.work_handoff after governance has reassigned work (contract v0).

Env: ACN_BASE_URL, ACN_API_KEY (sender), ACN_ORG_ID
Usage:
  python3 send_handoff.py --work work_… --to agt_… [--note '…'] [--generation 1]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from acn_client_min import agents_me, normalize_base, send_message

HANDOFF_TYPE = "acn.org.work_handoff"


def build_envelope(
    *,
    org_id: str,
    work_id: str,
    from_agent: str,
    to_agent: str,
    title: str,
    note: str,
    generation: int,
    kb_refs: list | None,
) -> dict:
    gen = max(1, int(generation))
    env: dict = {
        "type": HANDOFF_TYPE,
        "schema_version": 1,
        "idempotency_key": f"{org_id}:{work_id}:handoff:{gen}:{from_agent}:{to_agent}",
        "org_id": org_id,
        "work_id": work_id,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "title": title,
        "hint": "Fetch work with Org API; confirm assignee is you; then execute.",
    }
    if note:
        env["note"] = note
    if kb_refs:
        env["kb_refs"] = kb_refs
    return env


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Send Org work_handoff")
    p.add_argument("--work", required=True, help="work_id")
    p.add_argument("--to", required=True, help="target agent_id")
    p.add_argument("--org", default="", help="org_id (or ACN_ORG_ID)")
    p.add_argument("--title", default="", help="optional title snapshot")
    p.add_argument("--note", default="", help="short handoff note")
    p.add_argument("--generation", type=int, default=1)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    base_url = os.environ.get("ACN_BASE_URL", "").strip()
    api_key = os.environ.get("ACN_API_KEY", "").strip()
    org_id = (args.org or os.environ.get("ACN_ORG_ID", "")).strip()
    if not base_url or not api_key or not org_id:
        print("Need ACN_BASE_URL, ACN_API_KEY, and --org or ACN_ORG_ID", file=sys.stderr)
        return 2

    base = normalize_base(base_url)
    me = agents_me(base, api_key)
    from_agent = str(me.get("agent_id") or "")
    if not from_agent:
        print("agents/me missing agent_id", file=sys.stderr)
        return 1

    kb_refs = None
    if os.environ.get("ORG_KB_ATTACH_DEFAULTS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        kb_refs = [{"uri": f"orgkb://{org_id}/charter.md", "title": "charter.md"}]

    env = build_envelope(
        org_id=org_id,
        work_id=args.work.strip(),
        from_agent=from_agent,
        to_agent=args.to.strip(),
        title=args.title.strip() or args.work.strip(),
        note=args.note,
        generation=args.generation,
        kb_refs=kb_refs,
    )
    text = json.dumps(env, ensure_ascii=False)
    if args.dry_run:
        print(text)
        return 0

    send_message(
        base,
        api_key,
        from_agent=from_agent,
        target_agent=args.to.strip(),
        text=text,
    )
    print(
        f"[send_handoff] OK from={from_agent} to={args.to} "
        f"work={args.work} idem={env['idempotency_key']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
