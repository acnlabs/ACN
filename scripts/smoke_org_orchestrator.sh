#!/usr/bin/env bash
# Smoke: Org orchestrator P2 — create assigned work → wake send → in_progress
#
# Usage:
#   ACN_BASE_URL=https://api.acnlabs.dev \
#   ACN_API_KEY=acn_xxx \
#   ./scripts/smoke_org_orchestrator.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RAW_BASE="${ACN_BASE_URL:-http://127.0.0.1:8000}"
KEY="${ACN_API_KEY:?ACN_API_KEY required}"
AUTH="Authorization: Bearer ${KEY}"
ORCH="${ROOT}/examples/org-orchestrator"
IDEM="$(mktemp -t org-orch-idem.XXXXXX.json)"
trap 'rm -f "$IDEM"' EXIT

if [[ "$RAW_BASE" == */api/v1 ]]; then
  API="$RAW_BASE"
else
  API="${RAW_BASE%/}/api/v1"
fi

echo "==> Resolve caller agent"
ME=$(curl -fsS -H "$AUTH" "${API}/agents/me")
AGENT_ID=$(echo "$ME" | python3 -c "import sys,json; print(json.load(sys.stdin)['agent_id'])")

NAME="smoke-orch-$(date +%s)"
echo "==> Create Org: $NAME"
ORG=$(curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"display_name\":\"${NAME}\"}" \
  "${API}/orgs")
ORG_ID=$(echo "$ORG" | python3 -c "import sys,json; print(json.load(sys.stdin)['org_id'])")

echo "==> Create open work assigned to self"
WORK=$(curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"title\":\"orchestrator smoke\",\"assignee_agent_id\":\"${AGENT_ID}\"}" \
  "${API}/orgs/${ORG_ID}/work")
WORK_ID=$(echo "$WORK" | python3 -c "import sys,json; print(json.load(sys.stdin)['work_id'])")
echo "    org_id=$ORG_ID work_id=$WORK_ID assignee=$AGENT_ID"

echo "==> Run orchestrator once"
export ACN_BASE_URL="$API"
export ACN_ORG_ID="$ORG_ID"
export ACN_API_KEY="$KEY"
export ORCHESTRATOR_IDEM_PATH="$IDEM"
python3 "${ORCH}/run_orchestrator.py" --once

echo "==> Verify idempotency recorded (includes assignee)"
python3 -c "
import json
d=json.load(open('${IDEM}'))
key='${ORG_ID}:${WORK_ID}:wake:1:${AGENT_ID}'
assert key in d.get('sent', {}), (key, d)
assert d['sent'][key].get('pending') is False
print('    idem ok', key)
"

echo "==> Verify work in_progress"
STATUS=$(curl -fsS -H "$AUTH" "${API}/orgs/${ORG_ID}/work?open_only=false" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); w=[x for x in d['work'] if x['work_id']=='${WORK_ID}'][0]; print(w['status'])")
if [[ "$STATUS" != "in_progress" ]]; then
  echo "expected status=in_progress got $STATUS" >&2
  exit 1
fi

echo "==> Second run should skip (idempotent)"
OUT=$(python3 "${ORCH}/run_orchestrator.py" --once)
echo "$OUT"
echo "$OUT" | grep -q "already sent"

echo "OK — Org orchestrator smoke passed (org_id=$ORG_ID work_id=$WORK_ID)"
