#!/usr/bin/env python3
"""Interfaze official hop — any Mode B complete (not agent-specific).

Same idea as requested_model / chat_usage.py: Host decides the path;
this helper only honors it.

  python3 official_hop.py [--mint] < event.json     # JSON wake fields
  python3 official_hop.py --complete [-- <byo>]     # official → Host
                                                    # {"content"}; BYO → exec
  eval "$(python3 official_hop.py --door)"          # stdin = event; official
                                                    # exports OPENAI_BASE_URL
  python3 official_hop.py --proxy                   # local OpenAI door

CLI 1.0.9+ completes official hops itself. --complete is for older CLI
or an explicit --chat-complete-exec wrapper. Official only when Host said
so and hop + allowlisted URL + JWT are present. BYO / missing JWT = no-op
unless a command follows `--`. Do not invent a provider.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

HOSTS = {"api.agentplanet.org", "api.agenticplanet.space"}


USER_TEXT_MAX = 32_000


def _clean(s: object, n: int = 240) -> str:
    if not isinstance(s, str):
        return ""
    t = "".join(ch if ch >= " " and ch != "\x7f" else " " for ch in s).strip()
    return t[:n]


def _plain_text(s: object, n: int) -> str:
    """User text: keep newlines; do not use _clean's 240-char hop-field cap."""
    if not isinstance(s, str):
        return ""
    t = "".join(
        ch if ch in "\n\t" or (ch >= " " and ch != "\x7f") else " " for ch in s
    )
    return t.strip()[:n]


def _dig(obj: object, *paths: str) -> str:
    for path in paths:
        cur: object = obj
        ok = True
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                ok = False
                break
            cur = cur[part]
        if ok:
            got = _clean(cur)
            if got:
                return got
    return ""


def allow_host_url(raw: str) -> str:
    raw = _clean(raw, 300)
    if not raw:
        return ""
    try:
        u = urlparse(raw)
    except ValueError:
        return ""
    path = (u.path or "").rstrip("/")
    if path != "/api/inference/v1":
        return ""
    if u.query or u.fragment:
        return ""
    host = (u.hostname or "").lower()
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if u.scheme == "https" and host in HOSTS:
        return f"{u.scheme}://{u.netloc}/api/inference/v1"
    if u.scheme == "http" and loopback:
        return f"{u.scheme}://{u.netloc}/api/inference/v1"
    return ""


def try_mint_jwt(agent_id: str, env: dict[str, str]) -> str:
    """Mint a short-lived agent JWT from ~/.acn/config.json when CLI omitted it."""
    from urllib.error import URLError
    from urllib.request import Request, urlopen

    cfg_path = Path.home() / ".acn" / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(cfg, dict):
        return ""
    key = _clean(cfg.get("api_key") or env.get("ACN_API_KEY") or "", 200)
    aid = agent_id or _clean(cfg.get("agent_id") or "", 80)
    base = _clean(
        cfg.get("base_url") or env.get("ACN_BASE_URL") or "https://api.acnlabs.dev",
        200,
    )
    if not key or not aid:
        return ""
    body = json.dumps(
        {
            "grant_type": "client_credentials",
            "client_id": aid,
            "client_secret": key,
            "audience": "https://api.agentplanet.org",
        }
    ).encode("utf-8")
    req = Request(
        base.rstrip("/") + "/oauth/token",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=15) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, ValueError, OSError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return _clean(payload.get("access_token"), 4000)


def resolve_wake(
    ev: object,
    env: dict[str, str] | None = None,
    *,
    mint: bool = False,
) -> dict[str, str]:
    env = env or os.environ
    evd = ev if isinstance(ev, dict) else {}
    path = _dig(
        evd,
        "chat.inference_path",
        "raw.params.message.metadata.agentplanet.inference_path",
    ) or _clean(env.get("ACN_INFERENCE_PATH", ""))
    if path not in {"official", "byo"}:
        path = "byo"
    hop = _dig(
        evd,
        "chat.hop_id",
        "raw.params.message.metadata.agentplanet.hop_id",
    ) or _clean(env.get("ACN_CHAT_HOP_ID", ""))
    url = allow_host_url(
        _dig(
            evd,
            "chat.host_inference_url",
            "raw.params.message.metadata.agentplanet.host_inference_url",
        )
        or env.get("ACN_HOST_INFERENCE_URL", "")
    )
    requested = _dig(
        evd,
        "chat.requested_model",
        "raw.params.message.metadata.agentplanet.requested_model",
    )
    user_text = _user_text(evd)
    max_out = _max_output_tokens(evd)
    agent_id = _clean(env.get("ACN_AGENT_ID") or "", 80)
    jwt = _clean(env.get("ACN_AGENT_JWT", ""), 4000)
    if mint and path == "official" and hop and url and not jwt:
        jwt = try_mint_jwt(agent_id, env)
    official = path == "official" and bool(hop and url and jwt)
    return {
        "inference_path": "official" if official else "byo",
        "hop_id": hop,
        "host_inference_url": url if official else "",
        "requested_model": requested,
        "user_text": user_text,
        "max_output_tokens": max_out,
        "agent_id": agent_id,
        "jwt": jwt if official else "",
    }


