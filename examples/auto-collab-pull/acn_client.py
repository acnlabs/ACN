"""Minimal ACN HTTP helpers for auto-collab-pull (stdlib only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise urllib.error.HTTPError(
            e.url, e.code, f"{e.reason}: {err_body}", e.headers, None
        ) from e


def agents_me(base: str, api_key: str) -> dict[str, Any]:
    return _request("GET", f"{base}/agents/me", api_key)


def get_task(base: str, task_id: str, api_key: str) -> dict[str, Any]:
    return _request("GET", f"{base}/tasks/{task_id}", api_key)


def list_participations(
    base: str, task_id: str, api_key: str
) -> list[dict[str, Any]]:
    payload = _request("GET", f"{base}/tasks/{task_id}/participations", api_key)
    rows = payload.get("participations") or payload.get("items") or []
    if isinstance(payload, list):
        rows = payload
    return [r for r in rows if isinstance(r, dict)]


def send_message(
    base: str,
    api_key: str,
    *,
    from_agent: str,
    target_agent: str,
    text: str,
) -> dict[str, Any]:
    return _request(
        "POST",
        f"{base}/communication/send",
        api_key,
        {
            "from_agent": from_agent,
            "target_agent": target_agent,
            "message": {"text": text},
            "priority": "normal",
            "message_type": "collaboration",
        },
    )


def accept_task(base: str, api_key: str, task_id: str) -> dict[str, Any]:
    return _request("POST", f"{base}/tasks/{task_id}/accept", api_key, {})


def refresh_performance(
    base: str,
    api_key: str,
    agent_id: str,
    *,
    internal_token: str | None = None,
) -> dict[str, Any]:
    """POST /agents/{id}/performance/refresh — server recomputes metadata.performance."""
    if internal_token:
        import urllib.request

        url = f"{base}/agents/{agent_id}/performance/refresh"
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers={
                "X-Internal-Token": internal_token,
                "Authorization": f"Bearer {internal_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    return _request(
        "POST",
        f"{base}/agents/{agent_id}/performance/refresh",
        api_key,
        {},
    )


def search_agents(
    base: str,
    api_key: str,
    *,
    tags: list[str] | None = None,
    status: str = "online",
    subnet: str | None = None,
    limit: int = 64,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """GET /agents — tag/skill exact filter (MVP-2a). Uses ``tag`` query param."""
    q: dict[str, str] = {
        "status": status,
        "visibility": "real",
        "limit": str(max(1, min(int(limit), 200))),
        "offset": str(max(0, int(offset))),
    }
    if tags:
        q["tag"] = ",".join(t for t in tags if t)
    if subnet:
        q["subnet"] = subnet
    url = f"{base}/agents?{urllib.parse.urlencode(q)}"
    payload = _request("GET", url, api_key)
    rows = payload.get("agents") or payload.get("items") or []
    if isinstance(payload, list):
        rows = payload
    return [r for r in rows if isinstance(r, dict)]


def invite_agent(
    base: str,
    api_key: str,
    task_id: str,
    agent_id: str,
    *,
    agent_name: str = "",
) -> dict[str, Any]:
    body: dict[str, Any] = {"agent_id": agent_id}
    if agent_name:
        body["agent_name"] = agent_name
    return _request("POST", f"{base}/tasks/{task_id}/invite", api_key, body)
