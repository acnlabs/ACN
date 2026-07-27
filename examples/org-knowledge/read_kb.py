#!/usr/bin/env python3
"""CLI: load Org knowledge (filesystem sidecar) for a member agent before work.

Examples:
  ORG_KB_ORG_ID=org_demo python3 read_kb.py
  python3 read_kb.py --org org_demo --ref orgkb://org_demo/sop/release.md
  echo '{"kb_refs":[{"uri":"orgkb://org_demo/charter.md"}]}' | python3 read_kb.py --from-json -

Trust: see kb.py module docstring / README — no ACN ACL.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow `python3 read_kb.py` from any cwd.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from kb import (  # noqa: E402
    KbRef,
    default_kb_root,
    default_refs_for_org,
    format_bundle,
    read_refs,
    resolve_orgkb_uri,
)


def _parse_refs_from_json(raw: str) -> list[KbRef]:
    data = json.loads(raw)
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("kb_refs") or data.get("refs") or []
    else:
        raise ValueError("JSON must be object with kb_refs or a list")
    return [
        KbRef.from_mapping(x) if isinstance(x, dict) else KbRef(uri=str(x))
        for x in items
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read Org knowledge base (sidecar)")
    p.add_argument("--org", default="", help="org_id (or ORG_KB_ORG_ID); pins URI org")
    p.add_argument(
        "--root",
        default="",
        help="ORG_KB_ROOT (default: examples/org-knowledge/data)",
    )
    p.add_argument(
        "--ref",
        action="append",
        default=[],
        help="orgkb://… or relative path (repeatable)",
    )
    p.add_argument(
        "--from-json",
        default="",
        help="path to JSON with kb_refs, or '-' for stdin",
    )
    p.add_argument(
        "--max-chars",
        type=int,
        default=24_000,
        help="truncate bundled output (default 24000)",
    )
    p.add_argument(
        "--json-out",
        action="store_true",
        help="emit JSON list of {uri,title,path,text} instead of markdown bundle",
    )
    args = p.parse_args(argv)

    if args.root:
        os.environ["ORG_KB_ROOT"] = args.root
    root = default_kb_root()

    org_id = (args.org or os.environ.get("ORG_KB_ORG_ID", "")).strip()
    refs: list[KbRef] = []

    if args.from_json:
        raw = (
            sys.stdin.read()
            if args.from_json == "-"
            else Path(args.from_json).read_text(encoding="utf-8")
        )
        refs.extend(_parse_refs_from_json(raw))

    for r in args.ref:
        refs.append(KbRef(uri=r.strip()))

    if not refs:
        if not org_id:
            print(
                "Need --org / ORG_KB_ORG_ID, or --ref / --from-json",
                file=sys.stderr,
            )
            return 2
        refs = default_refs_for_org(org_id)
        os.environ.setdefault("ORG_KB_ORG_ID", org_id)

    # Pin org when provided: reject cross-org URIs.
    if org_id:
        os.environ.setdefault("ORG_KB_ORG_ID", org_id)

    try:
        pairs = read_refs(refs, root=root, expected_org_id=org_id or None)
    except (ValueError, FileNotFoundError, OSError, json.JSONDecodeError) as e:
        print(f"[read_kb] error: {e}", file=sys.stderr)
        return 1

    if args.json_out:
        payload = []
        for ref, text in pairs:
            _oid, path = resolve_orgkb_uri(ref.uri, root=root)
            payload.append(
                {
                    "uri": ref.uri,
                    "title": ref.title,
                    "path": str(path),
                    "text": text,
                }
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_bundle(pairs, max_chars=args.max_chars), end="")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