def _user_text(evd: dict[str, object]) -> str:
    chat = evd.get("chat")
    if isinstance(chat, dict):
        t = _plain_text(chat.get("user_text"), USER_TEXT_MAX)
        if t:
            return t
    for path in (
        ("raw", "params", "message"),
        ("params", "message"),
        ("message",),
    ):
        cur: object = evd
        ok = True
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                ok = False
                break
            cur = cur[key]
        if not ok or not isinstance(cur, dict):
            continue
        parts = cur.get("parts")
        if not isinstance(parts, list):
            continue
        chunks: list[str] = []
        for part in parts:
            if not isinstance(part, dict) or part.get("kind") != "text":
                continue
            got = _plain_text(part.get("text"), USER_TEXT_MAX)
            if got:
                chunks.append(got)
        if chunks:
            return "\n".join(chunks)
    return ""


def _max_output_tokens(evd: dict[str, object]) -> str:
    for path in (
        ("chat", "max_output_tokens"),
        ("raw", "params", "message", "metadata", "agentplanet", "max_output_tokens"),
    ):
        cur: object = evd
        ok = True
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                ok = False
                break
            cur = cur[key]
        if not ok:
            continue
        if isinstance(cur, bool):
            continue
        if isinstance(cur, int) and cur > 0:
            return str(cur)
        if isinstance(cur, str) and cur.strip():
            try:
                n = int(cur.strip())
            except ValueError:
                continue
            if n > 0:
                return str(n)
    return ""


