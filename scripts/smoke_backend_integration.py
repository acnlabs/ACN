#!/usr/bin/env python3
"""
ACN + Backend production smoke test.

Checks:
1) ACN health endpoint
2) Backend health endpoint
3) ACN task creation flow
4) ACN payment task creation flow

When ``ACN_INTERNAL_TOKEN`` is set in the environment, the script:
  - Registers fixtures via ``/agents/join/internal`` (so the server stamps
    ``metadata.visibility="test"`` and they are excluded from public agent
    lists / agentplanet.org/world).
  - Tears down created fixtures in a ``finally`` block via the internal
    bulk-delete endpoint to avoid accumulating stale rows in production.

Without the token, the script falls back to the public ``/agents/join``
path and skips teardown (legacy behaviour). Operators should configure the
GitHub Actions secret ``ACN_INTERNAL_TOKEN`` to get the hygienic path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field

import requests


@dataclass
class SmokeConfig:
    acn_base_url: str
    backend_base_url: str
    timeout: int
    internal_token: str | None = None
    created_agent_ids: list[str] = field(default_factory=list)


def _url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _get_json(url: str, timeout: int) -> tuple[int, dict]:
    resp = requests.get(url, timeout=timeout)
    payload = resp.json() if resp.text else {}
    return resp.status_code, payload


def _post_json(url: str, data: dict, timeout: int, headers: dict | None = None) -> tuple[int, dict]:
    resp = requests.post(url, json=data, timeout=timeout, headers=headers or {})
    payload = resp.json() if resp.text else {}
    return resp.status_code, payload


def _join_agent(cfg: SmokeConfig, name: str, skills: list[str]) -> tuple[str, str]:
    """Register a fixture agent.

    If ``cfg.internal_token`` is set, uses the internal endpoint so the
    server stamps ``visibility=test``; otherwise falls back to the public
    endpoint for backwards compatibility.
    """
    if cfg.internal_token:
        path = "/api/v1/agents/join/internal"
        headers = {"X-Internal-Token": cfg.internal_token}
    else:
        path = "/api/v1/agents/join"
        headers = None

    code, body = _post_json(
        _url(cfg.acn_base_url, path),
        {
            "name": name,
            "description": "smoke-test agent",
            "skills": skills,
            "endpoint": f"{cfg.acn_base_url}/smoke-placeholder",
        },
        cfg.timeout,
        headers=headers,
    )
    if code != 200:
        raise RuntimeError(f"join_agent failed ({code}): {body}")

    agent_id = body["agent_id"]
    cfg.created_agent_ids.append(agent_id)
    return agent_id, body["api_key"]


def _teardown_fixtures(cfg: SmokeConfig) -> dict:
    """Best-effort delete of every agent created during this run.

    Uses the admin bulk-delete endpoint with an explicit ``agent_ids``
    list (X-Internal-Token gated). Never raises — teardown failure must
    not mask the smoke result.
    """
    if not cfg.internal_token or not cfg.created_agent_ids:
        return {"skipped": True, "reason": "no token or nothing to delete"}

    try:
        resp = requests.delete(
            _url(cfg.acn_base_url, "/api/v1/agents"),
            headers={"X-Internal-Token": cfg.internal_token},
            params={
                "agent_ids": ",".join(cfg.created_agent_ids),
                "dry_run": "false",
            },
            timeout=cfg.timeout,
        )
        body = resp.json() if resp.text else {}
        return {"status_code": resp.status_code, "body": body, "ids": cfg.created_agent_ids}
    except Exception as exc:  # best-effort; never raise from teardown
        return {"error": str(exc), "ids": cfg.created_agent_ids}


def run_smoke(cfg: SmokeConfig) -> dict:
    ts = str(int(time.time()))
    # Use a short hex suffix so names don't trigger the auto-generated-name guard
    suffix = format(int(ts) % 0xFFFF, "04x")
    result: dict = {"ok": True, "timestamp": ts, "checks": {}}

    # 1) Health checks
    acn_health_code, acn_health = _get_json(_url(cfg.acn_base_url, "/health"), cfg.timeout)
    backend_health_code, backend_health = _get_json(_url(cfg.backend_base_url, "/health"), cfg.timeout)
    result["checks"]["acn_health"] = {"status_code": acn_health_code, "body": acn_health}
    result["checks"]["backend_health"] = {"status_code": backend_health_code, "body": backend_health}
    if acn_health_code != 200 or backend_health_code != 200:
        result["ok"] = False
        return result

    # 2) Task flow
    tasker_id, tasker_key = _join_agent(cfg, f"smoke-tasker-{suffix}", ["test"])
    task_code, task_body = _post_json(
        _url(cfg.acn_base_url, "/api/v1/tasks/agent/create"),
        {
            "title": f"Smoke task {ts}",
            "description": "Smoke test task creation and webhook path",
            "task_type": "general",
            "required_tags": ["test"],
            "reward": "0",
            "reward_currency": "ap_points",
            "deadline_hours": 24,
        },
        cfg.timeout,
        headers={"Authorization": f"Bearer {tasker_key}"},
    )
    result["checks"]["task_create"] = {"status_code": task_code, "body": task_body, "tasker_id": tasker_id}
    if task_code != 200:
        result["ok"] = False
        return result

    # 3) Payment flow
    seller_id, seller_key = _join_agent(cfg, f"smoke-seller-{suffix}", ["pay"])
    buyer_id, buyer_key = _join_agent(cfg, f"smoke-buyer-{suffix}", ["pay"])

    cap_code, cap_body = _post_json(
        _url(cfg.acn_base_url, f"/api/v1/payments/{seller_id}/payment-capability"),
        {
            "supported_methods": ["platform_credits"],
            "supported_networks": ["ethereum"],
            "accepts_payment": True,
            "token_pricing": {
                "input_price_per_million": 3.0,
                "output_price_per_million": 15.0,
                "currency": "USD",
            },
        },
        cfg.timeout,
        headers={"Authorization": f"Bearer {seller_key}"},
    )
    result["checks"]["payment_capability"] = {"status_code": cap_code, "body": cap_body, "seller_id": seller_id}
    if cap_code != 200:
        result["ok"] = False
        return result

    payment_code, payment_body = _post_json(
        _url(cfg.acn_base_url, "/api/v1/payments/tasks"),
        {
            "from_agent": buyer_id,
            "to_agent": seller_id,
            "amount": 1.5,
            "currency": "USD",
            "payment_method": "platform_credits",
            "network": "ethereum",
            "description": "Smoke payment task creation",
        },
        cfg.timeout,
        headers={"Authorization": f"Bearer {buyer_key}"},
    )
    result["checks"]["payment_task_create"] = {
        "status_code": payment_code,
        "body": payment_body,
        "buyer_id": buyer_id,
    }
    if payment_code != 200:
        result["ok"] = False

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test ACN + Backend main flows")
    parser.add_argument(
        "--acn-base-url",
        default="https://api.acnlabs.dev",
        help="ACN base URL",
    )
    parser.add_argument(
        "--backend-base-url",
        default="https://agentplanet-backend-production.up.railway.app",
        help="Backend base URL",
    )
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    args = parser.parse_args()

    internal_token = os.environ.get("ACN_INTERNAL_TOKEN") or None
    if not internal_token:
        print(
            "WARN: ACN_INTERNAL_TOKEN not set — falling back to public /agents/join "
            "and skipping teardown. Created agents will leak into agent lists. "
            "Configure the secret to get clean smoke runs.",
            file=sys.stderr,
        )

    cfg = SmokeConfig(
        acn_base_url=args.acn_base_url,
        backend_base_url=args.backend_base_url,
        timeout=args.timeout,
        internal_token=internal_token,
    )

    result: dict
    try:
        result = run_smoke(cfg)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    finally:
        teardown_result = _teardown_fixtures(cfg)
        if teardown_result:
            # Attach to result for visibility in CI logs
            if isinstance(result, dict):
                result.setdefault("teardown", teardown_result)

    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
