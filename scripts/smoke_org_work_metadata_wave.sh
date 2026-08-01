#!/usr/bin/env bash
# Smoke: Org work metadata.wave dogfood (no auto fan-out).
#
# Always: offline observe + report from metadata.wave (no ACN).
# Optional live (needs governance key + migrated ACN with work.metadata):
#   ACN_BASE_URL=… ACN_API_KEY=acn_… ./scripts/smoke_org_work_metadata_wave.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ORCH="${ROOT}/examples/org-orchestrator"
PY="${ACN_PY:-python3}"
if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PY="${ROOT}/.venv/bin/python"
fi

echo "==> unit: metadata.wave graph"
"$PY" -m pytest \
  "${ROOT}/tests/examples/test_org_work_observe.py::test_wave_graph_from_metadata_and_report" \
  -q -p no:cov -o addopts=

echo "==> offline: serial wave via metadata.wave"
OBS_DIR="$(mktemp -d "${TMPDIR:-/tmp}/org-meta-wave.XXXXXX")"
EVENTS="${OBS_DIR}/events.jsonl"
SNAP="${OBS_DIR}/snap.json"
trap 'rm -rf "$OBS_DIR"' EXIT

"$PY" - <<PY
import json, sys
from pathlib import Path
sys.path.insert(0, "${ORCH}")
from work_observe import ObservationStore, report

wave_id = "wv_smoke_meta"
root = "work_root"
a, b = "work_a", "work_b"

def item(wid, status, role, assignee, **extra):
    return {
        "work_id": wid,
        "status": status,
        "assignee_agent_id": assignee,
        "metadata": {
            "wave": {
                "role": role,
                "wave_id": wave_id,
                "root_work_id": root,
            }
        },
        **extra,
    }

s = ObservationStore("${EVENTS}")
s.observe([
    item(root, "in_progress", "root", "g"),
    item(a, "in_progress", "child", "x"),
    item(b, "todo", "child", "y"),
], observed_at="2026-08-01T10:00:00+00:00")
s.observe([
    item(root, "in_progress", "root", "g"),
    item(a, "done", "child", "x"),
    item(b, "in_progress", "child", "y"),
], observed_at="2026-08-01T10:10:00+00:00")
final = [
    item(root, "done", "root", "g"),
    item(a, "done", "child", "x"),
    item(b, "done", "child", "y"),
]
s.observe(final, observed_at="2026-08-01T10:20:00+00:00")
Path("${SNAP}").write_text(json.dumps({"work": final}), encoding="utf-8")
out = report(final, s.read_events(), org_id="org_smoke", from_metadata=True)
assert out["wave_graph_source"] == "metadata.wave", out
assert out["wave_count"] == 1, out
assert "SERIAL_COLLAPSE" in out["waves"][0]["alerts"], out
print("ok offline metadata.wave SERIAL_COLLAPSE")
PY

rep="$("$PY" "${ORCH}/work_observe.py" report --events "$EVENTS" --snapshot "$SNAP" \
  --org-id org_smoke --from-metadata)"
echo "$rep" | "$PY" -c "
import json,sys
d=json.load(sys.stdin)
assert d.get('wave_graph_source')=='metadata.wave', d
assert d['wave_count']==1
assert 'SERIAL_COLLAPSE' in d['waves'][0]['alerts'], d
print('ok CLI report from metadata')
"

if [[ -z "${ACN_API_KEY:-}" ]]; then
  echo "OK — smoke_org_work_metadata_wave (offline only; set ACN_API_KEY for live)"
  exit 0
fi

RAW_BASE="${ACN_BASE_URL:-http://127.0.0.1:8000}"
if [[ "$RAW_BASE" == */api/v1 ]]; then
  API="$RAW_BASE"
else
  API="${RAW_BASE%/}/api/v1"
fi
AUTH=(-H "Authorization: Bearer ${ACN_API_KEY}" -H "Content-Type: application/json")

echo "==> live: create Org + root/children with metadata.wave"
ME=$(curl -fsS -H "Authorization: Bearer ${ACN_API_KEY}" "${API}/agents/me")
AID=$(echo "$ME" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['agent_id'])")
NAME="smoke-meta-wave-$(date +%s)"
ORG=$(curl -fsS -X POST "${API}/orgs" "${AUTH[@]}" \
  -d "{\"display_name\":\"${NAME}\"}")
ORG_ID=$(echo "$ORG" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['org_id'])")
WAVE_ID="wv_${ORG_ID: -8}"

# Probe metadata support
PROBE=$(curl -sS -w '\n%{http_code}' -X POST "${API}/orgs/${ORG_ID}/work" "${AUTH[@]}" \
  -d "{\"title\":\"meta probe\",\"metadata\":{\"wave\":{\"role\":\"root\",\"wave_id\":\"${WAVE_ID}\",\"root_work_id\":\"pending\"}}}")
