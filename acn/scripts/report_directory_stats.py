#!/usr/bin/env python3
"""Report this network's agent and subnet counts to the ACN network directory.

The directory does not pull these numbers out of a network; the network
pushes them. This script reads the local analytics endpoints (agents is
public, subnets needs the internal token) and PATCHes the directory record
with the registry key. Nothing else is sent: two counts and a timestamp.

Stdlib only, so it runs anywhere Python 3.11+ runs. Intended for a schedule
(Railway cron, host cron) next to a running network:

    NETWORK_ID=acn-global \
    ANALYTICS_BASE_URL=http://127.0.0.1:8000 \
    INTERNAL_API_TOKEN=... \
    REGISTRY_URL=https://network-registry-production-5ed71a1b.up.railway.app \
    REGISTRY_KEY=... \
    python3 acn/scripts/report_directory_stats.py

Exit code is 0 on success, 2 on any failure. Secrets are never printed.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime

_TIMEOUT = 10


class ReportError(Exception):
    """A fetch or the report itself failed."""


def _get_json(url: str, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"GET {url} failed: {exc}") from exc


def fetch_counts(base_url: str, internal_token: str) -> tuple[int, int]:
    """Agent total and subnet total from the local analytics endpoints."""
    base = base_url.rstrip("/")
    headers = {"X-Internal-Token": internal_token}
    agents = _get_json(f"{base}/api/v1/analytics/agents", headers)
    subnets = _get_json(f"{base}/api/v1/analytics/subnets", headers)
    try:
        return int(agents["total"]), int(subnets["total"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReportError(f"analytics payload has no numeric total: {exc}") from exc


def report(
    registry_url: str, registry_key: str, network_id: str, agents: int, subnets: int
) -> dict:
    """PATCH the directory record. The id and sequence number are untouched."""
    body = json.dumps(
        {
            "agents": agents,
            "subnets": subnets,
            "reported_at": datetime.now(UTC).isoformat(),
        }
    ).encode()
    request = urllib.request.Request(
        f"{registry_url.rstrip('/')}/networks/{network_id}",
        data=body,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {registry_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise ReportError(f"registry PATCH answered {exc.code}") from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"registry PATCH failed: {exc}") from exc


def main() -> int:
    try:
        network_id = os.environ["NETWORK_ID"]
        registry_key = os.environ["REGISTRY_KEY"]
        internal_token = os.environ["INTERNAL_API_TOKEN"]
    except KeyError as exc:
        print(f"missing env var: {exc}", file=sys.stderr)
        return 2
    base_url = os.environ.get("ANALYTICS_BASE_URL", "http://127.0.0.1:8000")
    registry_url = os.environ.get(
        "REGISTRY_URL", "https://network-registry-production-5ed71a1b.up.railway.app"
    )
    try:
        agents, subnets = fetch_counts(base_url, internal_token)
        record = report(registry_url, registry_key, network_id, agents, subnets)
    except ReportError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"reported {network_id}: agents={agents} subnets={subnets} seq={record.get('seq')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
