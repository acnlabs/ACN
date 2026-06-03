#!/usr/bin/env python3
"""
ADR-0012 Mode B (relay delivery) end-to-end smoke test — real components, no mocks.

Verifies the "core closed loop" for an endpoint-less agent: a push-mode agent
that registered with ``delivery="relay"`` and holds an outbound WebSocket
(`acn listen`) receives a message in real time over that socket — not via the
offline inbox.

Flow:
  1. start a tiny local A2A HTTP server (the relay agent's --forward target)
  2. start ACN (uvicorn) against real Redis
  3. join two agents: a relay agent (delivery=relay, open) and a sender
  4. run the REAL `acn listen` CLI as the relay agent, --forward -> step 1
  5. sender POSTs /communication/send to the relay agent
  6. assert: forward server was hit AND response delivery_mode == "relay"
     AND the agent's reply payload is tunnelled back to the sender

Prereqs:
  - Redis reachable (default redis://localhost:6379; override --redis-url)
  - CLI built once:  (cd clients/cli && npm install && npm run build)

Usage:
  uv run python scripts/e2e_relay_smoke.py
  uv run python scripts/e2e_relay_smoke.py --redis-url redis://localhost:6379

Exit code 0 on PASS, non-zero on any failure. Ports are allocated dynamically
so repeated runs never collide with a previous (leaked) instance.

NOTE: connects over loopback between several local processes. If your shell
runs sandboxed with a network allowlist that blocks 127.0.0.1, run it with
unrestricted networking.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI_ENTRY = os.path.join(REPO_ROOT, "clients", "cli", "dist", "index.js")

_forward_hits: list[dict] = []


class _ForwardHandler(BaseHTTPRequestHandler):
    """Stands in for the relay agent's own local A2A server."""

    def log_message(self, *_a):  # silence default logging
        pass

    def do_POST(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw}
        _forward_hits.append({"headers": dict(self.headers), "body": parsed})
        reply = {
            "jsonrpc": "2.0",
            "id": parsed.get("id", "0"),
            "result": {
                "kind": "message",
                "messageId": "reply-1",
                "role": "agent",
                "parts": [{"kind": "text", "text": "pong from relay agent"}],
            },
        }
        payload = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2.0).status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="ADR-0012 Mode B relay e2e smoke")
    parser.add_argument("--redis-url", default="redis://localhost:6379")
    parser.add_argument(
        "--startup-timeout", type=float, default=45.0, help="ACN health wait (s)"
    )
    args = parser.parse_args()

    if not os.path.exists(CLI_ENTRY):
        print(
            f"[e2e] FAIL: CLI build not found at {CLI_ENTRY}\n"
            "       Build it first: (cd clients/cli && npm install && npm run build)"
        )
        return 2

    acn_port = _free_port()
    base = f"http://127.0.0.1:{acn_port}"
    api = f"{base}/api/v1"

    procs: list[subprocess.Popen] = []
    fwd = HTTPServer(("127.0.0.1", 0), _ForwardHandler)
    fwd_port = fwd.server_address[1]
    threading.Thread(target=fwd.serve_forever, daemon=True).start()
    print(f"[e2e] forward server up on :{fwd_port}")

    home = tempfile.mkdtemp(prefix="acn-e2e-home-")
    try:
        # 1. Start ACN
        env = {
            **os.environ,
            "DEV_MODE": "true",
            "HOST": "127.0.0.1",
            "PORT": str(acn_port),
            "REDIS_URL": args.redis_url,
            "GATEWAY_BASE_URL": base,
            "BACKEND_URL": base,
            "INTERNAL_API_TOKEN": "e2e-internal-token-at-least-32-characters-long-xx",
            "CORS_ORIGINS": '["*"]',
            "ESCROW_ENABLED": "false",
        }
        acn = subprocess.Popen(
            ["uv", "run", "uvicorn", "acn.api:app", "--host", "127.0.0.1",
             "--port", str(acn_port)],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        procs.append(acn)
        if not _wait_http(f"{base}/health", args.startup_timeout):
            print("[e2e] FAIL: ACN did not become healthy")
            if acn.stdout:
                print(acn.stdout.read()[-3000:])
            return 1
        print("[e2e] ACN healthy")

        # 2-3. Join the relay agent (open + relay, no endpoint) and a sender
        with httpx.Client(timeout=10.0) as c:
            r = c.post(
                f"{api}/agents/join",
                json={
                    "name": "Relay Smoke Agent",
                    "description": "endpoint-less agent reached over the WS relay",
                    "tags": ["e2e"],
                    "delivery": "relay",
                    "communication_policy": {"mode": "open"},
                },
            )
            r.raise_for_status()
            relay = r.json()
            relay_id, relay_key = relay["agent_id"], relay["api_key"]
            print(f"[e2e] relay agent joined: {relay_id}")

            r = c.post(
                f"{api}/agents/join",
                json={
                    "name": "Sender Smoke Agent",
                    "description": "sends a message to the relay agent",
                    "tags": ["e2e"],
                },
            )
            r.raise_for_status()
            sender = r.json()
            sender_id, sender_key = sender["agent_id"], sender["api_key"]
            print(f"[e2e] sender joined: {sender_id}")

        # 4. Run the REAL `acn listen` CLI (temp HOME so the real ~/.acn config
        #    is never touched), forwarding relayed requests to the stub server.
        cfg_dir = os.path.join(home, ".acn")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "config.json"), "w") as f:
            json.dump(
                {"base_url": base, "api_key": relay_key, "agent_id": relay_id}, f
            )

        listen = subprocess.Popen(
            ["node", CLI_ENTRY, "listen", "--forward", f"http://127.0.0.1:{fwd_port}"],
            env={**os.environ, "HOME": home},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        procs.append(listen)

        listen_log: list[str] = []
        threading.Thread(
            target=lambda: [listen_log.append(ln) for ln in listen.stdout],  # type: ignore[union-attr]
            daemon=True,
        ).start()

        deadline = time.time() + 20
        connected = False
        while time.time() < deadline:
            if any("connected as" in ln for ln in listen_log):
                connected = True
                break
            if listen.poll() is not None:
                break
            time.sleep(0.3)
        if not connected:
            print("[e2e] FAIL: `acn listen` did not connect")
            print("".join(listen_log))
            return 1
        print("[e2e] acn listen connected")
        time.sleep(1.0)  # let the server register the WS connection

        # 5. sender -> /communication/send -> relay agent (real-time over WS)
        with httpx.Client(timeout=15.0) as c:
            r = c.post(
                f"{api}/communication/send",
                headers={"Authorization": f"Bearer {sender_key}"},
                json={
                    "from_agent": sender_id,
                    "target_agent": relay_id,
                    "message": {"text": "ping over the relay"},
                },
            )
            print(f"[e2e] /send status={r.status_code} body={r.text[:400]}")
            r.raise_for_status()
            result = r.json()

        # 6. Assertions
        ok = True
        if result.get("delivery_mode") != "relay":
            print(
                f"[e2e] FAIL: expected delivery_mode=relay, "
                f"got {result.get('delivery_mode')!r} (offline inbox fallback?)"
            )
            ok = False
        if not _forward_hits:
            print("[e2e] FAIL: forward server was never hit (message not relayed)")
            ok = False
        else:
            hit = _forward_hits[-1]
            method = hit["body"].get("method")
            if method != "message/send":
                print(f"[e2e] FAIL: relayed body method={method!r}, want message/send")
                ok = False
            else:
                version_hdrs = [k for k in hit["headers"] if "version" in k.lower()]
                print(f"[e2e] forward hit: method={method}, version headers={version_hdrs}")
        reply_text = (
            (result.get("response") or {}).get("parts", [{}])[0].get("text")
            if isinstance(result.get("response"), dict)
            else None
        )
        if reply_text != "pong from relay agent":
            print(f"[e2e] FAIL: agent reply not tunnelled back; got {reply_text!r}")
            ok = False

        if ok:
            print("[e2e] PASS — real-time relay delivered end to end")
            return 0
        return 1
    finally:
        for p in procs:
            try:
                p.send_signal(signal.SIGINT)
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        fwd.shutdown()


if __name__ == "__main__":
    sys.exit(main())