def extract_completion_content(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        msg = choices[0].get("message")
        if isinstance(msg, dict):
            got = _clean(msg.get("content"), 20000)
            if got and got.lower() != "accepted":
                return got
        got = _clean(choices[0].get("text"), 20000)
        if got and got.lower() != "accepted":
            return got
    for key in ("content", "reply", "text"):
        got = _clean(payload.get(key), 20000)
        if got and got.lower() != "accepted":
            return got
    return ""


def serve_proxy() -> int:
    """Local OpenAI-compatible door that adds Host hop headers."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    target = allow_host_url(os.environ.get("ACN_HOST_INFERENCE_URL", ""))
    jwt = _clean(os.environ.get("ACN_AGENT_JWT", ""), 4000)
    hop = _clean(os.environ.get("ACN_CHAT_HOP_ID", ""))
    agent_id = _clean(os.environ.get("ACN_AGENT_ID") or "", 80)
    if not (target and jwt and hop):
        print("official_hop: proxy missing url/jwt/hop", file=sys.stderr)
        return 2
    upstream = f"{target}/chat/completions"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _send(self, code: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            path = (self.path or "").split("?", 1)[0]
            if path not in {"/v1/chat/completions", "/chat/completions"}:
                self._send(404, b'{"error":"not_found"}')
                return
            n = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(n) if n > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send(400, b'{"error":"invalid_json"}')
                return
            if not isinstance(payload, dict):
                self._send(400, b'{"error":"invalid_json"}')
                return
            payload.pop("agent_id", None)
            payload["hop_id"] = hop
            headers = {
                "Authorization": f"Bearer {jwt}",
                "Content-Type": "application/json",
                "X-Hop-Id": hop,
            }
            if agent_id:
                headers["X-Agent-Id"] = agent_id
            req = Request(
                upstream,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                stream_req = payload.get("stream") is True
                timeout = 180 if stream_req else 120
                with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — Host allowlisted
                    ct = resp.headers.get("Content-Type") or "application/json"
                    if stream_req or "text/event-stream" in ct.lower():
                        self.send_response(resp.status)
                        self.send_header("Content-Type", ct)
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Connection", "keep-alive")
                        self.send_header("X-Accel-Buffering", "no")
                        self.end_headers()
                        while True:
                            chunk = resp.read(4096)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        return
                    out = resp.read()
                    self._send(resp.status, out, ct)
            except HTTPError as e:
                self._send(e.code, e.read() or b'{"error":"upstream"}')
            except URLError as e:
                msg = json.dumps({"error": f"upstream_unreachable:{e.reason}"}).encode()
                self._send(502, msg)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    sys.stdout.write(str(port) + "\n")
    sys.stdout.flush()
    httpd.serve_forever()
    return 0


def door_already_open(env: dict[str, str] | None = None) -> bool:
    """CLI 1.0.7+ already injected OPENAI_* — do not read stdin again."""
    env = env or os.environ
    base = _clean(env.get("OPENAI_BASE_URL", ""), 200)
    key = _clean(env.get("OPENAI_API_KEY", ""), 4000)
    return base.startswith("http://127.0.0.1:") and bool(key)


def run_door() -> int:
    if door_already_open():
        print("# acn official_hop: door already open")
        return 0
    raw = sys.stdin.read()
    try:
        ev = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(f"official_hop: invalid_json:{e}", file=sys.stderr)
        return 2
    wake = resolve_wake(ev, mint=True)
    if wake["inference_path"] != "official":
        print("# acn official_hop: byo")
        return 0
    env = os.environ.copy()
    env["ACN_HOST_INFERENCE_URL"] = wake["host_inference_url"]
    env["ACN_AGENT_JWT"] = wake["jwt"]
    env["ACN_CHAT_HOP_ID"] = wake["hop_id"]
    if wake["agent_id"]:
        env["ACN_AGENT_ID"] = wake["agent_id"]
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, str(Path(__file__).resolve()), "--proxy"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    assert child.stdout is not None
    line = child.stdout.readline().decode("utf-8", errors="replace").strip()
    if not line.isdigit():
        child.kill()
        print("# acn official_hop: proxy_failed; staying byo", file=sys.stderr)
        return 0
    print(f"export OPENAI_BASE_URL=http://127.0.0.1:{line}/v1")
    print(f"export OPENAI_API_KEY={shlex.quote(wake['jwt'])}")
    print("export ACN_INFERENCE_PATH=official")
    print(f"export ACN_CHAT_HOP_ID={shlex.quote(wake['hop_id'])}")
    print(f"export ACN_HOST_INFERENCE_URL={shlex.quote(wake['host_inference_url'])}")
    print(f"export ACN_OFFICIAL_PROXY_PID={child.pid}")
    return 0


def _byo_cmd(argv: list[str]) -> list[str]:
    if "--" in argv:
        return [a for a in argv[argv.index("--") + 1 :] if a]
    return []


def complete_official(wake: dict[str, str], text: str) -> int:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    model = wake["requested_model"]
    if not model or not text:
        print("official_hop: missing_model_or_text", file=sys.stderr)
        return 2
    base = wake["host_inference_url"]
    jwt = wake["jwt"]
    hop = wake["hop_id"]
    payload: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "hop_id": hop,
    }
    try:
        max_tokens = int(wake.get("max_output_tokens") or "")
    except ValueError:
        max_tokens = 0
    if max_tokens > 0:
        payload["max_tokens"] = max_tokens
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Content-Type": "application/json",
        "X-Hop-Id": hop,
    }
    if wake["agent_id"]:
        headers["X-Agent-Id"] = wake["agent_id"]
    req = Request(
        f"{base}/chat/completions",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(req, timeout=120) as resp:  # noqa: S310 — Host allowlisted
            raw = resp.read()
    except HTTPError as e:
        print(f"official_hop: http_{e.code}", file=sys.stderr)
        return 2
    except URLError as e:
        print(f"official_hop: upstream:{e.reason}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        print("official_hop: invalid_json", file=sys.stderr)
        return 2
    content = extract_completion_content(payload)
    if not content:
        print("official_hop: missing_content", file=sys.stderr)
        return 2
    print(json.dumps({"content": content}, ensure_ascii=False))
    return 0


def run_complete() -> int:
    raw = sys.stdin.read()
    try:
        ev = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(f"official_hop: invalid_json:{e}", file=sys.stderr)
        return 2
    wake = resolve_wake(ev, mint=True)
    text = wake.get("user_text") or _user_text(ev if isinstance(ev, dict) else {})
    if wake["inference_path"] == "official":
        return complete_official(wake, text)
    byo = _byo_cmd(sys.argv)
    if not byo:
        print("official_hop: byo_use_complete_exec", file=sys.stderr)
        return 3
    child = subprocess.run(  # noqa: S603
        byo,
        input=raw.encode("utf-8"),
        check=False,
    )
    return child.returncode


def main() -> int:
    if "--proxy" in sys.argv:
        return serve_proxy()
    if "--door" in sys.argv:
        return run_door()
    if "--complete" in sys.argv:
        return run_complete()
    raw = sys.stdin.read()
    try:
        ev = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(f"official_hop: invalid_json:{e}", file=sys.stderr)
        return 2
    print(json.dumps(resolve_wake(ev, mint="--mint" in sys.argv), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
