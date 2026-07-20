#!/usr/bin/env bash
# Smoke: Org Harness Kernel — create → member → work → loop tick
#
# Requires a running ACN with agent API key auth.
# Usage:
#   ACN_BASE_URL=https://api.acnlabs.dev \
#   ACN_API_KEY=acn_xxx \
#   ./scripts/smoke_org_kernel.sh
set -euo pipefail

BASE="${ACN_BASE_URL:-http://127.0.0.1:8000}"
KEY="${ACN_API_KEY:?ACN_API_KEY required}"
AUTH="Authorization: Bearer ${KEY}"

echo "==> Resolve caller agent"
ME=$(curl -fsS -H "$AUTH" "${BASE}/api/v1/agents/me")
AGENT_ID=$(echo "$ME" | python3 -c "import sys,json; print(json.load(sys.stdin)['agent_id'])")
echo "    agent_id=$AGENT_ID"

NAME="smoke-org-$(date +%s)"
echo "==> Create Org: $NAME"
ORG=$(curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"display_name\":\"${NAME}\"}" \
  "${BASE}/api/v1/orgs")
ORG_ID=$(echo "$ORG" | python3 -c "import sys,json; print(json.load(sys.stdin)['org_id'])")
SUBNET=$(echo "$ORG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('subnet_id') or d['fencing']['subnet_id'])")
echo "    org_id=$ORG_ID subnet=$SUBNET"

echo "==> Show Org"
curl -fsS -H "$AUTH" "${BASE}/api/v1/orgs/${ORG_ID}" | python3 -m json.tool >/dev/null

echo "==> List members (expect steward)"
MEMBERS=$(curl -fsS -H "$AUTH" "${BASE}/api/v1/orgs/${ORG_ID}/members")
echo "$MEMBERS" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['count']>=1, d"

echo "==> Create work item"
WORK=$(curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"title\":\"smoke work\",\"assignee_agent_id\":\"${AGENT_ID}\"}" \
  "${BASE}/api/v1/orgs/${ORG_ID}/work")
WORK_ID=$(echo "$WORK" | python3 -c "import sys,json; print(json.load(sys.stdin)['work_id'])")
echo "    work_id=$WORK_ID"

echo "==> Loop tick"
TICK=$(curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{}' \
  "${BASE}/api/v1/orgs/${ORG_ID}/loop/tick")
echo "$TICK" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['open_count']>=1, d"

echo "==> Claim Org as agent"
CLAIMED=$(curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{}' \
  "${BASE}/api/v1/orgs/${ORG_ID}/claim")
echo "$CLAIMED" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['owner']['kind']=='agent', d"

echo "OK — Org Kernel smoke passed (org_id=$ORG_ID)"
