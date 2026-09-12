#!/usr/bin/env python3
"""Upload this-chat media, then print complete JSON for acn listen.

CLI 1.0.15+ forwards attachments that are mbx: only. CLI 1.0.16+ keeps
them on official hops. This script uploads. CLI does not.

Not vendor-specific. Same contract as chat_usage.py / official_hop.py.

  python3 scripts/chat_attach.py --chat-id <id> --content "..."
  python3 scripts/chat_attach.py --chat-id <id> --resp-file inner.json
  python3 official_hop.py --complete -- inner \\
    | python3 scripts/chat_attach.py --event-file event.json

Looks for png/jpeg/gif/webp/mp4/webm in:
  quoted / spaced / token paths in --content
  ACN_CHAT_ATTACH_FILES (os.pathsep, usually colon)
  ACN_CHAT_MEDIA_DIR files with mtime >= --since-epoch (newest first)

No chat_id / no files → print {"content"} (and usage if present) and exit 0.
Do not invent mbx:. Auth: ACN_AGENT_JWT, else mint with ACN_API_KEY /
~/.acn/config.json. Host base: ACN_CHAT_API_BASE / AGENTPLANET_API_BASE /
CHAT_API_BASE.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm"}
MAX_FILES = 4
CHAT_ID_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
EXT_ALT = "png|jpe?g|gif|webp|mp4|webm"
QUOTE_RE = re.compile(r'"([^"]+)"|\'([^\']+)\'')
PATH_RE = re.compile(
    rf"(?:file://)?(?:~|/|\./)[^\n\"']+?\.(?:{EXT_ALT})(?=[\s)\"']|$)",
    re.IGNORECASE,
)
TOKEN_RE = re.compile(rf"(?:file://)?[^\s\"')]+?\.(?:{EXT_ALT})\b", re.IGNORECASE)


def _cfg() -> dict[str, Any]:
    path = Path.home() / ".acn" / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_json_object(raw: str) -> dict[str, Any]:
    raw = raw.lstrip("\ufeff").strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def unwrap_complete(raw: str) -> tuple[str, dict[str, Any] | None, list[str]]:
    """If content is a complete JSON object, take .content / .usage / mbx:."""
    data = load_json_object(raw)
    inner = data.get("content") if data else None
    if not isinstance(inner, str):
        return raw, None, []
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
    return inner, usage, mailbox_refs(data.get("attachments"))


def mailbox_refs(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        ref = item.strip()
        if ref.startswith("mbx:") and ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def chat_api_base(env: dict[str, str] | None = None) -> str:
    environ = os.environ if env is None else env
    return (
        (environ.get("ACN_CHAT_API_BASE") or "").strip()
        or (environ.get("AGENTPLANET_API_BASE") or "").strip()
        or (environ.get("CHAT_API_BASE") or "").strip()
        or "https://api.agentplanet.org"
    )


def mint_jwt() -> str | None:
    tok = (os.environ.get("ACN_AGENT_JWT") or "").strip()
    if tok:
        return tok
    cfg = _cfg()
    api_key = (os.environ.get("ACN_API_KEY") or cfg.get("api_key") or "").strip()
    agent_id = (os.environ.get("ACN_AGENT_ID") or cfg.get("agent_id") or "").strip()
    base = (
        os.environ.get("ACN_BASE_URL") or cfg.get("base_url") or "https://api.acnlabs.dev"
    ).rstrip("/")
    aud = (os.environ.get("ACN_JWT_AUDIENCE") or "https://api.agentplanet.org").strip()
    if not api_key or not agent_id:
        return None
    req = urllib.request.Request(
        f"{base}/oauth/token",
        data=json.dumps(
            {
                "grant_type": "client_credentials",
                "client_id": agent_id,
                "client_secret": api_key,
                "audience": aud,
            }
        ).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as exc:
        print(f"chat-attach: jwt mint failed: {exc}", file=sys.stderr)
        return None
    token = payload.get("access_token") if isinstance(payload, dict) else None
    return token.strip() if isinstance(token, str) and token.strip() else None


def media_path(value: str) -> Path | None:
    raw = value.strip().strip("\"'")
    if raw.startswith("file://"):
        raw = raw[7:]
    path = Path(raw).expanduser()
    try:
        if path.is_file() and path.suffix.lower() in EXTS:
            return path.resolve()
    except OSError:
        return None
    return None


def content_path_strings(content: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        text = raw.strip()
        if text and text not in seen:
            seen.add(text)
            found.append(text)

    for match in QUOTE_RE.finditer(content):
        add(match.group(1) or match.group(2) or "")
    for match in PATH_RE.finditer(content):
        add(match.group(0))
    for match in TOKEN_RE.finditer(content):
        add(match.group(0))
    return found


def collect(
    content: str,
    since: int | None,
    env: dict[str, str] | None = None,
) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    environ = os.environ if env is None else env

    def add(path: Path | None) -> None:
        if path is None:
            return
        key = str(path)
        if key not in seen:
            seen.add(key)
            found.append(path)

    extra = (environ.get("ACN_CHAT_ATTACH_FILES") or "").strip()
    if extra:
        for part in extra.split(os.pathsep):
            add(media_path(part))
    for tok in content_path_strings(content):
        add(media_path(tok))
    root = (environ.get("ACN_CHAT_MEDIA_DIR") or "").strip()
    if root and since is not None:
        directory = Path(root).expanduser()
        if directory.is_dir():
            ranked: list[tuple[float, Path]] = []
            for child in directory.rglob("*"):
                if not child.is_file() or child.suffix.lower() not in EXTS:
                    continue
                try:
                    mtime = child.stat().st_mtime
                    if mtime >= since:
                        ranked.append((mtime, child.resolve()))
                except OSError:
                    pass
            ranked.sort(key=lambda item: item[0], reverse=True)
            for _, path in ranked:
                if len(found) >= MAX_FILES:
                    break
                add(path)
    return found[:MAX_FILES]


def valid_chat_id(chat_id: str) -> bool:
    return bool(chat_id) and all(ch in CHAT_ID_CHARS for ch in chat_id) and len(chat_id) <= 80


def parse_since(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"\d{9,12}", text):
        return int(text)
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def envelope_chat_id(event: dict[str, Any]) -> str:
    chat = event.get("chat")
    if isinstance(chat, dict):
        cid = chat.get("chat_id")
        if isinstance(cid, str) and valid_chat_id(cid.strip()):
            return cid.strip()
    raw = event.get("raw")
    if isinstance(raw, dict):
        try:
            cid = (
                raw.get("params", {})
                .get("message", {})
                .get("metadata", {})
                .get("agentplanet", {})
                .get("chat_id")
            )
        except AttributeError:
            cid = None
        if isinstance(cid, str) and valid_chat_id(cid.strip()):
            return cid.strip()
    return ""


def envelope_since(event: dict[str, Any]) -> int | None:
    got = parse_since(event.get("received_at"))
    if got is not None:
        return got
    chat = event.get("chat")
    if isinstance(chat, dict):
        return parse_since(chat.get("received_at"))
    return None


def classify_json(obj: dict[str, Any]) -> str | None:
    if envelope_chat_id(event=obj) or obj.get("event_type") == "a2a_message":
        return "event"
    if isinstance(obj.get("content"), str) or isinstance(obj.get("usage"), dict):
        return "complete"
    return None


def upload(chat_id: str, api_base: str, jwt: str, path: Path) -> str | None:
    boundary = "----acnchatmedia"
    data = path.read_bytes()
    filename = path.name.replace('"', "_").encode("ascii", "replace").decode("ascii")
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{api_base.rstrip('/')}/api/chats/{chat_id}/files",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(
            f"chat-attach: upload {path.name} http={exc.code} {exc.read()[:300]!r}",
            file=sys.stderr,
        )
        return None
    except Exception as exc:
        print(f"chat-attach: upload {path.name} failed: {exc}", file=sys.stderr)
        return None
    ref = payload.get("ref") if isinstance(payload, dict) else None
    return ref if isinstance(ref, str) and ref.startswith("mbx:") else None


def complete_payload(
    content: str,
    refs: list[str],
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"content": content}
    if usage:
        out["usage"] = usage
    if refs:
        out["attachments"] = refs
    return out


def read_json_file(path: str) -> dict[str, Any]:
    try:
        return load_json_object(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"chat-attach: read {path} failed: {exc}", file=sys.stderr)
        return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat-id", default="")
    parser.add_argument("--content", default="")
    parser.add_argument("--since-epoch", type=int, default=None)
    parser.add_argument("--resp-file", default="")
    parser.add_argument("--event-file", default="")
    args = parser.parse_args()

    event: dict[str, Any] = {}
    resp: dict[str, Any] = {}
    if args.event_file:
        event = read_json_file(args.event_file)
    if args.resp_file:
        resp = read_json_file(args.resp_file)
    elif not sys.stdin.isatty():
        incoming = load_json_object(sys.stdin.read())
        kind = classify_json(incoming)
        if kind == "event" and not event:
            event = incoming
        elif kind == "complete" and not resp:
            resp = incoming
        elif kind == "event":
            event = incoming
        elif incoming:
            resp = incoming

    content = args.content
    usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else None
    existing = mailbox_refs(resp.get("attachments"))
    if not content and isinstance(resp.get("content"), str):
        content = resp["content"]
    if content:
        unwrapped, wrapped_usage, wrapped_refs = unwrap_complete(content)
        if unwrapped != content:
            content = unwrapped
            if usage is None:
                usage = wrapped_usage
            existing = mailbox_refs(existing + wrapped_refs)

    chat_id = (args.chat_id or "").strip() or envelope_chat_id(event)
    since = args.since_epoch if args.since_epoch is not None else envelope_since(event)
    paths = collect(content, since) if valid_chat_id(chat_id) else []
    refs = list(existing) if valid_chat_id(chat_id) else []
    if valid_chat_id(chat_id) and paths:
        jwt = mint_jwt()
        if not jwt:
            print(
                "chat-attach: no ACN_AGENT_JWT / ACN_API_KEY; skip upload",
                file=sys.stderr,
            )
        else:
            api_base = chat_api_base()
            for path in paths:
                ref = upload(chat_id, api_base, jwt, path)
                if ref and ref not in refs:
                    refs.append(ref)
                    print(f"chat-attach: uploaded {path.name} -> {ref}", file=sys.stderr)
    print(json.dumps(complete_payload(content, refs, usage), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
