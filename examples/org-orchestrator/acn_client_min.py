"""Minimal ACN HTTP helpers for Org orchestrator (stdlib only)."""

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


def fetch_open_work(base: str, org_id: str, api_key: str) -> dict[str, Any]:
    return _request("GET", f"{base}/orgs/{org_id}/work?open_only=true", api_key)


def fetch_members(base: str, org_id: str, api_key: str) -> dict[str, Any]:
    return _request("GET", f"{base}/orgs/{org_id}/members", api_key)


class WorkNotFoundError(LookupError):
    """Raised when list-work does not contain the requested work_id."""


def get_work(
    base: str,
    org_id: str,
    work_id: str,
    api_key: str,
) -> dict[str, Any]:
    """Fetch one work item via list API (v0 has no GET /work/{id})."""
    payload = _request(
        "GET",
        f"{base}/orgs/{org_id}/work?open_only=false",
        api_key,
    )
    for item in payload.get("work") or []:
        if str(item.get("work_id") or item.get("id") or "") == work_id:
            return item
    raise WorkNotFoundError(work_id)


def patch_work_status(
    base: str,
    org_id: str,
    work_id: str,
    api_key: str,
    status: str,
) -> dict[str, Any]:
    return _request(
        "PATCH",
        f"{base}/orgs/{org_id}/work/{work_id}",
        api_key,
        {"status": status},
    )


def patch_work(
    base: str,
    org_id: str,
    work_id: str,
    api_key: str,
    *,
    status: str | None = None,
    assignee_agent_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if status is not None:
        body["status"] = status
    if assignee_agent_id is not None:
        body["assignee_agent_id"] = assignee_agent_id
    return _request(
        "PATCH",
        f"{base}/orgs/{org_id}/work/{work_id}",
        api_key,
        body,
    )


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


def work_id(item: dict[str, Any]) -> str:
    return str(item.get("work_id") or item.get("id") or "")


def assignee_id(item: dict[str, Any]) -> str:
    return str(item.get("assignee_agent_id") or item.get("assignee") or "").strip()


def active_member_ids(members_payload: dict[str, Any]) -> set[str]:
    rows = members_payload.get("members") or members_payload.get("items") or []
    if isinstance(members_payload, list):
        rows = members_payload
    out: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        aid = str(row.get("agent_id") or "")
        status = str(row.get("status") or "active")
        if aid and status == "active":
            out.add(aid)
    return out
