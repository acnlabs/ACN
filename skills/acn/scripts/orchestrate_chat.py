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
  ACN_ORCH_PROPOSE_GROUP  if ``1``, attach propose_group (human must confirm)
  ACN_ORCH_PROPOSE_IDS    extra agent ids (comma); defaults to the callee
  ACN_ORCH_PROPOSE_TITLE / ACN_ORCH_PROPOSE_SUMMARY / ACN_ORCH_PROPOSE_CHAT
  ACN_ORCH_PROPOSE_TASK   if ``1``, attach propose_task (human must confirm)
  ACN_ORCH_TASK_REWARD    required when the switch is on; numeric string, 0 allowed
  ACN_ORCH_TASK_TITLE     optional; defaults to a short slice of the user text
  ACN_ORCH_TASK_DEADLINE_HOURS  optional; default 72 (1..2160)
  ACN_ORCH_TASK_DESCRIPTION     optional

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
    chat_id = (env.get("ACN_ORCH_CHAT_ID") or "").strip()
    if chat_id:
        message["metadata"] = {"agentplanet": {"chat_id": chat_id}}
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
    return attach_propose_task(attach_propose_group(out, env), env, user_text)


def attach_propose_group(
    out: dict[str, Any], env: Mapping[str, str]
) -> dict[str, Any]:
    """Optional P2 card. Host still will not create the group for the agent."""
    flag = (env.get("ACN_ORCH_PROPOSE_GROUP") or "").strip().lower()
    if flag not in ("1", "true", "yes"):
        return out
    orch = out.setdefault("orchestration", {})
    if not isinstance(orch, dict):
        return out
    ids: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        bare = raw.strip()
        if bare.lower().startswith("acn:"):
            bare = bare[4:].strip()
        key = bare.lower()
        if not bare or key.startswith(("local:", "sys:")) or key in seen:
            return
        seen.add(key)
        ids.append(bare)

    extra = (env.get("ACN_ORCH_PROPOSE_IDS") or "").strip()
    if extra:
        for part in extra.split(","):
            _add(part)
    for row in orch.get("callees") or []:
        if isinstance(row, dict) and isinstance(row.get("agent_id"), str):
            _add(row["agent_id"])
    existing = (env.get("ACN_ORCH_PROPOSE_CHAT") or "").strip()[:64]
    if not ids and not existing:
        return out
    propose: dict[str, Any] = {}
    if ids:
        propose["agent_ids"] = ids[:8]
    title = (env.get("ACN_ORCH_PROPOSE_TITLE") or "").strip()[:200]
    summary = (env.get("ACN_ORCH_PROPOSE_SUMMARY") or "").strip()[:4000]
    if title:
        propose["title"] = title
    if summary:
        propose["summary"] = summary
    if existing:
        propose["existing_chat_id"] = existing
    if propose:
        orch["propose_group"] = propose
    return out


_TASK_REWARD_MAX = 1_000_000
_TASK_TITLE_DEFAULT = 80


def _task_reward(raw: str) -> str | None:
    """Match gateway: numeric text, 0..1_000_000, stored trimmed (max 32)."""
    text = raw.strip()
    if not text:
        return None
    try:
        amount = float(text)
    except ValueError:
        return None
    if amount != amount or amount < 0 or amount > _TASK_REWARD_MAX:
        return None
    return text[:32]


def _task_deadline_hours(raw: str | None) -> int | None:
    """Blank → 72. Explicit value must be an integer in 1..2160."""
    if raw is None or not str(raw).strip():
        return 72
    text = str(raw).strip()
    if not text.isdigit():
        return None
    hours = int(text)
    if 1 <= hours <= 2160:
        return hours
    return None


def attach_propose_task(
    out: dict[str, Any], env: Mapping[str, str], user_text: str
) -> dict[str, Any]:
    """Optional P4 card. This script does not create a task or call match."""
    flag = (env.get("ACN_ORCH_PROPOSE_TASK") or "").strip().lower()
    if flag not in ("1", "true", "yes"):
        return out
    reward = _task_reward(env.get("ACN_ORCH_TASK_REWARD") or "")
    if reward is None:
        return out
    title = (env.get("ACN_ORCH_TASK_TITLE") or "").strip()
    if not title:
        title = " ".join((user_text or "").split())[:_TASK_TITLE_DEFAULT]
    title = title[:200]
    if not title:
        return out
    deadline = _task_deadline_hours(env.get("ACN_ORCH_TASK_DEADLINE_HOURS"))
    if deadline is None:
        return out
    orch = out.setdefault("orchestration", {})
    if not isinstance(orch, dict):
        return out
    propose: dict[str, Any] = {
        "title": title,
        "reward": reward,
        "deadline_hours": deadline,
    }
    description = (env.get("ACN_ORCH_TASK_DESCRIPTION") or "").strip()[:2000]
    if description:
        propose["description"] = description
    orch["propose_task"] = propose
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
    env_out: dict[str, str] = dict(env)
    cid = chat_id_from_event(event)
    if cid:
        env_out["ACN_ORCH_CHAT_ID"] = cid
    return invoke_and_summarize(text, env_out, invoke_fn=invoke_fn)


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
