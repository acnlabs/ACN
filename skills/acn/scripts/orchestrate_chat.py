#!/usr/bin/env python3
"""Conversation-orchestrator host helper (P1).

Same door for Mode A handlers and Mode B ``--chat-complete-exec``:
``POST /api/v1/invoke``. Not Match, not ``acn message send``, not P13
``collab_request``. Chat ``usage`` is this hop's tokens only — never copy
callee usage from the invoke response.

Mode B (stdin = NormalizedEvent, stdout = complete JSON)::

  acn listen --runtime log --chat-writeback \\
    --chat-api-base "$AGENTPLANET_API_BASE" \\
    --chat-complete-exec 'python3 orchestrate_chat.py'

Env (complete-exec does **not** inject ``acn_*``):

  ACN_API_KEY     this agent's key (required to invoke)
  ACN_BASE_URL    origin or ``…/api/v1`` (default global)
  ACN_ORCH_TO     callee agent id (specified-id)
  ACN_ORCH_SLOT   optional; v0 ``text.reply`` enables slot failover
  ACN_ORCH_NAME   optional display name on the callee bubble
  ACN_ORCH_SKIP   if ``1``, do not invoke (self-reply; debug)

If invoke returns body text → one writeback, ``status=completed``.
If only ``accepted``/``sent``/inbox → ``content`` is 「已请 X」;
a later second ``agent-messages`` is the host's job when the callee
replies (this script does not poll). Failures still return a sentence.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

USER_TEXT_MAX = 8_000
REPLY_MAX = 8_000
DEFAULT_BASE = "https://api.acnlabs.dev"

InvokeFn = Callable[[str, str, dict[str, Any]], dict[str, Any]]


class InvokeError(Exception):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"invoke HTTP {status}: {body[:400]}")


def _as_record(v: Any) -> dict[str, Any] | None:
    return v if isinstance(v, dict) else None


def normalize_base(url: str) -> str:
    base = url.strip().rstrip("/")
    if not base:
        return f"{DEFAULT_BASE}/api/v1"
    if not base.endswith("/api/v1"):
        base = f"{base}/api/v1"
    return base


def user_text_from_event(event: dict[str, Any]) -> str:
    chat = _as_record(event.get("chat")) or {}
    text = chat.get("user_text")
    if isinstance(text, str) and text.strip():
        return text.strip()[:USER_TEXT_MAX]
    return ""


def chat_id_from_event(event: dict[str, Any]) -> str:
    chat = _as_record(event.get("chat")) or {}
    cid = chat.get("chat_id")
    return cid.strip() if isinstance(cid, str) and cid.strip() else ""


def extract_reply_text(payload: Any, *, _depth: int = 0) -> str | None:
    """Pull user-visible text from an invoke / A2A delivery blob."""
    if _depth > 6 or payload is None:
        return None
    if isinstance(payload, str):
        t = payload.strip()
        if t and t.lower() != "accepted":
            return t[:REPLY_MAX]
        return None
    rec = _as_record(payload)
    if rec is None:
        return None
    for key in ("content", "reply", "text"):
        got = extract_reply_text(rec.get(key), _depth=_depth + 1)
        if got:
            return got
    parts = rec.get("parts")
    if isinstance(parts, list):
        chunks: list[str] = []
        for item in parts:
            part = _as_record(item)
            if not part:
                continue
            kind = str(part.get("kind") or part.get("type") or "text").lower()
            if kind not in ("text",):
                continue
            t = part.get("text")
            if isinstance(t, str) and t.strip():
                chunks.append(t.strip())
        if chunks:
            return "\n".join(chunks)[:REPLY_MAX]
    for key in ("delivery", "response", "result", "message", "artifact"):
        if key not in rec:
            continue
        got = extract_reply_text(rec.get(key), _depth=_depth + 1)
        if got:
            return got
    return None


def _status_token(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower()


def callee_status(payload: Any, reply: str | None) -> str:
    if reply:
        return "completed"
    rec = _as_record(payload) or {}
    tokens = [
        _status_token(rec.get("status")),
        _status_token((_as_record(rec.get("delivery")) or {}).get("status")),
    ]
    for st in tokens:
        if st in ("failed", "error", "rejected"):
            return "failed"
    for st in tokens:
        if st in ("accepted", "sent", "inbox", "delivered"):
            return "accepted"
    return "failed" if not rec else "sent"


def own_usage(env: Mapping[str, str]) -> dict[str, Any] | None:
    """Only what this host measured. Never copy invoke ``usage``."""
    raw = (env.get("ACN_ORCH_USAGE_JSON") or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) and data else None


def _label(agent_id: str, name: str) -> str:
    if name:
        return name
    return agent_id[:12] if agent_id else "帮手"


def summarize(
    *,
    user_text: str,
    callee_id: str,
    callee_name: str,
    payload: dict[str, Any] | None,
    error: str | None,
) -> dict[str, Any]:
    reply = extract_reply_text(payload) if payload else None
    status = "failed" if error else callee_status(payload, reply)
    hop_id = ""
    to = callee_id
    if payload:
        hop = payload.get("hop_id")
        if isinstance(hop, str) and hop.strip():
            hop_id = hop.strip()
        got_to = payload.get("to")
        if isinstance(got_to, str) and got_to.strip():
            to = got_to.strip()
    label = _label(to, callee_name)
    if status == "completed" and reply:
        content = reply
    elif status == "failed":
        reason = error or "invoke 失败"
        content = f"没叫到帮手 {label}：{reason}"
    else:
        content = f"已请 {label}，等它回我。"
        if user_text:
            content += f" 你刚说：{user_text[:120]}"
        status = "accepted"

    callee: dict[str, str] = {"agent_id": to, "status": status}
    if hop_id:
        callee["hop_id"] = hop_id
    if callee_name:
        callee["name"] = callee_name[:200]
    return {
        "content": content,
        "orchestration": {"callees": [callee]},
    }


def post_invoke(
    base: str,
    api_key: str,
    body: dict[str, Any],
    *,
    timeout: float = 30,
) -> dict[str, Any]:
    url = f"{normalize_base(base)}/invoke"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            if not raw:
                return {}
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode(errors="replace") if exc.fp else ""
        raise InvokeError(exc.code, err_body) from exc
    except urllib.error.URLError as exc:
        raise InvokeError(0, str(exc.reason)) from exc


def invoke_and_summarize(
    user_text: str,
    env: Mapping[str, str],
    *,
    invoke_fn: InvokeFn | None = None,
) -> dict[str, Any]:
    """Mode A handler and Mode B complete share this."""
    callee = (env.get("ACN_ORCH_TO") or "").strip()
    name = (env.get("ACN_ORCH_NAME") or "").strip()
    if not callee:
        return {
            "content": (
                "没有帮手 id（设 ACN_ORCH_TO）。"
                + (f" 你刚说：{user_text[:120]}" if user_text else "")
            )
        }
    if (env.get("ACN_ORCH_SKIP") or "").strip() == "1":
        return {
            "content": f"（跳过 invoke）收到：{user_text[:200] or '（空）'}"
        }

    api_key = (env.get("ACN_API_KEY") or "").strip()
    if not api_key:
        out = summarize(
            user_text=user_text,
            callee_id=callee,
            callee_name=name,
            payload=None,
            error="缺少 ACN_API_KEY（complete-exec 不会注入）",
        )
        return out

    slot = (env.get("ACN_ORCH_SLOT") or "").strip()
    message: dict[str, Any] = {"text": user_text or "hello"}
    body: dict[str, Any] = {"to": callee, "message": message}
    if slot:
        body["slot"] = slot

    fn = invoke_fn or (
        lambda _base, _key, payload: post_invoke(
            env.get("ACN_BASE_URL") or DEFAULT_BASE, api_key, payload
        )
    )
    try:
        payload = fn(env.get("ACN_BASE_URL") or DEFAULT_BASE, api_key, body)
    except InvokeError as exc:
        return summarize(
            user_text=user_text,
            callee_id=callee,
            callee_name=name,
            payload=None,
            error=str(exc),
        )
    out = summarize(
        user_text=user_text,
        callee_id=callee,
        callee_name=name,
        payload=payload,
        error=None,
    )
    # Defense: never forward callee settlement as dialog usage.
    if "usage" in out:
        del out["usage"]
    usage = own_usage(env)
    if usage:
        out["usage"] = usage
    return out


def complete_chat(
    event: dict[str, Any],
    env: Mapping[str, str],
    *,
    invoke_fn: InvokeFn | None = None,
) -> dict[str, Any]:
    """Mode B complete-exec. Invoke hops are not this script's job."""
    if event.get("invoke") and not event.get("chat"):
        return {"content": "这是 invoke 跳，不是对话编排。"}
    text = user_text_from_event(event)
    return invoke_and_summarize(text, env, invoke_fn=invoke_fn)


def main() -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        event = {}
    if not isinstance(event, dict):
        event = {}
    out = complete_chat(event, os.environ)
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
