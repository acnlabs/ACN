#!/usr/bin/env bash
# Smoke: auto-collab-pull MVP-1
#
# Offline (always):
#   ./scripts/smoke_auto_collab_pull.sh
#
# Live (optional):
#   ACN_BASE_URL=… ACN_API_KEY=acn_… ./scripts/smoke_auto_collab_pull.sh --live
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PULL="${ROOT}/examples/auto-collab-pull"
LIVE=0
if [[ "${1:-}" == "--live" ]]; then
  LIVE=1
fi

echo "==> Offline checks"
python3 "${PULL}/effective_cap.py"
python3 "${PULL}/match.py"
python3 "${PULL}/semantic.py"
python3 "${PULL}/performance.py"
python3 "${PULL}/completion.py"
python3 "${PULL}/offline_checks.py"

echo "==> Perf enrich fixture → hybrid prefers high completion_rate"
FIX="$(mktemp -t hist.XXXXXX.json)"
CACHE="$(mktemp -t perf.XXXXXX.json)"
cat >"$FIX" <<'EOF'
[
  {"status":"completed","joined_at":"2026-07-01T00:00:00Z","submitted_at":"2026-07-01T01:00:00Z"},
  {"status":"completed","joined_at":"2026-07-02T00:00:00Z","submitted_at":"2026-07-02T01:00:00Z"},
  {"status":"completed","joined_at":"2026-07-03T00:00:00Z","submitted_at":"2026-07-03T01:00:00Z"}
]
EOF
python3 "${PULL}/run_perf_enrich.py" --fixture "$FIX" --agent-id agt_strong --cache "$CACHE" --min-samples 3 >/dev/null
cat >"$FIX" <<'EOF'
[
  {"status":"rejected","joined_at":"2026-07-01T00:00:00Z","submitted_at":"2026-07-02T00:00:00Z"},
  {"status":"rejected","joined_at":"2026-07-02T00:00:00Z","submitted_at":"2026-07-03T00:00:00Z"},
  {"status":"completed","joined_at":"2026-07-03T00:00:00Z","submitted_at":"2026-07-05T00:00:00Z"}
]
EOF
python3 "${PULL}/run_perf_enrich.py" --fixture "$FIX" --agent-id agt_weak --cache "$CACHE" --min-samples 3 >/dev/null
MATCH_PERF_WEIGHT=0.5 PERF_CACHE_PATH="$CACHE" python3 - <<PY
import json, os, sys
sys.path.insert(0, "${PULL}")
from completion import PerfCache
from match import plan_invites_for_task
agents = [
  {"agent_id":"agt_weak","description":"fix login authentication","tags":["auth"],"status":"online","metadata":{}},
  {"agent_id":"agt_strong","description":"fix login authentication","tags":["auth"],"status":"online","metadata":{}},
]
agents = PerfCache("${CACHE}").merge_into_agents(agents)
task = {"title":"fix login authentication","required_tags":["auth"],"max_participants":1}
got = plan_invites_for_task(task, agents, mode="hybrid")
assert got[0]=="agt_strong", got
print("    perf-cache hybrid rank OK", got[0])
PY
rm -f "$FIX" "$CACHE"

if [[ "$LIVE" -eq 0 ]]; then
  echo "OK — auto-collab-pull offline smoke passed (pass --live for ACN e2e)"
  exit 0
fi

RAW_BASE="${ACN_BASE_URL:-http://127.0.0.1:8000}"
KEY="${ACN_API_KEY:?ACN_API_KEY required for --live}"
AUTH="Authorization: Bearer ${KEY}"
if [[ "$RAW_BASE" == */api/v1 ]]; then
  API="$RAW_BASE"
else
  API="${RAW_BASE%/}/api/v1"
fi

IDEM="$(mktemp -t auto-collab-pull-idem.XXXXXX.json)"
trap 'rm -f "$IDEM"' EXIT

echo "==> Resolve creator (puller sender; not an invitee)"
ME=$(curl -fsS -H "$AUTH" "${API}/agents/me")
CREATOR=$(echo "$ME" | python3 -c "import sys,json; print(json.load(sys.stdin)['agent_id'])")
echo "    creator=$CREATOR"

