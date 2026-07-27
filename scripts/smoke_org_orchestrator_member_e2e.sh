#!/usr/bin/env bash
# Smoke: Org orchestrator → inbox history → handle_wake → governance done
#
# Usage (on a host that can reach ACN):
#   ACN_BASE_URL=http://127.0.0.1:8001 \
#   ACN_API_KEY=acn_xxx \
#   ORCH_DIR=/path/to/examples/org-orchestrator \
#   ./scripts/smoke_org_orchestrator_member_e2e.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ORCH_DIR="${ORCH_DIR:-$ROOT/examples/org-orchestrator}"
RAW_BASE="${ACN_BASE_URL:-http://127.0.0.1:8000}"
KEY="${ACN_API_KEY:?ACN_API_KEY required}"
AUTH="Authorization: Bearer ${KEY}"

if [[ "$RAW_BASE" == */api/v1 ]]; then
  API="$RAW_BASE"
else
  API="${RAW_BASE%/}/api/v1"
fi

IDEM_ORCH="$(mktemp -t orch-idem.XXXXXX.json)"
IDEM_MEM="$(mktemp -t wake-idem.XXXXXX.json)"
HIST_FILE="$(mktemp -t hist.XXXXXX.json)"
trap 'rm -f "$IDEM_ORCH" "$IDEM_MEM" "$HIST_FILE"' EXIT

echo "==> Resolve agent"
ME=$(curl -fsS -H "$AUTH" "${API}/agents/me")
AID=$(echo "$ME" | python3 -c "import sys,json; print(json.load(sys.stdin)['agent_id'])")
echo "    agent_id=$AID"

NAME="smoke-wake-$(date +%s)"
echo "==> Create Org: $NAME"
ORG=$(curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"display_name\":\"${NAME}\"}" "${API}/orgs")
ORG_ID=$(echo "$ORG" | python3 -c "import sys,json; print(json.load(sys.stdin)['org_id'])")

echo "==> Create assigned work"
WORK=$(curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"title\":\"member wake e2e\",\"assignee_agent_id\":\"${AID}\"}" \
  "${API}/orgs/${ORG_ID}/work")
WORK_ID=$(echo "$WORK" | python3 -c "import sys,json; print(json.load(sys.stdin)['work_id'])")
echo "    org_id=$ORG_ID work_id=$WORK_ID"

echo "==> Orchestrator once"
export ACN_BASE_URL="$API"
export ACN_ORG_ID="$ORG_ID"
export ACN_API_KEY="$KEY"
export ORCHESTRATOR_IDEM_PATH="$IDEM_ORCH"
python3 "${ORCH_DIR}/run_orchestrator.py" --once

echo "==> Fetch inbox history"
curl -fsS -H "$AUTH" "${API}/communication/history/${AID}" >"$HIST_FILE"

echo "==> Extract acn.org.work_wake for this work"
WAKE_JSON=$(WORK_ID="$WORK_ID" HIST_FILE="$HIST_FILE" python3 - <<'PY'
import json, os, sys

work_id = os.environ["WORK_ID"]
hist = json.load(open(os.environ["HIST_FILE"]))
msgs = hist.get("messages") or []


def texts_from(obj):
    out = []
    if isinstance(obj, str):
        out.append(obj)
        return out
    if not isinstance(obj, dict):
        out.append(json.dumps(obj))
        return out
    if isinstance(obj.get("text"), str):
        out.append(obj["text"])
    for p in obj.get("parts") or []:
        if isinstance(p, dict) and isinstance(p.get("text"), str):
            out.append(p["text"])
    # common inbox shapes
    for k in ("content", "message", "body", "payload"):
        if k in obj:
            out.extend(texts_from(obj[k]))
    out.append(json.dumps(obj))
    return out


def try_parse(text):
    if not isinstance(text, str):
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and obj.get("type") == "acn.org.work_wake":
            return obj
    except json.JSONDecodeError:
        pass
    for needle in ('{"type": "acn.org.work_wake"', '{"type":"acn.org.work_wake"'):
        i = text.find(needle)
        if i < 0:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "acn.org.work_wake":
            return obj
    return None


found = None
for m in reversed(msgs):
    for t in texts_from(m):
        obj = try_parse(t)
        if obj and obj.get("work_id") == work_id:
            found = obj
            break
    if found:
        break

if not found:
    print("wake not found in inbox history", file=sys.stderr)
    if msgs:
        print(json.dumps(msgs[-1], ensure_ascii=False)[:1200], file=sys.stderr)
    sys.exit(1)
print(json.dumps(found, ensure_ascii=False))
PY
)
echo "    idem=$(echo "$WAKE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('idempotency_key',''))")"

echo "==> handle_wake #1 (expect OK)"
export HANDLE_WAKE_IDEM_PATH="$IDEM_MEM"
echo "$WAKE_JSON" | python3 "${ORCH_DIR}/handle_wake.py" | tee /tmp/handle_wake_e2e_1.log
grep -q '\[handle_wake\] OK' /tmp/handle_wake_e2e_1.log

echo "==> handle_wake #2 (expect dedupe)"
echo "$WAKE_JSON" | python3 "${ORCH_DIR}/handle_wake.py" | tee /tmp/handle_wake_e2e_2.log
grep -q 'deduped' /tmp/handle_wake_e2e_2.log

echo "==> Governance PATCH done"
curl -fsS -X PATCH -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"status\":\"done\"}" "${API}/orgs/${ORG_ID}/work/${WORK_ID}" >/dev/null
STATUS=$(curl -fsS -H "$AUTH" "${API}/orgs/${ORG_ID}/work?open_only=false" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); w=[x for x in d['work'] if x['work_id']=='${WORK_ID}'][0]; print(w['status'])")
if [[ "$STATUS" != "done" ]]; then
  echo "expected done got $STATUS" >&2
  exit 1
fi

echo "OK — member wake e2e passed (org_id=$ORG_ID work_id=$WORK_ID)"
