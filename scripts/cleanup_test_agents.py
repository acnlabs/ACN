#!/usr/bin/env python3
"""
One-shot script to delete test/smoke agents from production ACN.

Usage:
    # Preview (dry run, default)
    python scripts/cleanup_test_agents.py --acn-url https://api.acnlabs.dev

    # Actually delete
    python scripts/cleanup_test_agents.py --acn-url https://api.acnlabs.dev --execute

Requires INTERNAL_API_TOKEN env var (same value as ACN's INTERNAL_API_TOKEN on Railway).
"""

import argparse
import os
import sys

import httpx

TEST_PREFIXES = [
    "e2e-",
    "smoke-",
    "prod-tasker-",
    "prod-buyer-",
    "prod-seller-",
    "test-wallet",
    "SmokeTest",
    "final-e2e",
    "final-v2",
    "best-effort-test",
    "e2e-agent-",
    "prod-agent-",
    # Naming convention for ad-hoc end-to-end probes that exercise prod
    # via the public `/agents/join` endpoint (see AGENTS.md "Probing
    # production"). The internal `/agents/join/internal` flow already
    # stamps `visibility=test` and self-filters out of public listings,
    # but probes that slip in via the public route get caught here as
    # the safety net.
    "probe-",
]


def is_test_agent(name: str) -> bool:
    return any(name.startswith(p) or name.lower().startswith(p.lower()) for p in TEST_PREFIXES)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cleanup test agents from ACN")
    parser.add_argument("--acn-url", default="http://localhost:9000", help="ACN base URL")
    parser.add_argument("--execute", action="store_true", help="Actually delete (default is dry-run preview)")
    args = parser.parse_args()

    token = os.environ.get("INTERNAL_API_TOKEN")
    if not token:
        print("ERROR: Set INTERNAL_API_TOKEN env var first.")
        sys.exit(1)

    headers = {"X-Internal-Token": token}
    base = args.acn_url.rstrip("/")

    print(f"ACN: {base}")
    print(f"Mode: {'EXECUTE (will delete!)' if args.execute else 'DRY RUN (preview only)'}")
    print()

    # Fetch all agents
    resp = httpx.get(f"{base}/api/v1/agents?status=all", headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    all_agents = data.get("agents", [])
    print(f"Total agents in ACN: {len(all_agents)}")

    test_agents = [a for a in all_agents if is_test_agent(a["name"])]
    real_agents = [a for a in all_agents if not is_test_agent(a["name"])]

    print(f"Test agents to delete: {len(test_agents)}")
    print(f"Real agents to keep:   {len(real_agents)}")
    print()

    if not test_agents:
        print("Nothing to delete.")
        return

    print("=== Agents to DELETE ===")
    for a in test_agents:
        print(f"  {a['name']:45} owner={a.get('owner','?')}")

    print()
    print("=== Agents to KEEP ===")
    for a in real_agents:
        print(f"  {a['name']:45} owner={a.get('owner','?')}")

    if not args.execute:
        print("\nDry run complete. Add --execute to actually delete.")
        return

    # Use the admin bulk-delete endpoint per name prefix
    print("\nDeleting via admin bulk endpoint (by name prefix)...")
    total_deleted, total_failed = 0, 0

    for prefix in TEST_PREFIXES:
        del_resp = httpx.delete(
            f"{base}/api/v1/agents",
            headers=headers,
            params={"name_prefix": prefix, "owner": "unowned", "dry_run": "false"},
            timeout=30,
        )
        if del_resp.status_code == 200:
            result = del_resp.json()
            n = result.get("deleted", 0)
            if n:
                print(f"  [{prefix}]  deleted {n}")
            total_deleted += n
            total_failed += result.get("failed", 0)
        else:
            print(f"  [{prefix}]  ERROR {del_resp.status_code}: {del_resp.text[:100]}")

    print(f"\nDone. Total deleted: {total_deleted}, Failed: {total_failed}")


if __name__ == "__main__":
    main()
