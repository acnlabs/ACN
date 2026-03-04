#!/usr/bin/env python3
"""
ACN + Backend production smoke test.

Checks:
1) ACN health endpoint
2) Backend health endpoint
3) ACN task creation flow
4) ACN payment task creation flow
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass

import requests


@dataclass
class SmokeConfig:
    acn_base_url: str
    backend_base_url: str
    timeout: int


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
    code, body = _post_json(
        _url(cfg.acn_base_url, "/api/v1/agents/join"),
        {"name": name, "description": "smoke-test agent", "skills": skills},
        cfg.timeout,
    )
    if code != 200:
        raise RuntimeError(f"join_agent failed ({code}): {body}")
    return body["agent_id"], body["api_key"]


def run_smoke(cfg: SmokeConfig) -> dict:
    ts = str(int(time.time()))
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
    tasker_id, tasker_key = _join_agent(cfg, f"smoke-tasker-{ts}", ["test"])
    task_code, task_body = _post_json(
        _url(cfg.acn_base_url, "/api/v1/tasks/agent/create"),
        {
            "title": f"Smoke task {ts}",
            "description": "Smoke test task creation and webhook path",
            "mode": "open",
            "task_type": "general",
            "required_skills": ["test"],
            "reward_amount": "0",
            "reward_currency": "ap_points",
        },
        cfg.timeout,
        headers={"Authorization": f"Bearer {tasker_key}"},
    )
    result["checks"]["task_create"] = {"status_code": task_code, "body": task_body, "tasker_id": tasker_id}
    if task_code != 200:
        result["ok"] = False
        return result

    # 3) Payment flow
    seller_id, seller_key = _join_agent(cfg, f"smoke-seller-{ts}", ["pay"])
    buyer_id, buyer_key = _join_agent(cfg, f"smoke-buyer-{ts}", ["pay"])

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
        default="https://acn-production-9ae5.up.railway.app",
        help="ACN base URL",
    )
    parser.add_argument(
        "--backend-base-url",
        default="https://agentplanet-backend-production.up.railway.app",
        help="Backend base URL",
    )
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    args = parser.parse_args()

    cfg = SmokeConfig(
        acn_base_url=args.acn_base_url,
        backend_base_url=args.backend_base_url,
        timeout=args.timeout,
    )

    try:
        result = run_smoke(cfg)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True, indent=2))
        return 1

    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
