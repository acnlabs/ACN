#!/usr/bin/env python3
"""CLI: agent contribute to Org knowledge (filesystem sidecar).

Examples:
  python3 contribute_kb.py --org org_demo --from-agent agt_1 \\
    --path sop/learned.md --body-file ./note.md

  echo '# tip' | python3 contribute_kb.py --org org_demo --from-agent agt_1 \\
    --path skills/tip.md --body -

  # charter (Owner only)
  python3 contribute_kb.py --org org_demo --from-agent agt_owner --as-owner \\
    --path charter.md --body-file ./charter.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from contribute import ContributeProposal, contribute  # noqa: E402
from kb import default_kb_root  # noqa: E402


def _read_body(args: argparse.Namespace) -> str:
    if args.body is not None:
        if args.body == "-":
            return sys.stdin.read()
        return args.body
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8")
    if args.from_json:
        raw = (
            sys.stdin.read()
            if args.from_json == "-"
            else Path(args.from_json).read_text(encoding="utf-8")
        )
        data = json.loads(raw)
        return str(data.get("body") or "")
    raise ValueError("need --body, --body-file, or --from-json")


def _proposal_from_json(raw: str, *, defaults: argparse.Namespace) -> ContributeProposal:
    data = json.loads(raw)
    org_id = str(data.get("org_id") or defaults.org or "").strip()
    path = str(data.get("path") or defaults.path or "").strip()
    body = str(data.get("body") or "")
    from_agent = str(data.get("from_agent") or defaults.from_agent or "").strip()
    if not org_id or not path or not from_agent:
        raise ValueError("JSON needs org_id, path, from_agent (and body)")
    if not body and defaults.body_file:
        body = Path(defaults.body_file).read_text(encoding="utf-8")
    return ContributeProposal(
        org_id=org_id,
        path=path,
        body=body,
        from_agent=from_agent,
        work_id=str(data.get("work_id") or defaults.work_id or ""),
        title=str(data.get("title") or defaults.title or ""),
        as_owner=bool(data.get("as_owner") or defaults.as_owner),
        force=bool(data.get("force") or defaults.force),
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Contribute to Org knowledge base")
    p.add_argument("--org", default="", help="org_id")
    p.add_argument("--root", default="", help="ORG_KB_ROOT")
    p.add_argument("--from-agent", default="", help="contributing agent_id")
    p.add_argument("--path", default="", help="relative .md path under org")
    p.add_argument("--body", default=None, help="markdown body, or '-' for stdin")
    p.add_argument("--body-file", default="", help="read body from file")
    p.add_argument("--work-id", default="", help="optional work_id provenance")
    p.add_argument("--title", default="", help="optional title note")
    p.add_argument(
        "--as-owner",
        action="store_true",
        help="allow charter / unrestricted paths",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="overwrite on conflict instead of disputed/",
    )
    p.add_argument(
        "--from-json",
        default="",
        help="proposal JSON path, or '-' for stdin",
    )
    p.add_argument("--json-out", action="store_true", help="machine-readable result")
    args = p.parse_args(argv)

    if args.root:
        os.environ["ORG_KB_ROOT"] = args.root
    root = default_kb_root()

    try:
        if args.from_json:
            raw = (
                sys.stdin.read()
                if args.from_json == "-"
                else Path(args.from_json).read_text(encoding="utf-8")
            )
            prop = _proposal_from_json(raw, defaults=args)
            if not prop.body:
                # Allow body alongside JSON via --body-file
                if args.body is not None or args.body_file:
                    prop = ContributeProposal(
                        org_id=prop.org_id,
                        path=prop.path,
                        body=_read_body(args),
                        from_agent=prop.from_agent,
                        work_id=prop.work_id,
                        title=prop.title,
                        as_owner=prop.as_owner,
                        force=prop.force,
                    )
        else:
            org_id = (args.org or os.environ.get("ORG_KB_ORG_ID", "")).strip()
            from_agent = (
                args.from_agent or os.environ.get("ORG_KB_FROM_AGENT", "")
            ).strip()
            if not org_id or not args.path or not from_agent:
                print(
                    "Need --org, --path, --from-agent (or --from-json)",
                    file=sys.stderr,
                )
                return 2
            prop = ContributeProposal(
                org_id=org_id,
                path=args.path,
                body=_read_body(args),
                from_agent=from_agent,
                work_id=args.work_id,
                title=args.title,
                as_owner=args.as_owner,
                force=args.force,
            )

        result = contribute(prop, root=root)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"[contribute_kb] error: {e}", file=sys.stderr)
        return 1

    payload = {
        "decision": result.decision.value,
        "path": result.path,
        "abs_path": result.abs_path,
        "reason": result.reason,
        "org_id": prop.org_id,
        "from_agent": prop.from_agent,
    }
    if args.json_out:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"[contribute_kb] {result.decision.value}: {result.path}"
            + (f" ({result.reason})" if result.reason else ""),
            flush=True,
        )
        if result.abs_path:
            print(result.abs_path, flush=True)

    if result.decision.value == "rejected":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
