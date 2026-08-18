#!/usr/bin/env python3
"""Interfaze official hop — any Mode B complete (not agent-specific).

Same idea as requested_model / chat_usage.py: Host decides the path;
this helper only honors it.

  python3 official_hop.py [--mint] < event.json     # JSON wake fields
  eval "$(python3 official_hop.py --door)"          # stdin = event; official
                                                    # exports OPENAI_BASE_URL
  python3 official_hop.py --proxy                   # local OpenAI door

Official only when Host said so and hop + allowlisted URL + JWT are present.
BYO / missing JWT = no-op. Do not invent a provider.
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


def _clean(s: object, n: int = 240) -> str:
    if not isinstance(s, str):
        return ""
    t = "".join(ch if ch >= " " and ch != "\x7f" else " " for ch in s).strip()
    return t[:n]


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
        "agent_id": agent_id,
        "jwt": jwt if official else "",
    }


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
                with urlopen(req, timeout=120) as resp:  # noqa: S310 — Host allowlisted
                    out = resp.read()
                    self._send(
                        resp.status,
                        out,
                        resp.headers.get("Content-Type") or "application/json",
                    )
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


def run_door() -> int:
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


def main() -> int:
    if "--proxy" in sys.argv:
        return serve_proxy()
    if "--door" in sys.argv:
        return run_door()
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
