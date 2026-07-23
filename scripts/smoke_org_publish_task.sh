#!/usr/bin/env bash
# Smoke: Org → Task Pool publish bridge (network, no fence)
#
# Requires a running ACN with agent API key auth.
# Usage:
#   ACN_BASE_URL=https://api.acnlabs.dev \
#   ACN_API_KEY=acn_xxx \
#   ./scripts/smoke_org_publish_task.sh
set -euo pipefail

BASE="${ACN_BASE_URL:-http://127.0.0.1:8000}"
KEY="${ACN_API_KEY:?ACN_API_KEY required}"
AUTH="Authorization: Bearer ${KEY}"

echo "==> Resolve caller agent"
ME=$(curl -fsS -H "$AUTH" "${BASE}/api/v1/agents/me")
AGENT_ID=$(echo "$ME" | python3 -c "import sys,json; print(json.load(sys.stdin)['agent_id'])")
echo "    agent_id=$AGENT_ID"

NAME="smoke-org-pub-$(date +%s)"
echo "==> Create Org: $NAME"
ORG=$(curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"display_name\":\"${NAME}\"}" \
  "${BASE}/api/v1/orgs")
ORG_ID=$(echo "$ORG" | python3 -c "import sys,json; print(json.load(sys.stdin)['org_id'])")
SUBNET=$(echo "$ORG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('subnet_id') or d['fencing']['subnet_id'])")
echo "    org_id=$ORG_ID subnet=$SUBNET"

echo "==> Publish Task (network — no subnet_slug)"
TASK=$(curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"title\":\"Org publish smoke\",\"description\":\"Smoke test for org-task-bridge-v0 publish path.\",\"required_tags\":[\"smoke\"],\"deadline_hours\":48,\"reward\":\"0\",\"reward_currency\":\"ap_points\",\"task_type\":\"general\",\"metadata\":{\"org_id\":\"${ORG_ID}\",\"org_publish\":true}}" \
  "${BASE}/api/v1/tasks/agent/create")
TASK_ID=$(echo "$TASK" | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")
echo "    task_id=$TASK_ID"

echo "==> GET task — assert metadata + unscoped"
curl -fsS -H "$AUTH" "${BASE}/api/v1/tasks/${TASK_ID}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
meta = d.get('metadata') or {}
assert meta.get('org_id') == '${ORG_ID}', meta
assert meta.get('org_publish') is True, meta
assert not d.get('subnet_slug'), d.get('subnet_slug')
assert 'harness_secret' not in meta, meta
print('    ok org_id=%s org_publish=%s subnet=%s' % (
    meta.get('org_id'), meta.get('org_publish'), d.get('subnet_slug')))
"

echo "OK — Org publish-task smoke passed (org_id=$ORG_ID task_id=$TASK_ID)"
