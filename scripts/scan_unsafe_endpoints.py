#!/usr/bin/env python3
"""Scan registered agents for unsafe / SSRF-prone endpoints.

Why this script exists
----------------------
The SSRF defences shipped with security audit C1b reject
``http://127.0.0.1`` / ``http://10.x.x.x`` style endpoints at
*registration* time. Agents that registered BEFORE that fix were never
re-validated, so a database that has been around since alpha may still
hold endpoints pointing into private/reserved address space. Such
endpoints are blocked at proxy-dispatch time (``safe_resolve_target``)
but they are still pollution: they break agent discovery, generate 502s
on every proxy call, and make the audit signal noisier.

This script does a one-shot scan, reusing the same
``acn.security.validate_endpoint_url`` rules so a positive hit here is
exactly what the runtime guard would reject. The script is *read-only
by design*. Operator-driven cleanup goes through:

  1. Eyeball the JSON output (``--json``) and pipe it through your own
     deletion pipeline using a real admin JWT (``DELETE /api/v1/agents/{id}``).
  2. Or, for bulk cleanup grouped by a true name prefix, call the admin
     bulk delete endpoint by hand — it requires ``X-Internal-Token`` and
     records ``ADMIN_BULK_DELETE`` audit events automatically (Phase C
     of H-audit). NOTE: that endpoint matches names with ``startswith``,
     so feeding individual agent names is unsafe (over-matches siblings
     that share the prefix). See BACKLOG: "admin bulk delete by-id mode".

Usage
-----
    # Read-only preview (default, recommended)
    INTERNAL_API_TOKEN=... python scripts/scan_unsafe_endpoints.py \\
        --acn-url https://acn-production.up.railway.app

    # Machine-readable JSON for downstream tooling
    INTERNAL_API_TOKEN=... python scripts/scan_unsafe_endpoints.py \\
        --acn-url https://acn-production.up.railway.app --json

Environment
-----------
    INTERNAL_API_TOKEN   Required. Same value as ACN's INTERNAL_API_TOKEN.

Notes
-----
- Loopback addresses are flagged unless ``--allow-loopback`` is set
  (matches ACN's own ``dev_mode=true`` runtime behaviour).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass

import httpx

# Reuse the same validator the runtime SSRF guard uses so a hit here
# matches what ACN would actually reject.
try:
    from acn.security import SSRFViolation, validate_endpoint_url
except ImportError:
    print(
        "ERROR: cannot import acn.security; run from the acn/ directory "
        "or with PYTHONPATH set to the acn package root.",
        file=sys.stderr,
    )
    sys.exit(2)


@dataclass
class UnsafeAgent:
    agent_id: str
    name: str
    owner: str | None
    endpoint: str
    reason: str


def fetch_all_agents(base_url: str, token: str, timeout: float = 30.0) -> list[dict]:
    """Fetch every registered agent (status=all) via the public list endpoint."""
    headers = {"X-Internal-Token": token}
    resp = httpx.get(
        f"{base_url}/api/v1/agents",
        params={"status": "all"},
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json().get("agents", [])


def scan_for_unsafe(
    agents: list[dict],
    *,
    allow_loopback: bool,
) -> list[UnsafeAgent]:
    """Return the subset of agents whose ``endpoint`` fails SSRF validation."""
    unsafe: list[UnsafeAgent] = []
    for a in agents:
        endpoint = a.get("endpoint") or ""
        if not endpoint:
            unsafe.append(
                UnsafeAgent(
                    agent_id=a.get("agent_id", ""),
                    name=a.get("name", ""),
                    owner=a.get("owner"),
                    endpoint=endpoint,
                    reason="endpoint is empty",
                )
            )
            continue
        try:
            validate_endpoint_url(endpoint, allow_loopback=allow_loopback)
        except SSRFViolation as exc:
            unsafe.append(
                UnsafeAgent(
                    agent_id=a.get("agent_id", ""),
                    name=a.get("name", ""),
                    owner=a.get("owner"),
                    endpoint=endpoint,
                    reason=str(exc),
                )
            )
    return unsafe


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--acn-url",
        default="http://localhost:9000",
        help="ACN base URL (default: http://localhost:9000)",
    )
    parser.add_argument(
        "--allow-loopback",
        action="store_true",
        default=False,
        help=(
            "Treat 127.0.0.0/8 + ::1 as safe (matches dev_mode=true). "
            "Other private ranges are still flagged."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit machine-readable JSON instead of human output.",
    )
    args = parser.parse_args()

    token = os.environ.get("INTERNAL_API_TOKEN")
    if not token:
        print("ERROR: INTERNAL_API_TOKEN env var required.", file=sys.stderr)
        sys.exit(1)

    base = args.acn_url.rstrip("/")

    if not args.json:
        print(f"\n=== scan_unsafe_endpoints  acn={base} ===")
        print("Mode: READ-ONLY (this script never deletes)\n")

    try:
        agents = fetch_all_agents(base, token)
    except httpx.HTTPError as e:
        print(f"ERROR: cannot fetch agent list: {e}", file=sys.stderr)
        sys.exit(1)

    unsafe = scan_for_unsafe(agents, allow_loopback=args.allow_loopback)

    if args.json:
        payload = {
            "total_agents": len(agents),
            "unsafe_count": len(unsafe),
            "unsafe": [u.__dict__ for u in unsafe],
        }
        print(json.dumps(payload, indent=2, default=str))
        return

    print(f"Total agents: {len(agents)}")
    print(f"Unsafe endpoints found: {len(unsafe)}")
    print()

    if not unsafe:
        print("All endpoints pass SSRF validation. Nothing to clean.")
        return

    print("=== Unsafe agents ===")
    for u in unsafe:
        print(
            f"  {u.agent_id:32}  name={u.name:30}  owner={u.owner or 'unowned':16}"
            f"  endpoint={u.endpoint:40}  reason={u.reason}"
        )

    print(
        "\nNext steps:\n"
        "  - rerun with --json + your own DELETE-by-id pipeline (admin JWT), OR\n"
        "  - call DELETE /api/v1/agents (X-Internal-Token, name_prefix matches"
        " by startswith — pick a prefix you own end-to-end).\n"
        "Cleanup actions trigger ADMIN_BULK_DELETE / AGENT_UNREGISTERED audit"
        " events automatically (Phase C of H-audit)."
    )


if __name__ == "__main__":
    main()
