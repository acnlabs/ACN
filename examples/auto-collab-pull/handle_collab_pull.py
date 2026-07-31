#!/usr/bin/env python3
"""Member-side: parse acn.task.collab_pull from stdin, fetch task, dedupe.

For Mode B:
  acn listen --runtime command --wake-exec 'python3 handle_collab_pull.py'

Env:
  ACN_BASE_URL, ACN_API_KEY — member key
  HANDLE_COLLAB_PULL_IDEM_PATH — seen keys (default ./.handle-collab-pull-idem.json)
  HANDLE_COLLAB_PULL_SKIP_FETCH — parse only (no API / no dedupe claim)
  HANDLE_COLLAB_PULL_ACCEPT — if 1, POST /tasks/{id}/accept after validate

Exit 0: handled, deduped, ignored non-pull, or skip-fetch parse ok.
Exit 1: pull recognized but validation/API failed.
Exit 2: misconfiguration.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ORCH = _HERE.parent / "org-orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from acn_client import accept_task, agents_me, get_task, normalize_base  # noqa: E402
from idempotency import IdempotencyStore  # noqa: E402

PULL_TYPE = "acn.task.collab_pull"


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
    if payload.get("type") == PULL_TYPE:
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


def parse_collab_pull(payload: Any) -> dict[str, Any] | None:
    needle_a = '{"type": "acn.task.collab_pull"'
    needle_b = '{"type":"acn.task.collab_pull"'
    for text in _candidate_strings(payload):
        try:
            obj = json.loads(text) if isinstance(text, str) else text
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and obj.get("type") == PULL_TYPE:
            return obj
        if not isinstance(text, str):
            continue
        start = text.find(needle_a)
        if start < 0:
            start = text.find(needle_b)
        if start < 0:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == PULL_TYPE:
            return obj
    return None


def resolve_idempotency_key(env: dict[str, Any]) -> str:
    key = str(env.get("idempotency_key") or "").strip()
    if key:
        return key
    task_id = str(env.get("task_id") or "").strip()
    invitee = str(env.get("invitee") or "").strip()
    if task_id and invitee:
        return f"{task_id}:collab_pull:1:{invitee}"
    return ""


def main() -> int:
    base_url = os.environ.get("ACN_BASE_URL", "").strip()
    api_key = os.environ.get("ACN_API_KEY", "").strip()
    skip_fetch = os.environ.get("HANDLE_COLLAB_PULL_SKIP_FETCH", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    do_accept = os.environ.get("HANDLE_COLLAB_PULL_ACCEPT", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    idem_path = os.environ.get(
        "HANDLE_COLLAB_PULL_IDEM_PATH",
        os.path.join(os.getcwd(), ".handle-collab-pull-idem.json"),
    )

    payload = _load_stdin()
    if payload is None:
        print("[handle_collab_pull] empty stdin — ignore", flush=True)
        return 0

    env = parse_collab_pull(payload)
    if env is None:
        print("[handle_collab_pull] not an acn.task.collab_pull — ignore", flush=True)
        return 0

    task_id = str(env.get("task_id") or "")
    invitee = str(env.get("invitee") or "")
    idem = resolve_idempotency_key(env)
    summary = str(env.get("summary") or "")
    print(
        f"[handle_collab_pull] task={task_id} invitee={invitee} "
        f"idem={idem} summary={summary[:80]!r}",
        flush=True,
    )

    if skip_fetch:
        print(
            "[handle_collab_pull] HANDLE_COLLAB_PULL_SKIP_FETCH set — not calling API",
            flush=True,
        )
        print(json.dumps(env, ensure_ascii=False, indent=2), flush=True)
        return 0

    if not base_url or not api_key:
        print("Need ACN_BASE_URL and ACN_API_KEY", file=sys.stderr)
        return 2
    if not task_id:
        print("[handle_collab_pull] missing task_id", file=sys.stderr)
        return 1
    if not idem:
        print(
            "[handle_collab_pull] missing idempotency_key "
            "(and cannot derive from task/invitee)",
            file=sys.stderr,
        )
        return 1

    base = normalize_base(base_url)
    try:
        me = agents_me(base, api_key)
        my_id = str(me.get("agent_id") or "")
    except Exception as e:
        print(f"[handle_collab_pull] agents/me failed: {e}", file=sys.stderr)
        return 1

    if invitee and my_id and invitee != my_id:
        print(
            f"[handle_collab_pull] envelope invitee {invitee} != me {my_id} — ignore",
            flush=True,
        )
        return 0

    try:
        task = get_task(base, task_id, api_key)
    except urllib.error.HTTPError as e:
        print(
            f"[handle_collab_pull] get task failed HTTP {e.code}: {e.reason}",
            file=sys.stderr,
        )
        return 1

    status = str(task.get("status") or "").lower()
    print(
        f"[handle_collab_pull] task status={status!r} title={task.get('title')!r}",
        flush=True,
    )
    if status in {"completed", "cancelled", "rejected", "closed", "expired"}:
        print(f"[handle_collab_pull] task terminal ({status}) — stop", flush=True)
        return 0

    store = IdempotencyStore(idem_path)
    try:
        claimed = store.try_claim(idem, work_id=task_id, assignee=my_id or invitee)
    except OSError as e:
        print(f"[handle_collab_pull] idempotency claim failed: {e}", file=sys.stderr)
        return 1
    if not claimed:
        print(f"[handle_collab_pull] deduped idem={idem} — already handled", flush=True)
        return 0

    accept_result = None
    if do_accept:
        try:
            accept_result = accept_task(base, api_key, task_id)
            print("[handle_collab_pull] accept OK", flush=True)
        except urllib.error.HTTPError as e:
            print(
                f"[handle_collab_pull] accept failed HTTP {e.code}: {e.reason}",
                file=sys.stderr,
            )
            try:
                store.release(idem)
            except OSError as re:
                print(f"[handle_collab_pull] release failed: {re}", file=sys.stderr)
            return 1
    else:
        print(
            "[handle_collab_pull] OK — fetch full materials / accept via "
            f"POST {base}/tasks/{task_id}/accept "
            "(set HANDLE_COLLAB_PULL_ACCEPT=1 to auto-accept).",
            flush=True,
        )

    print(
        json.dumps(
            {
                "envelope": env,
                "task": {
                    "task_id": task.get("task_id") or task.get("id") or task_id,
                    "status": task.get("status"),
                    "title": task.get("title"),
                },
                "accepted": bool(accept_result),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    try:
        store.confirm(idem)
    except OSError as e:
        print(f"[handle_collab_pull] idempotency confirm failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
