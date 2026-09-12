#!/usr/bin/env python3
"""Upload this-chat media, then print complete JSON for acn listen.

CLI 1.0.15+ forwards attachments that are mbx: only. CLI 1.0.16+ keeps
them on official hops. This script uploads. CLI does not.

Not vendor-specific. Same contract as chat_usage.py / official_hop.py.

  python3 scripts/chat_attach.py --chat-id <id> --content "..."
  python3 scripts/chat_attach.py --chat-id <id> --content "..." --since-epoch UNIX

Looks for png/jpeg/gif/webp/mp4/webm in:
  --content path tokens
  ACN_CHAT_ATTACH_FILES (colon-separated)
  ACN_CHAT_MEDIA_DIR files with mtime >= --since-epoch

No chat_id / no files → print {"content"} and exit 0. Do not invent mbx:.
Auth: ACN_AGENT_JWT, else mint with ACN_API_KEY / ~/.acn/config.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm"}
MAX_FILES = 4
CHAT_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")


def _cfg() -> dict[str, Any]:
    path = Path.home() / ".acn" / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


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


def collect(content: str, since: int | None, env: dict[str, str] | None = None) -> list[Path]:
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
        for part in extra.split(":"):
            add(media_path(part))
    if content:
        for tok in content.replace(")", " ").replace("(", " ").split():
            add(media_path(tok))
    root = (environ.get("ACN_CHAT_MEDIA_DIR") or "").strip()
    if root and since:
        directory = Path(root).expanduser()
        if directory.is_dir():
            for child in directory.rglob("*"):
                if not child.is_file() or child.suffix.lower() not in EXTS:
                    continue
                try:
                    if child.stat().st_mtime >= since:
                        add(child.resolve())
                except OSError:
                    pass
    return found[:MAX_FILES]


def valid_chat_id(chat_id: str) -> bool:
    return bool(chat_id) and all(ch in CHAT_ID_CHARS for ch in chat_id) and len(chat_id) <= 80


def upload(chat_id: str, api_base: str, jwt: str, path: Path) -> str | None:
    boundary = "----acnchatmedia"
    data = path.read_bytes()
    filename = path.name.replace('"', "_")
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


def complete_payload(content: str, refs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"content": content}
    if refs:
        out["attachments"] = refs
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat-id", default="")
    parser.add_argument("--content", default="")
    parser.add_argument("--since-epoch", type=int)
    args = parser.parse_args()
    out = complete_payload(args.content, [])
    if not valid_chat_id(args.chat_id):
        print(json.dumps(out, ensure_ascii=False))
        return 0
    paths = collect(args.content, args.since_epoch)
    if not paths:
        print(json.dumps(out, ensure_ascii=False))
        return 0
    api_base = (
        os.environ.get("AGENTPLANET_API_BASE")
        or os.environ.get("CHAT_API_BASE")
        or "https://api.agentplanet.org"
    )
    jwt = mint_jwt()
    if not jwt:
        print("chat-attach: no ACN_AGENT_JWT / ACN_API_KEY; skip upload", file=sys.stderr)
        print(json.dumps(out, ensure_ascii=False))
        return 0
    refs: list[str] = []
    for path in paths:
        ref = upload(args.chat_id, api_base, jwt, path)
        if ref:
            refs.append(ref)
            print(f"chat-attach: uploaded {path.name} -> {ref}", file=sys.stderr)
    print(json.dumps(complete_payload(args.content, refs), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
