#!/usr/bin/env python3
"""Member-side: parse acn.org.work_handoff, verify sender (§4.3), dedupe, validate work.

Env:
  ACN_BASE_URL, ACN_API_KEY — receiving member
  HANDLE_HANDOFF_IDEM_PATH — default ./.handle-handoff-idem.json
  HANDLE_HANDOFF_SKIP_FETCH — parse + sender check only
  HANDLE_HANDOFF_SKIP_KB — skip Org knowledge sidecar
  HANDOFF_TRUSTED_SENDER — when stdin is bare handoff JSON (no transport sender),
    set this to the expected from_agent (smoke / tests). Production Mode B/inbox
    payloads should carry from_agent/sender on the outer envelope.
  ORG_KB_ROOT — filesystem knowledge root

Exit 0: handled, deduped, ignored non-handoff, or skip-fetch ok.
Exit 1: handoff recognized but validation/API failed.
Exit 2: misconfiguration.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
from pathlib import Path
from typing import Any

from acn_client_min import WorkNotFoundError, agents_me, get_work, normalize_base
from handle_wake import assignee_matches_me, load_knowledge_bundle
from idempotency import IdempotencyStore

HANDOFF_TYPE = "acn.org.work_handoff"
_KB_DIR = Path(__file__).resolve().parent.parent / "org-knowledge"


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
    if payload.get("type") == HANDOFF_TYPE:
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


def parse_handoff(payload: Any) -> dict[str, Any] | None:
    for text in _candidate_strings(payload):
        try:
            obj = json.loads(text) if isinstance(text, str) else text
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and obj.get("type") == HANDOFF_TYPE:
            return obj
        if not isinstance(text, str):
            continue
        for needle in (
            '{"type": "acn.org.work_handoff"',
            '{"type":"acn.org.work_handoff"',
        ):
            start = text.find(needle)
            if start < 0:
                continue
            try:
                obj, _ = json.JSONDecoder().raw_decode(text[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("type") == HANDOFF_TYPE:
                return obj
    return None


def extract_transport_sender(payload: Any) -> str:
    """Best-effort ACN transport sender (never trust bare envelope from_agent).

    A bare ``acn.org.work_handoff`` object carries claim fields only — its
    ``from_agent`` is *not* transport proof (§4.3). Prefer outer inbox/Mode B
    wrappers, else ``HANDOFF_TRUSTED_SENDER``.
    """
    if not isinstance(payload, dict):
        return ""
    # Bare handoff JSON: do not treat envelope from_agent as transport.
    if payload.get("type") == HANDOFF_TYPE:
        return ""
    for key in ("from_agent", "sender", "sender_id", "from"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    msg = payload.get("message")
    if isinstance(msg, dict):
        for key in ("from_agent", "sender", "from"):
            val = msg.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    raw = payload.get("raw")
    if isinstance(raw, dict):
        for key in ("from_agent", "sender", "from"):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        params = raw.get("params")
        if isinstance(params, dict):
            for key in ("from_agent", "sender", "from"):
                val = params.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return ""


def resolve_trusted_sender(payload: Any) -> str:
    transport = extract_transport_sender(payload)
    if transport:
        return transport
    return (os.environ.get("HANDOFF_TRUSTED_SENDER") or "").strip()


def resolve_idempotency_key(handoff: dict[str, Any]) -> str:
    key = str(handoff.get("idempotency_key") or "").strip()
    if key:
        return key
    org_id = str(handoff.get("org_id") or "").strip()
    work_id = str(handoff.get("work_id") or "").strip()
    frm = str(handoff.get("from_agent") or "").strip()
    to = str(handoff.get("to_agent") or "").strip()
    if org_id and work_id and frm and to:
        return f"{org_id}:{work_id}:handoff:1:{frm}:{to}"
    return ""


def verify_sender_anti_spoof(
    *,
    envelope_from: str,
    trusted_sender: str,
    my_id: str,
    envelope_to: str,
) -> tuple[bool, str]:
    """Contract §4.3 — fail closed when sender unknown or mismatched."""
    env_from = str(envelope_from or "").strip()
    trusted = str(trusted_sender or "").strip()
    to = str(envelope_to or "").strip()
    if not env_from:
        return False, "envelope missing from_agent"
    if not trusted:
        return (
            False,
            "no transport sender (set HANDOFF_TRUSTED_SENDER for bare JSON smoke)",
        )
    if env_from != trusted:
        return False, f"from_agent {env_from} != transport sender {trusted}"
    if to and my_id and to != my_id:
        return False, f"to_agent {to} != me {my_id}"
    return True, ""


def main() -> int:
    base_url = os.environ.get("ACN_BASE_URL", "").strip()
    api_key = os.environ.get("ACN_API_KEY", "").strip()
    skip_fetch = os.environ.get("HANDLE_HANDOFF_SKIP_FETCH", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    skip_kb = os.environ.get("HANDLE_HANDOFF_SKIP_KB", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    idem_path = os.environ.get(
        "HANDLE_HANDOFF_IDEM_PATH",
        os.path.join(os.getcwd(), ".handle-handoff-idem.json"),
    )

    payload = _load_stdin()
    if payload is None:
        print("[handle_handoff] empty stdin — ignore", flush=True)
        return 0

    handoff = parse_handoff(payload)
    if handoff is None:
        print("[handle_handoff] not an acn.org.work_handoff — ignore", flush=True)
        return 0

    trusted = resolve_trusted_sender(payload)
    org_id = str(handoff.get("org_id") or "")
    work_id = str(handoff.get("work_id") or "")
    idem = resolve_idempotency_key(handoff)
    from_agent = str(handoff.get("from_agent") or "")
    to_agent = str(handoff.get("to_agent") or "")
    print(
        f"[handle_handoff] org={org_id} work={work_id} "
        f"from={from_agent} to={to_agent} idem={idem} trusted_sender={trusted}",
        flush=True,
    )

    if skip_fetch:
        # Still enforce §4.3 when possible (me unknown without API)
        ok, reason = verify_sender_anti_spoof(
            envelope_from=from_agent,
            trusted_sender=trusted,
            my_id=to_agent,
            envelope_to=to_agent,
        )
        if not ok:
            print(f"[handle_handoff] {reason}", file=sys.stderr)
            return 1
        print("[handle_handoff] HANDLE_HANDOFF_SKIP_FETCH — not calling API", flush=True)
        print(json.dumps(handoff, ensure_ascii=False, indent=2), flush=True)
        if not skip_kb:
            bundle = load_knowledge_bundle(handoff)
            if bundle:
                print("[handle_handoff] knowledge bundle:", flush=True)
                print(bundle, end="" if bundle.endswith("\n") else "\n", flush=True)
        return 0

    if not base_url or not api_key:
        print("Need ACN_BASE_URL and ACN_API_KEY", file=sys.stderr)
        return 2
    if not org_id or not work_id:
        print("[handle_handoff] missing org_id/work_id", file=sys.stderr)
        return 1
    if not idem:
        print("[handle_handoff] missing idempotency_key", file=sys.stderr)
        return 1

    base = normalize_base(base_url)
    try:
        me = agents_me(base, api_key)
        my_id = str(me.get("agent_id") or "")
    except Exception as e:
        print(f"[handle_handoff] agents/me failed: {e}", file=sys.stderr)
        return 1

    ok, reason = verify_sender_anti_spoof(
        envelope_from=from_agent,
        trusted_sender=trusted,
        my_id=my_id,
        envelope_to=to_agent,
    )
    if not ok:
        print(f"[handle_handoff] {reason} — reject", file=sys.stderr)
        return 1

    try:
        work = get_work(base, org_id, work_id, api_key)
    except WorkNotFoundError:
        print(f"[handle_handoff] work not found: {work_id}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        print(
            f"[handle_handoff] list work failed HTTP {e.code}: {e.reason}",
            file=sys.stderr,
        )
        return 1

    status = work.get("status")
    work_assignee = work.get("assignee_agent_id") or work.get("assignee")
    print(
        f"[handle_handoff] work status={status!r} assignee={work_assignee!r}",
        flush=True,
    )
    if status not in ("todo", "in_progress"):
        print(f"[handle_handoff] work not open ({status}) — stop", flush=True)
        return 0

    ok, reason = assignee_matches_me(
        envelope_assignee=to_agent,
        work_assignee=str(work_assignee) if work_assignee is not None else None,
        my_id=my_id,
    )
    if not ok:
        # Not ignore: handoff arrived before governance reassign completed
        print(f"[handle_handoff] {reason} — reject (reassign first)", file=sys.stderr)
        return 1

    # Optional: confirm from/to still members (best-effort; ignore list failures)
    try:
        from acn_client_min import active_member_ids, fetch_members

        members = fetch_members(base, org_id, api_key)
        active = active_member_ids(members)
        if active:
            if from_agent not in active or my_id not in active:
                print(
                    f"[handle_handoff] from/me not both active members "
                    f"(from={from_agent in active} me={my_id in active}) — reject",
                    file=sys.stderr,
                )
                return 1
    except Exception as e:
        print(f"[handle_handoff] membership check skipped: {e}", flush=True)

    store = IdempotencyStore(idem_path)
    try:
        claimed = store.try_claim(idem, work_id=work_id, assignee=my_id)
    except OSError as e:
        print(f"[handle_handoff] idempotency claim failed: {e}", file=sys.stderr)
        return 1
    if not claimed:
        print(f"[handle_handoff] deduped idem={idem}", flush=True)
        return 0

    print(
        "[handle_handoff] OK — run your L1; ask governance to PATCH done|cancelled.",
        flush=True,
    )
    if str(_KB_DIR) not in sys.path:
        sys.path.insert(0, str(_KB_DIR))
    kb_bundle = None
    if not skip_kb:
        kb_bundle = load_knowledge_bundle(handoff)
        if kb_bundle:
            print("[handle_handoff] knowledge bundle:", flush=True)
            print(
                kb_bundle,
                end="" if kb_bundle.endswith("\n") else "\n",
                flush=True,
            )
    print(
        json.dumps(
            {
                "handoff": handoff,
                "work": work,
                "trusted_sender": trusted,
                "knowledge_loaded": bool(kb_bundle),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    try:
        store.confirm(idem)
    except OSError as e:
        print(f"[handle_handoff] idempotency confirm failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