join_invitee() {
  local suffix="$1"
  local ts join
  ts=$(date +%s)
  join=$(curl -fsS -X POST "${API}/agents/join" -H "Content-Type: application/json" \
    -d "{\"name\":\"collab-pull-${suffix}-${ts: -4}\",\"description\":\"auto-collab-pull smoke invitee\",\"tags\":[\"smoke\"],\"delivery\":\"relay\"}")
  echo "$join" | python3 -c "import sys,json; print(json.load(sys.stdin)['agent_id'])"
}

if [[ -n "${ACN_INVITEE_AGENT_ID:-}" && -n "${ACN_INVITEE_AGENT_ID_2:-}" ]]; then
  AID_B="$ACN_INVITEE_AGENT_ID"
  AID_C="$ACN_INVITEE_AGENT_ID_2"
  echo "    B=$AID_B C=$AID_C (from env)"
else
  echo "==> Join throwaway invitees B then C (B1 next-seat pull)"
  AID_B=$(join_invitee b)
  AID_C=$(join_invitee c)
  echo "    B=$AID_B C=$AID_C"
fi

echo "==> Create task (max_participants=2, active_cap=1)"
TASK=$(curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{
    \"title\":\"auto-collab-pull smoke\",
    \"description\":\"Summary-only wake test for invitee puller sidecar.\",
    \"required_tags\":[\"smoke\"],
    \"deadline_hours\":48,
    \"reward\":\"0\",
    \"reward_currency\":\"ap_points\",
    \"task_type\":\"general\",
    \"max_participants\":2,
    \"metadata\":{\"sparse_collab\":{\"active_cap\":1,\"visibility\":\"invite_only\",\"disclosure\":\"summary_to_l1\"}}
  }" \
  "${API}/tasks/agent/create")
TASK_ID=$(echo "$TASK" | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")
echo "    task_id=$TASK_ID"

echo "==> Invite B then C (whitelist + best-effort A2A)"
curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"agent_id\":\"${AID_B}\"}" \
  "${API}/tasks/${TASK_ID}/invite" >/dev/null
curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"agent_id\":\"${AID_C}\"}" \
  "${API}/tasks/${TASK_ID}/invite" >/dev/null

export ACN_BASE_URL="$API"
export ACN_API_KEY="$KEY"
export ACN_TASK_ID="$TASK_ID"
export PULLER_IDEM_PATH="$IDEM"

echo "==> Puller tick 1 — should wake B only (cap=1)"
OUT1=$(python3 "${PULL}/run_puller.py" --once --task-id "$TASK_ID")
echo "$OUT1"
echo "$OUT1" | grep -q "to_wake=1"
KEY_B="${TASK_ID}:collab_pull:1:${AID_B}"
KEY_C="${TASK_ID}:collab_pull:1:${AID_C}"
python3 -c "
import json
d=json.load(open('${IDEM}'))
sent=d.get('sent', {})
assert '${KEY_B}' in sent, (sent, '${KEY_B}')
assert sent['${KEY_B}'].get('pending') is False
assert '${KEY_C}' not in sent, sent
print('    tick1 idem ok (B only)')
"

echo "==> Puller tick 2 — B1: B notified/unaccepted → pull C"
OUT2=$(python3 "${PULL}/run_puller.py" --once --task-id "$TASK_ID")
echo "$OUT2"
echo "$OUT2" | grep -E -q "to_wake=1|send → ${AID_C}"
python3 -c "
import json
d=json.load(open('${IDEM}'))
sent=d.get('sent', {})
assert '${KEY_B}' in sent and '${KEY_C}' in sent, sent
assert sent['${KEY_C}'].get('pending') is False
print('    tick2 idem ok (B+C)')
"

echo "==> Puller tick 3 — both notified, seats still open → nothing to wake"
OUT3=$(python3 "${PULL}/run_puller.py" --once --task-id "$TASK_ID")
echo "$OUT3"
echo "$OUT3" | grep -E -q "nothing to wake|to_wake=0"

echo "==> Member handle_collab_pull (parse / skip-fetch)"
ENC=$(python3 -c "
import json, sys
sys.path.insert(0, '${PULL}')
from run_puller import build_envelope
print(json.dumps(build_envelope({
  'task_id':'${TASK_ID}',
  'title':'auto-collab-pull smoke',
  'description':'Summary-only wake test',
}, '${AID_B}')))
")
HANDLE_COLLAB_PULL_SKIP_FETCH=1 \
  python3 "${PULL}/handle_collab_pull.py" <<<"$ENC" | grep -q "acn.task.collab_pull"

echo "==> MVP-2a: tag recall → invite → pull (required_tags=smoke)"
TASK2=$(curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{
    \"title\":\"auto-collab-match smoke\",
    \"description\":\"MVP-2a tag recall test\",
    \"required_tags\":[\"smoke\"],
    \"deadline_hours\":48,
    \"reward\":\"0\",
    \"reward_currency\":\"ap_points\",
    \"task_type\":\"general\",
    \"max_participants\":2,
    \"metadata\":{\"sparse_collab\":{\"active_cap\":1,\"sensitivity\":\"public\",\"disclosure\":\"summary_to_l1\"}}
  }" \
  "${API}/tasks/agent/create")
TASK2_ID=$(echo "$TASK2" | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")
echo "    task_id=$TASK2_ID"
IDEM2="$(mktemp -t auto-collab-match-idem.XXXXXX.json)"
trap 'rm -f "$IDEM" "$IDEM2"' EXIT
export PULLER_IDEM_PATH="$IDEM2"
# MVP-2a path still asserted via --mode tags; default product path is hybrid
OUTM=$(python3 "${PULL}/run_matcher.py" --task-id "$TASK2_ID" --no-pull --mode tags)
echo "$OUTM"
# Assert invite materialized by re-fetching task
curl -fsS -H "$AUTH" "${API}/tasks/${TASK2_ID}" | python3 -c "
import json,sys
t=json.load(sys.stdin)
inv=t.get('invited_agent_ids') or []
assert len(inv)>=1, t
print('    mvp2a invited', len(inv), 'agents')
"
# confidential must refuse
TASK3=$(curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{
    \"title\":\"auto-collab-match confidential\",
    \"description\":\"must refuse public match\",
    \"required_tags\":[\"smoke\"],
    \"deadline_hours\":48,
    \"reward\":\"0\",
    \"reward_currency\":\"ap_points\",
    \"task_type\":\"general\",
    \"max_participants\":1,
    \"metadata\":{\"sparse_collab\":{\"sensitivity\":\"confidential\",\"active_cap\":1}}
  }" \
  "${API}/tasks/agent/create")
TASK3_ID=$(echo "$TASK3" | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")
set +e
python3 "${PULL}/run_matcher.py" --task-id "$TASK3_ID" --no-pull --dry-run
RC=$?
set -e
[[ "$RC" -eq 3 ]] || { echo "expected exit 3 for confidential, got $RC"; exit 1; }
echo "    confidential forbid ok (exit 3)"

echo "==> MVP-2b: hybrid semantic rank (description-aware)"
TASK4=$(curl -fsS -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "{
    \"title\":\"auto-collab semantic smoke\",
    \"description\":\"Need an agent that handles smoke test collaboration invites\",
    \"required_tags\":[\"smoke\"],
    \"deadline_hours\":48,
    \"reward\":\"0\",
    \"reward_currency\":\"ap_points\",
    \"task_type\":\"general\",
    \"max_participants\":2,
    \"metadata\":{\"sparse_collab\":{\"active_cap\":1,\"sensitivity\":\"public\"}}
  }" \
  "${API}/tasks/agent/create")
TASK4_ID=$(echo "$TASK4" | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")
OUTH=$(python3 "${PULL}/run_matcher.py" --task-id "$TASK4_ID" --no-pull --mode hybrid)
echo "$OUTH"
curl -fsS -H "$AUTH" "${API}/tasks/${TASK4_ID}" | python3 -c "
import json,sys
t=json.load(sys.stdin)
inv=t.get('invited_agent_ids') or []
assert len(inv)>=1, t
print('    mvp2b hybrid invited', len(inv))
"

echo "OK — auto-collab-pull live smoke passed (mvp1=$TASK_ID mvp2a=$TASK2_ID mvp2b=$TASK4_ID)"
