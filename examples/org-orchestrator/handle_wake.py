#!/usr/bin/env python3
"""Member-side: parse acn.org.work_wake from stdin event/text, then GET work.

For Mode B:
  acn listen --runtime command --wake-exec 'python3 handle_wake.py'

Env: ACN_BASE_URL, ACN_API_KEY (member key; read work / membership).
Exit 0: handled wake, non-wake ignored, or dry parse ok.
Exit 1: wake recognized but validation/API failed.
Exit 2: misconfiguration.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
from typing import Any

from acn_client_min import WorkNotFoundError, agents_me, get_work, normalize_base

WAKE_TYPE = "acn.org.work_wake"


def _load_stdin() -> Any:
    raw = sys.stdin.read()
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()


def _texts_from_a2a_message(msg: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if isinstance(msg.get("text"), str):
        out.append(msg["text"])
    parts = msg.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                out.append(part["text"])
    return out


def _candidate_strings(payload: Any) -> list[str]:
    if payload is None:
        return []
    if isinstance(payload, str):
        return [payload]
    if not isinstance(payload, dict):
        return [str(payload)]

    texts: list[str] = []
    # Direct wake JSON object
    if payload.get("type") == WAKE_TYPE:
        texts.append(json.dumps(payload, ensure_ascii=False))

    # Mode B normalized event: raw = JSON-RPC body
    raw = payload.get("raw")
    if isinstance(raw, dict):
        params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
        message = params.get("message") if isinstance(params, dict) else None
        if isinstance(message, dict):
            texts.extend(_texts_from_a2a_message(message))
        texts.append(json.dumps(raw, ensure_ascii=False))

    # Convenience shapes
    if isinstance(payload.get("message"), dict):
        texts.extend(_texts_from_a2a_message(payload["message"]))
    if isinstance(payload.get("text"), str):
        texts.append(payload["text"])

    # Whole payload as last resort string search
    texts.append(json.dumps(payload, ensure_ascii=False))
    return texts


def parse_wake(payload: Any) -> dict[str, Any] | None:
    for text in _candidate_strings(payload):
        # Exact JSON object
        try:
            obj = json.loads(text) if isinstance(text, str) else text
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and obj.get("type") == WAKE_TYPE:
            return obj
        # Embedded JSON object in a larger string
        if not isinstance(text, str):
            continue
        start = text.find('{"type": "acn.org.work_wake"')
        if start < 0:
            start = text.find('{"type":"acn.org.work_wake"')
        if start < 0:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == WAKE_TYPE:
            return obj
    return None


def main() -> int:
    base_url = os.environ.get("ACN_BASE_URL", "").strip()
    api_key = os.environ.get("ACN_API_KEY", "").strip()
    skip_fetch = os.environ.get("HANDLE_WAKE_SKIP_FETCH", "").strip() in (
        "1",
        "true",
        "yes",
    )

    payload = _load_stdin()
    if payload is None:
        print("[handle_wake] empty stdin — ignore", flush=True)
        return 0

    wake = parse_wake(payload)
    if wake is None:
        print("[handle_wake] not an acn.org.work_wake — ignore", flush=True)
        return 0

    org_id = str(wake.get("org_id") or "")
    work_id = str(wake.get("work_id") or "")
    idem = str(wake.get("idempotency_key") or "")
    assignee = str(wake.get("assignee") or "")
    print(
        f"[handle_wake] wake org={org_id} work={work_id} "
        f"assignee={assignee} idem={idem}",
        flush=True,
    )

    if skip_fetch:
        print("[handle_wake] HANDLE_WAKE_SKIP_FETCH set — not calling API", flush=True)
        print(json.dumps(wake, ensure_ascii=False, indent=2), flush=True)
        return 0

    if not base_url or not api_key:
        print("Need ACN_BASE_URL and ACN_API_KEY", file=sys.stderr)
        return 2
    if not org_id or not work_id:
        print("[handle_wake] wake missing org_id/work_id", file=sys.stderr)
        return 1

    base = normalize_base(base_url)
    try:
        me = agents_me(base, api_key)
        my_id = str(me.get("agent_id") or "")
    except Exception as e:
        print(f"[handle_wake] agents/me failed: {e}", file=sys.stderr)
        return 1

    if assignee and my_id and assignee != my_id:
        print(
            f"[handle_wake] assignee {assignee} != me {my_id} — ignore",
            flush=True,
        )
        return 0

    try:
        work = get_work(base, org_id, work_id, api_key)
    except WorkNotFoundError:
        print(f"[handle_wake] work not found: {work_id}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        print(f"[handle_wake] GET work failed HTTP {e.code}: {e.reason}", file=sys.stderr)
        return 1

    status = work.get("status")
    work_assignee = work.get("assignee_agent_id") or work.get("assignee")
    print(
        f"[handle_wake] work status={status!r} assignee={work_assignee!r} "
        f"title={work.get('title')!r}",
        flush=True,
    )
    if status not in ("todo", "in_progress"):
        print(f"[handle_wake] work not open ({status}) — stop", flush=True)
        return 0
    if work_assignee and my_id and str(work_assignee) != my_id:
        print(
            f"[handle_wake] API assignee {work_assignee} != me {my_id} — stop",
            flush=True,
        )
        return 0

    print(
        "[handle_wake] OK — run your L1 on this work; "
        "ask governance to PATCH done|cancelled when finished.",
        flush=True,
    )
    print(json.dumps({"wake": wake, "work": work}, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
