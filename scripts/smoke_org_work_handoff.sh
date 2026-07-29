#!/usr/bin/env bash
# Smoke: governance reassign + member work_handoff + handle_handoff
#
# Usage:
#   ACN_BASE_URL=http://127.0.0.1:8001 \
#   ACN_API_KEY=acn_steward_… \
#   ./scripts/smoke_org_work_handoff.sh
#
# Optional: ACN_MEMBER_API_KEY — second agent; if unset, joins a throwaway worker.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ORCH="${ROOT}/examples/org-orchestrator"
RAW_BASE="${ACN_BASE_URL:-http://127.0.0.1:8000}"
KEY_A="${ACN_API_KEY:?ACN_API_KEY required (steward/governance)}"
PY="${ACN_PY:-python3}"

if [[ "$RAW_BASE" == */api/v1 ]]; then
  API="$RAW_BASE"
else
  API="${RAW_BASE%/}/api/v1"
fi

AUTH_A=(-H "Authorization: Bearer ${KEY_A}" -H "Content-Type: application/json")

echo "==> Resolve steward A"
ME_A=$(curl -fsS -H "Authorization: Bearer ${KEY_A}" "${API}/agents/me")
AID_A=$(echo "$ME_A" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['agent_id'])")

if [[ -n "${ACN_MEMBER_API_KEY:-}" ]]; then
  KEY_B="$ACN_MEMBER_API_KEY"
  ME_B=$(curl -fsS -H "Authorization: Bearer ${KEY_B}" "${API}/agents/me")
  AID_B=$(echo "$ME_B" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['agent_id'])")
else
  echo "==> Join throwaway worker B"
  TS=$(date +%s)
  JOIN=$(curl -fsS -X POST "${API}/agents/join" -H "Content-Type: application/json" \
    -d "{\"name\":\"handoff-worker-${TS: -4}-runner\",\"description\":\"Org work_handoff smoke worker agent\",\"tags\":[\"smoke\"],\"delivery\":\"relay\"}")
  KEY_B=$(echo "$JOIN" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d.get('api_key') or d.get('agent_api_key'))")
  AID_B=$(echo "$JOIN" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['agent_id'])")
fi
echo "    A=$AID_A B=$AID_B"

NAME="smoke-handoff-$(date +%s)"
echo "==> Create Org: $NAME"
ORG=$(curl -fsS -X POST "${API}/orgs" "${AUTH_A[@]}" \
  -d "{\"display_name\":\"${NAME}\",\"plugins\":{\"knowledge\":\"git\"}}")
ORG_ID=$(echo "$ORG" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['org_id'])")

echo "==> Add B as member"
curl -fsS -X POST "${API}/orgs/${ORG_ID}/members" "${AUTH_A[@]}" \
  -d "{\"agent_id\":\"${AID_B}\",\"role\":\"worker\"}" >/dev/null || true

echo "==> Create work assigned to B"
WORK=$(curl -fsS -X POST "${API}/orgs/${ORG_ID}/work" "${AUTH_A[@]}" \
  -d "{\"title\":\"handoff smoke\",\"assignee_agent_id\":\"${AID_B}\"}")
WORK_ID=$(echo "$WORK" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['work_id'])")

echo "==> Governance reassign assignee B → A"
curl -fsS -X PATCH "${API}/orgs/${ORG_ID}/work/${WORK_ID}" "${AUTH_A[@]}" \
  -d "{\"status\":\"in_progress\",\"assignee_agent_id\":\"${AID_A}\"}" >/dev/null

echo "==> B sends work_handoff to A"
export ACN_BASE_URL="$API"
export ACN_ORG_ID="$ORG_ID"
export ACN_API_KEY="$KEY_B"
"$PY" "${ORCH}/send_handoff.py" --work "$WORK_ID" --to "$AID_A" --note "smoke handoff from B"

echo "==> A handle_handoff (inbox or trusted sender)"
HIST=$(mktemp -t handoff-hist.XXXXXX.json)
IDEM=$(mktemp -t handoff-idem.XXXXXX.json)
trap 'rm -f "$HIST" "$IDEM"' EXIT
curl -fsS -H "Authorization: Bearer ${KEY_A}" \
  "${API}/communication/history/${AID_A}" >"$HIST"

