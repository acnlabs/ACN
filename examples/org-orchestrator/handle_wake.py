#!/usr/bin/env python3
"""Member-side: parse acn.org.work_wake from stdin, validate work, dedupe by key.

For Mode B:
  acn listen --runtime command --wake-exec 'python3 handle_wake.py'

Env:
  ACN_BASE_URL, ACN_API_KEY — member key (list work / agents/me)
  HANDLE_WAKE_IDEM_PATH — member-side seen keys (default ./.handle-wake-idem.json)
  HANDLE_WAKE_SKIP_FETCH — if set, parse only (no API / no dedupe claim)

Exit 0: handled, deduped, ignored non-wake, or skip-fetch parse ok.
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
from idempotency import IdempotencyStore

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
    if payload.get("type") == WAKE_TYPE:
        texts.append(json.dumps(payload, ensure_ascii=False))

    raw = payload.get("raw")
    if isinstance(raw, dict):
        params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
        message = params.get("message") if isinstance(params, dict) else None
        if isinstance(message, dict):
            texts.extend(_texts_from_a2a_message(message))
        texts.append(json.dumps(raw, ensure_ascii=False))

    if isinstance(payload.get("message"), dict):
        texts.extend(_texts_from_a2a_message(payload["message"]))
    if isinstance(payload.get("text"), str):
        texts.append(payload["text"])

    texts.append(json.dumps(payload, ensure_ascii=False))
    return texts


def parse_wake(payload: Any) -> dict[str, Any] | None:
    for text in _candidate_strings(payload):
        try:
            obj = json.loads(text) if isinstance(text, str) else text
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and obj.get("type") == WAKE_TYPE:
            return obj
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


def resolve_idempotency_key(wake: dict[str, Any]) -> str:
    """Prefer envelope key; else derive org:work:wake:1:assignee."""
    key = str(wake.get("idempotency_key") or "").strip()
    if key:
        return key
    org_id = str(wake.get("org_id") or "").strip()
    work_id = str(wake.get("work_id") or "").strip()
    assignee = str(wake.get("assignee") or "").strip()
    if org_id and work_id and assignee:
        return f"{org_id}:{work_id}:wake:1:{assignee}"
    return ""


def assignee_matches_me(
    *,
    envelope_assignee: str,
    work_assignee: str | None,
    my_id: str,
) -> tuple[bool, str]:
    """Require API assignee == me. Envelope assignee, if set, must also match."""
    api = str(work_assignee or "").strip()
    if not api:
        return False, "work has no assignee"
    if not my_id:
        return False, "agents/me missing agent_id"
    if api != my_id:
        return False, f"API assignee {api} != me {my_id}"
    env = str(envelope_assignee or "").strip()
    if env and env != my_id:
        return False, f"envelope assignee {env} != me {my_id}"
    return True, ""


def main() -> int:
    base_url = os.environ.get("ACN_BASE_URL", "").strip()
    api_key = os.environ.get("ACN_API_KEY", "").strip()
    skip_fetch = os.environ.get("HANDLE_WAKE_SKIP_FETCH", "").strip() in (
        "1",
        "true",
        "yes",
    )
    idem_path = os.environ.get(
        "HANDLE_WAKE_IDEM_PATH",
        os.path.join(os.getcwd(), ".handle-wake-idem.json"),
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
    idem = resolve_idempotency_key(wake)
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
    if not idem:
        print(
            "[handle_wake] wake missing idempotency_key "
            "(and cannot derive from org/work/assignee)",
            file=sys.stderr,
        )
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
            f"[handle_wake] envelope assignee {assignee} != me {my_id} — ignore",
            flush=True,
        )
        return 0

    try:
        work = get_work(base, org_id, work_id, api_key)
    except WorkNotFoundError:
        print(f"[handle_wake] work not found: {work_id}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        print(
            f"[handle_wake] list work failed HTTP {e.code}: {e.reason}",
            file=sys.stderr,
        )
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

    ok, reason = assignee_matches_me(
        envelope_assignee=assignee,
        work_assignee=str(work_assignee) if work_assignee is not None else None,
        my_id=my_id,
    )
    if not ok:
        print(f"[handle_wake] {reason} — stop", flush=True)
        return 0

    store = IdempotencyStore(idem_path)
    try:
        claimed = store.try_claim(idem, work_id=work_id, assignee=my_id)
    except OSError as e:
        print(f"[handle_wake] idempotency claim failed: {e}", file=sys.stderr)
        return 1
    if not claimed:
        print(f"[handle_wake] deduped idem={idem} — already handled", flush=True)
        return 0

    print(
        "[handle_wake] OK — run your L1 on this work; "
        "ask governance to PATCH done|cancelled when finished.",
        flush=True,
    )
    print(json.dumps({"wake": wake, "work": work}, ensure_ascii=False, indent=2), flush=True)
    try:
        store.confirm(idem)
    except OSError as e:
        print(f"[handle_wake] idempotency confirm failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