PROBE_BODY=$(echo "$PROBE" | sed '$d')
PROBE_CODE=$(echo "$PROBE" | tail -n1)
if [[ "$PROBE_CODE" != "200" && "$PROBE_CODE" != "201" ]]; then
  echo "SKIP live: work.metadata not accepted HTTP ${PROBE_CODE} (migrate ACN?)"
  echo "$PROBE_BODY" | head -c 400
  echo
  echo "OK — smoke_org_work_metadata_wave (offline passed)"
  exit 0
fi
ROOT_ID=$(echo "$PROBE_BODY" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['work_id'])")
# Fix root_work_id now that we know it
curl -fsS -X PATCH "${API}/orgs/${ORG_ID}/work/${ROOT_ID}" "${AUTH[@]}" \
  -d "{\"status\":\"todo\",\"metadata\":{\"wave\":{\"role\":\"root\",\"wave_id\":\"${WAVE_ID}\",\"root_work_id\":\"${ROOT_ID}\"}}}" >/dev/null

CHILD_A=$(curl -fsS -X POST "${API}/orgs/${ORG_ID}/work" "${AUTH[@]}" \
  -d "{\"title\":\"child A\",\"assignee_agent_id\":\"${AID}\",\"metadata\":{\"wave\":{\"role\":\"child\",\"wave_id\":\"${WAVE_ID}\",\"root_work_id\":\"${ROOT_ID}\",\"shard_hint\":\"a\"}}}")
WID_A=$(echo "$CHILD_A" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['work_id'])")
CHILD_B=$(curl -fsS -X POST "${API}/orgs/${ORG_ID}/work" "${AUTH[@]}" \
  -d "{\"title\":\"child B\",\"assignee_agent_id\":\"${AID}\",\"metadata\":{\"wave\":{\"role\":\"child\",\"wave_id\":\"${WAVE_ID}\",\"root_work_id\":\"${ROOT_ID}\",\"shard_hint\":\"b\"}}}")
WID_B=$(echo "$CHILD_B" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['work_id'])")

LIVE_EVENTS="${OBS_DIR}/live.jsonl"
# Serial transitions for dogfood alert
curl -fsS -X PATCH "${API}/orgs/${ORG_ID}/work/${WID_A}" "${AUTH[@]}" \
  -d '{"status":"in_progress"}' >/dev/null
sleep 1
LIST1=$(curl -fsS -H "Authorization: Bearer ${ACN_API_KEY}" \
  "${API}/orgs/${ORG_ID}/work?open_only=false")
echo "$LIST1" | "$PY" "${ORCH}/work_observe.py" observe --events "$LIVE_EVENTS" --snapshot - \
  --observed-at "2026-08-01T12:00:00+00:00" >/dev/null

curl -fsS -X PATCH "${API}/orgs/${ORG_ID}/work/${WID_A}" "${AUTH[@]}" \
  -d '{"status":"done"}' >/dev/null
curl -fsS -X PATCH "${API}/orgs/${ORG_ID}/work/${WID_B}" "${AUTH[@]}" \
  -d '{"status":"in_progress"}' >/dev/null
sleep 1
LIST2=$(curl -fsS -H "Authorization: Bearer ${ACN_API_KEY}" \
  "${API}/orgs/${ORG_ID}/work?open_only=false")
echo "$LIST2" | "$PY" "${ORCH}/work_observe.py" observe --events "$LIVE_EVENTS" --snapshot - \
  --observed-at "2026-08-01T12:10:00+00:00" >/dev/null

curl -fsS -X PATCH "${API}/orgs/${ORG_ID}/work/${WID_B}" "${AUTH[@]}" \
  -d '{"status":"done"}' >/dev/null
curl -fsS -X PATCH "${API}/orgs/${ORG_ID}/work/${ROOT_ID}" "${AUTH[@]}" \
  -d '{"status":"done"}' >/dev/null
LIST3=$(curl -fsS -H "Authorization: Bearer ${ACN_API_KEY}" \
  "${API}/orgs/${ORG_ID}/work?open_only=false")
echo "$LIST3" | "$PY" "${ORCH}/work_observe.py" observe --events "$LIVE_EVENTS" --snapshot - \
  --observed-at "2026-08-01T12:20:00+00:00" >/dev/null

LIVE_REP=$(echo "$LIST3" | "$PY" "${ORCH}/work_observe.py" report \
  --events "$LIVE_EVENTS" --snapshot - --org-id "$ORG_ID" --from-metadata)
echo "$LIVE_REP" | "$PY" -c "
import json,sys
d=json.load(sys.stdin)
assert d.get('wave_graph_source')=='metadata.wave', d
assert d['wave_count']>=1, d
# live clocks are wall-clock sleeps — SERIAL may or may not fire; require graph + children
w=d['waves'][0]
assert w['child_count']==2, w
assert w['R']==1.0, w
print('ok live metadata.wave report', 'alerts=', w.get('alerts'))
"
echo "    org=${ORG_ID} root=${ROOT_ID} wave=${WAVE_ID}"
echo "OK — smoke_org_work_metadata_wave (offline + live)"
