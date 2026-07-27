"""Minimal ACN Org work HTTP helpers (stdlib only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


def normalize_base(url: str) -> str:
    base = url.rstrip("/")
    if not base.endswith("/api/v1"):
        base = f"{base}/api/v1"
    return base


def _request(
    method: str,
    url: str,
    api_key: str,
    body: dict[str, Any] | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    data = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        if not raw:
            return {}
        return json.loads(raw)


def fetch_open_work(base: str, org_id: str, api_key: str) -> dict[str, Any]:
    url = f"{base}/orgs/{org_id}/work?open_only=true"
    return _request("GET", url, api_key)


def patch_work_status(
    base: str,
    org_id: str,
    work_id: str,
    api_key: str,
    status: str,
) -> dict[str, Any]:
    url = f"{base}/orgs/{org_id}/work/{work_id}"
    return _request("PATCH", url, api_key, {"status": status})


def work_id(item: dict[str, Any]) -> str:
    return str(item.get("work_id") or item.get("id") or "")
