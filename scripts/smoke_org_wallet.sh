#!/usr/bin/env bash
# Smoke: Org wallet — Org-paid publish + cancel refund (org-wallet-v0)
#
# Requires:
#   ACN_BASE_URL   e.g. https://api.acnlabs.dev
#   ACN_API_KEY    agent API key that can create Orgs (treasury = this agent)
#   BACKEND_URL    AgentPlanet Backend base (no trailing slash)
#   INTERNAL_API_TOKEN  shared Backend internal token
#
# Optional:
#   REWARD         credits to lock (default 10)
#   SKIP_FUND=1    skip Backend topup (Org wallet must already have balance)
#
# Usage:
#   ACN_BASE_URL=https://api.acnlabs.dev \
#   ACN_API_KEY=acn_xxx \
#   BACKEND_URL=https://… \
#   INTERNAL_API_TOKEN=… \
#   ./scripts/smoke_org_wallet.sh
set -euo pipefail

BASE="${ACN_BASE_URL:-http://127.0.0.1:8000}"
KEY="${ACN_API_KEY:?ACN_API_KEY required}"
BACKEND="${BACKEND_URL:?BACKEND_URL required}"
INTERNAL="${INTERNAL_API_TOKEN:?INTERNAL_API_TOKEN required}"
REWARD="${REWARD:-10}"
AUTH="Authorization: Bearer ${KEY}"
INT="X-Internal-Token: ${INTERNAL}"

echo "==> Resolve caller agent (treasury)"
ME=$(curl --noproxy '*' -fsS -H "$AUTH" "${BASE}/api/v1/agents/me")
AGENT_ID=$(echo "$ME" | python3 -c "import sys,json; print(json.load(sys.stdin)['agent_id'])")
echo "    agent_id=$AGENT_ID"

NAME="smoke-org-wallet-$(date +%s)"
echo "==> Create Org: $NAME"
ORG=$(curl --noproxy '*' -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"display_name\":\"${NAME}\"}" \
  "${BASE}/api/v1/orgs")
ORG_ID=$(echo "$ORG" | python3 -c "import sys,json; print(json.load(sys.stdin)['org_id'])")
echo "    org_id=$ORG_ID"

if [[ "${SKIP_FUND:-0}" != "1" ]]; then
  echo "==> Ensure agent wallet has funds (internal receive)"
  curl --noproxy '*' -fsS -X POST -H "$INT" -H "Content-Type: application/json" \
    -d "{\"amount\":$((REWARD * 3)),\"description\":\"smoke fund agent for org topup\"}" \
    "${BACKEND}/api/agent-wallets/${AGENT_ID}/receive" >/dev/null \
    || curl --noproxy '*' -fsS -X POST -H "$INT" -H "Content-Type: application/json" \
         -d "{\"owner_id\":null}" \
         "${BACKEND}/api/agent-wallets/${AGENT_ID}" >/dev/null
  # receive again after create
  curl --noproxy '*' -fsS -X POST -H "$INT" -H "Content-Type: application/json" \
    -d "{\"amount\":$((REWARD * 3)),\"description\":\"smoke fund agent for org topup\"}" \
    "${BACKEND}/api/agent-wallets/${AGENT_ID}/receive" >/dev/null

  echo "==> Topup Org wallet from agent treasury (internal)"
  curl --noproxy '*' -fsS -X POST -H "$INT" -H "Content-Type: application/json" \
    -d "{\"owner_id\":\"${AGENT_ID}\"}" \
    "${BACKEND}/api/org-wallets/${ORG_ID}" >/dev/null || true
  OW=$(curl --noproxy '*' -fsS -X POST -H "$INT" -H "Content-Type: application/json" \
    -d "{\"amount\":$((REWARD * 2)),\"from_subject_id\":\"${AGENT_ID}\",\"description\":\"smoke org topup\"}" \
    "${BACKEND}/api/org-wallets/${ORG_ID}/topup-internal")
  echo "$OW" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d.get('org_id') == '${ORG_ID}', d
assert int(d.get('balance', 0)) >= ${REWARD}, d
print('    org balance=%s' % d.get('balance'))
"
fi

BAL_BEFORE=$(curl --noproxy '*' -fsS "${BACKEND}/api/org-wallets/${ORG_ID}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['balance'])")
echo "==> Org balance before publish: $BAL_BEFORE"

echo "==> Publish Org-paid task (pay_from_org, reward=$REWARD)"
TASK=$(curl --noproxy '*' -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"title\":\"Org wallet smoke bounty\",\"description\":\"Smoke test for org-wallet-v0 Org-paid publish and cancel refund.\",\"required_tags\":[\"smoke\"],\"deadline_hours\":48,\"reward\":\"${REWARD}\",\"task_type\":\"general\",\"pay_from_org\":true}" \
  "${BASE}/api/v1/orgs/${ORG_ID}/publish-task")
TASK_ID=$(echo "$TASK" | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")
echo "$TASK" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d.get('creator_type') == 'org', d
assert d.get('creator_id') == '${ORG_ID}', d
assert d.get('reward_currency') == 'credits', d
assert d.get('use_escrow') is True, d
meta = d.get('metadata') or {}
assert meta.get('org_id') == '${ORG_ID}', meta
assert meta.get('org_pay') is True, meta
print('    task_id=%s creator=%s/%s escrow=%s' % (
    d['task_id'], d['creator_type'], d['creator_id'], d.get('use_escrow')))
"

BAL_LOCKED=$(curl --noproxy '*' -fsS "${BACKEND}/api/org-wallets/${ORG_ID}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['balance'])")
echo "==> Org balance after lock: $BAL_LOCKED (expect $((BAL_BEFORE - REWARD)))"
python3 -c "
assert int('${BAL_LOCKED}') == int('${BAL_BEFORE}') - int('${REWARD}'), (
    '${BAL_BEFORE}', '${BAL_LOCKED}', '${REWARD}')
print('    ok locked')
"

echo "==> Cancel Org-paid task (treasury) — escrow refund"
curl --noproxy '*' -fsS -X POST -H "$AUTH" \
  "${BASE}/api/v1/tasks/${TASK_ID}/cancel" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d.get('status') == 'cancelled', d
print('    status=cancelled')
"

BAL_AFTER=$(curl --noproxy '*' -fsS "${BACKEND}/api/org-wallets/${ORG_ID}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['balance'])")
echo "==> Org balance after refund: $BAL_AFTER (expect $BAL_BEFORE)"
python3 -c "
assert int('${BAL_AFTER}') == int('${BAL_BEFORE}'), ('${BAL_BEFORE}', '${BAL_AFTER}')
print('    ok refunded')
"

echo "OK — Org wallet smoke passed (org_id=$ORG_ID task_id=$TASK_ID)"
