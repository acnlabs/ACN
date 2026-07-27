#!/usr/bin/env bash
# Smoke: Org Loop spawn sidecar (C2) — create work → sidecar → done
#
# Usage:
#   ACN_BASE_URL=https://api.acnlabs.dev \
#   ACN_API_KEY=acn_xxx \
#   ./scripts/smoke_org_loop_spawn_sidecar.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${ACN_BASE_URL:-http://127.0.0.1:8000}"
KEY="${ACN_API_KEY:?ACN_API_KEY required}"
AUTH="Authorization: Bearer ${KEY}"
SIDECAR="${ROOT}/examples/org-loop-spawn-sidecar"

echo "==> Resolve caller agent"
ME=$(curl -fsS -H "$AUTH" "${BASE}/api/v1/agents/me")
AGENT_ID=$(echo "$ME" | python3 -c "import sys,json; print(json.load(sys.stdin)['agent_id'])")

NAME="smoke-sidecar-$(date +%s)"
echo "==> Create Org: $NAME"
ORG=$(curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"display_name\":\"${NAME}\"}" \
  "${BASE}/api/v1/orgs")
ORG_ID=$(echo "$ORG" | python3 -c "import sys,json; print(json.load(sys.stdin)['org_id'])")

echo "==> Create open work"
WORK=$(curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"title\":\"sidecar smoke\",\"assignee_agent_id\":\"${AGENT_ID}\"}" \
  "${BASE}/api/v1/orgs/${ORG_ID}/work")
WORK_ID=$(echo "$WORK" | python3 -c "import sys,json; print(json.load(sys.stdin)['work_id'])")
echo "    org_id=$ORG_ID work_id=$WORK_ID"

echo "==> Run sidecar once (SPAWN_COMMAND=true)"
export ACN_BASE_URL="$BASE"
export ACN_ORG_ID="$ORG_ID"
export ACN_API_KEY="$KEY"
export SPAWN_COMMAND="true"
python3 "${SIDECAR}/run_sidecar.py" --once

echo "==> Verify work done"
STATUS=$(curl -fsS -H "$AUTH" "${BASE}/api/v1/orgs/${ORG_ID}/work?open_only=false" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); w=[x for x in d['work'] if x['work_id']=='${WORK_ID}'][0]; print(w['status'])")
if [[ "$STATUS" != "done" ]]; then
  echo "expected status=done got $STATUS" >&2
  exit 1
fi

echo "OK — Org Loop spawn sidecar smoke passed (org_id=$ORG_ID work_id=$WORK_ID)"
