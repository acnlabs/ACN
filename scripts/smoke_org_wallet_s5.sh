#!/usr/bin/env bash
# Smoke: Org wallet S5 — ownership sync + dissolve freeze
#
# Requires:
#   ACN_BASE_URL          e.g. https://acn.acnlabs.cn
#   ACN_API_KEY           steward agent key (creates Org)
#   BACKEND_URL           AgentPlanet Backend base (no trailing slash)
#   INTERNAL_API_TOKEN    Backend internal token (fund / topup)
#   ACN_INTERNAL_TOKEN    ACN X-Internal-Token for join/internal + teardown
#                         (defaults to INTERNAL_API_TOKEN when unset)
#
# Optional:
#   POLL_SECONDS          max wait for webhook sync (default 45)
#   REWARD                credits to topup (default 20)
#
# Hygiene (see acn/AGENTS.md):
#   Transfer-target agent is registered via POST /agents/join/internal
#   (visibility=test, probe- name prefix) and bulk-deleted on exit.
#
# Usage:
#   ACN_BASE_URL=https://acn.acnlabs.cn \
#   ACN_API_KEY=acn_xxx \
#   BACKEND_URL=https://api.acnlabs.cn \
#   INTERNAL_API_TOKEN=… \
#   ACN_INTERNAL_TOKEN=… \
#   ./scripts/smoke_org_wallet_s5.sh
set -euo pipefail

BASE="${ACN_BASE_URL:-http://127.0.0.1:8000}"
BASE="${BASE%/}"
BASE="${BASE%/api/v1}"
KEY="${ACN_API_KEY:?ACN_API_KEY required}"
BACKEND="${BACKEND_URL:?BACKEND_URL required}"
BACKEND="${BACKEND%/}"
INTERNAL="${INTERNAL_API_TOKEN:?INTERNAL_API_TOKEN required}"
ACN_INTERNAL="${ACN_INTERNAL_TOKEN:-$INTERNAL}"
REWARD="${REWARD:-20}"
POLL_SECONDS="${POLL_SECONDS:-45}"
AUTH="Authorization: Bearer ${KEY}"
INT="X-Internal-Token: ${INTERNAL}"
ACN_INT="X-Internal-Token: ${ACN_INTERNAL}"

AGENT_B=""
KEY_B=""

cleanup() {
  local code=$?
  if [[ -n "${AGENT_B}" ]]; then
    echo "==> Cleanup probe agent ${AGENT_B}"
    # Prefer admin bulk-delete (internal); fall back to agent self-unregister.
    curl --noproxy '*' -sS -X DELETE \
      -H "$ACN_INT" \
      "${BASE}/api/v1/agents?agent_ids=${AGENT_B}&dry_run=false" >/dev/null 2>&1 \
      || curl --noproxy '*' -sS -X DELETE \
           -H "Authorization: Bearer ${KEY_B}" \
           "${BASE}/api/v1/agents/${AGENT_B}?confirm=true" >/dev/null 2>&1 \
      || true
  fi
  return "$code"
}
trap cleanup EXIT

wait_wallet() {
  local org_id="$1" expect_owner="$2" expect_status="${3:-}"
  local i=0 owner status
  while [[ $i -lt $POLL_SECONDS ]]; do
    local body
    body=$(curl --noproxy '*' -fsS "${BACKEND}/api/org-wallets/${org_id}" || true)
    owner=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('owner_id') or '')" 2>/dev/null || true)
    status=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status') or '')" 2>/dev/null || true)
    if [[ -n "$expect_status" ]]; then
      if [[ "$owner" == "$expect_owner" && "$status" == "$expect_status" ]]; then
        echo "    wallet owner_id=$owner status=$status"
        return 0
      fi
    else
      if [[ "$owner" == "$expect_owner" ]]; then
        echo "    wallet owner_id=$owner status=$status"
        return 0
      fi
    fi
    sleep 1
    i=$((i + 1))
  done
  echo "TIMEOUT waiting wallet org=$org_id owner=$expect_owner status=${expect_status:-*} (last owner=$owner status=$status)" >&2
  return 1
}

echo "==> Resolve steward agent"
ME=$(curl --noproxy '*' -fsS -H "$AUTH" "${BASE}/api/v1/agents/me")
AGENT_A=$(echo "$ME" | python3 -c "import sys,json; print(json.load(sys.stdin)['agent_id'])")
echo "    agent_a=$AGENT_A"

echo "==> Join probe agent B (internal, visibility=test)"
JOIN_B=$(curl --noproxy '*' -fsS -X POST -H "$ACN_INT" -H "Content-Type: application/json" \
  -d "{\"name\":\"probe-s5-transfer-target\",\"description\":\"S5 ownership sync transfer target (test fixture)\",\"tags\":[\"smoke\",\"probe\"]}" \
  "${BASE}/api/v1/agents/join/internal")
AGENT_B=$(echo "$JOIN_B" | python3 -c "import sys,json; print(json.load(sys.stdin)['agent_id'])")
KEY_B=$(echo "$JOIN_B" | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])")
echo "    agent_b=$AGENT_B"