OUTER=$(WORK_ID="$WORK_ID" HIST_FILE="$HIST" AID_B="$AID_B" "$PY" - <<'PY'
import json, os, sys
work_id = os.environ["WORK_ID"]
hist = json.load(open(os.environ["HIST_FILE"]))
want_from = os.environ["AID_B"]
for m in reversed(hist.get("messages") or []):
    blob = json.dumps(m)
    if "acn.org.work_handoff" not in blob or work_id not in blob:
        continue
    # Prefer full message dict (has from_agent) for §4.3
    if isinstance(m, dict):
        print(json.dumps(m, ensure_ascii=False))
        sys.exit(0)
print("", end="")
sys.exit(1)
PY
)

export ACN_API_KEY="$KEY_A"
export HANDLE_HANDOFF_IDEM_PATH="$IDEM"
if [[ -n "$OUTER" ]]; then
  echo "$OUTER" | "$PY" "${ORCH}/handle_handoff.py" | tee /tmp/handle_handoff_1.log
else
  echo "==> history miss — fallback bare envelope + HANDOFF_TRUSTED_SENDER"
  ENV_JSON=$("$PY" "${ORCH}/send_handoff.py" --work "$WORK_ID" --to "$AID_A" --dry-run)
  # dry-run uses KEY_A now — rebuild with known from=B
  ENV_JSON=$(AID_A="$AID_A" AID_B="$AID_B" ORG_ID="$ORG_ID" WORK_ID="$WORK_ID" "$PY" - <<'PY'
import json, os
print(json.dumps({
  "type": "acn.org.work_handoff",
  "schema_version": 1,
  "idempotency_key": f"{os.environ['ORG_ID']}:{os.environ['WORK_ID']}:handoff:1:{os.environ['AID_B']}:{os.environ['AID_A']}",
  "org_id": os.environ["ORG_ID"],
  "work_id": os.environ["WORK_ID"],
  "from_agent": os.environ["AID_B"],
  "to_agent": os.environ["AID_A"],
  "title": "handoff smoke",
  "note": "fallback",
}, ensure_ascii=False))
PY
)
  export HANDOFF_TRUSTED_SENDER="$AID_B"
  echo "$ENV_JSON" | "$PY" "${ORCH}/handle_handoff.py" | tee /tmp/handle_handoff_1.log
fi
grep -qE '\[handle_handoff\] OK|deduped' /tmp/handle_handoff_1.log

echo "==> Spoof must fail (envelope from=B but trusted sender=A)"
SPOOF=$(AID_A="$AID_A" AID_B="$AID_B" ORG_ID="$ORG_ID" WORK_ID="$WORK_ID" "$PY" - <<'PY'
import json, os
print(json.dumps({
  "type": "acn.org.work_handoff",
  "schema_version": 1,
  "idempotency_key": f"{os.environ['ORG_ID']}:{os.environ['WORK_ID']}:handoff:1:{os.environ['AID_B']}:{os.environ['AID_A']}:spoof",
  "org_id": os.environ["ORG_ID"],
  "work_id": os.environ["WORK_ID"],
  "from_agent": os.environ["AID_B"],
  "to_agent": os.environ["AID_A"],
  "title": "spoof",
}, ensure_ascii=False))
PY
)
export HANDOFF_TRUSTED_SENDER="$AID_A"
export HANDLE_HANDOFF_SKIP_FETCH=1
set +e
echo "$SPOOF" | "$PY" "${ORCH}/handle_handoff.py" >/tmp/handle_handoff_spoof.log 2>&1
rc=$?
set -e
unset HANDLE_HANDOFF_SKIP_FETCH HANDOFF_TRUSTED_SENDER || true
[[ "$rc" -ne 0 ]] || { echo "expected spoof reject"; cat /tmp/handle_handoff_spoof.log; exit 1; }

echo "==> Governance done"
curl -fsS -X PATCH "${API}/orgs/${ORG_ID}/work/${WORK_ID}" "${AUTH_A[@]}" \
  -d '{"status":"done"}' >/dev/null

echo "OK — work_handoff smoke passed (org=$ORG_ID work=$WORK_ID)"