NAME="smoke-org-s5-$(date +%s)"
echo "==> Create Org: $NAME"
ORG=$(curl --noproxy '*' -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"display_name\":\"${NAME}\"}" \
  "${BASE}/api/v1/orgs")
ORG_ID=$(echo "$ORG" | python3 -c "import sys,json; print(json.load(sys.stdin)['org_id'])")
echo "    org_id=$ORG_ID"

echo "==> Fund agent A + topup Org wallet"
curl --noproxy '*' -fsS -X POST -H "$INT" -H "Content-Type: application/json" \
  -d "{\"owner_id\":null}" \
  "${BACKEND}/api/agent-wallets/${AGENT_A}" >/dev/null || true
curl --noproxy '*' -fsS -X POST -H "$INT" -H "Content-Type: application/json" \
  -d "{\"amount\":$((REWARD * 3)),\"description\":\"smoke s5 fund agent\"}" \
  "${BACKEND}/api/agent-wallets/${AGENT_A}/receive" >/dev/null
curl --noproxy '*' -fsS -X POST -H "$INT" -H "Content-Type: application/json" \
  -d "{\"owner_id\":\"${AGENT_A}\"}" \
  "${BACKEND}/api/org-wallets/${ORG_ID}" >/dev/null || true
OW=$(curl --noproxy '*' -fsS -X POST -H "$INT" -H "Content-Type: application/json" \
  -d "{\"amount\":${REWARD},\"from_subject_id\":\"${AGENT_A}\",\"description\":\"smoke s5 org topup\"}" \
  "${BACKEND}/api/org-wallets/${ORG_ID}/topup-internal")
echo "$OW" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d.get('org_id') == '${ORG_ID}', d
assert d.get('owner_id') == '${AGENT_A}', d
print('    balance=%s owner_id=%s status=%s' % (d.get('balance'), d.get('owner_id'), d.get('status')))
"

# Plant a wrong owner_id so claim's org.owner_changed must move B → A.
# (Topup above sets owner_id=A via treasury; without this plant, wait_wallet(A)
# would pass even if the claim webhook never fires.)
echo "==> Plant wrong wallet owner_id=B (pre-claim)"
curl --noproxy '*' -fsS -X POST -H "$INT" -H "Content-Type: application/json" \
  -d "{\"owner_id\":\"${AGENT_B}\"}" \
  "${BACKEND}/api/org-wallets/${ORG_ID}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d.get('owner_id') == '${AGENT_B}', d
print('    planted owner_id=%s' % d.get('owner_id'))
"

echo "==> Claim Org as agent A (expect webhook → owner_id A)"
curl --noproxy '*' -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{}' \
  "${BASE}/api/v1/orgs/${ORG_ID}/claim" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['owner']['kind'] == 'agent', d
assert d['owner']['subject'] == '${AGENT_A}', d
print('    claimed owner=agent/' + d['owner']['subject'])
"
wait_wallet "$ORG_ID" "$AGENT_A"

echo "==> Transfer Org A → B"
curl --noproxy '*' -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"new_owner_kind\":\"agent\",\"new_owner_subject\":\"${AGENT_B}\"}" \
  "${BASE}/api/v1/orgs/${ORG_ID}/transfer" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['owner']['subject'] == '${AGENT_B}', d
print('    transferred owner=agent/' + d['owner']['subject'])
"
wait_wallet "$ORG_ID" "$AGENT_B"

echo "==> Release Org (as B)"
curl --noproxy '*' -fsS -X POST -H "Authorization: Bearer ${KEY_B}" \
  "${BASE}/api/v1/orgs/${ORG_ID}/release" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['owner']['kind'] == 'none', d
print('    released owner=none')
"
# treasury falls back to created_by (A)
wait_wallet "$ORG_ID" "$AGENT_A"

echo "==> Dissolve Org (governance = created_by A)"
curl --noproxy '*' -fsS -X POST -H "$AUTH" \
  "${BASE}/api/v1/orgs/${ORG_ID}/dissolve" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d.get('status') == 'dissolved', d
print('    org status=dissolved')
"
wait_wallet "$ORG_ID" "$AGENT_A" "frozen"

echo "==> Topup must fail while frozen"
set +e
TOPUP_ERR=$(curl --noproxy '*' -sS -o /tmp/s5_topup.json -w "%{http_code}" -X POST \
  -H "$INT" -H "Content-Type: application/json" \
  -d "{\"amount\":1,\"from_subject_id\":\"${AGENT_A}\",\"description\":\"should fail\"}" \
  "${BACKEND}/api/org-wallets/${ORG_ID}/topup-internal")
set -e
python3 -c "
code = int('${TOPUP_ERR}')
assert code >= 400, ('expected topup reject, got', code, open('/tmp/s5_topup.json').read())
print('    topup rejected http=%s' % code)
"

echo "OK — Org wallet S5 smoke passed (org_id=$ORG_ID)"
