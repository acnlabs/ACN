#!/usr/bin/env bash
# Smoke: Org swarm metrics M0 + §3.3 observe (fixtures only — no ACN required)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ORCH="${ROOT}/examples/org-orchestrator"
PY="${ACN_PY:-python3}"
if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PY="${ROOT}/.venv/bin/python"
fi

echo "==> unit tests"
"$PY" -m pytest \
  "${ROOT}/tests/examples/test_org_swarm_metrics.py" \
  "${ROOT}/tests/examples/test_org_work_observe.py" \
  -q -p no:cov -o addopts=

echo "==> demo fixture"
out="$("$PY" "${ORCH}/swarm_metrics.py" "${ORCH}/fixtures/swarm_metrics_demo.json")"
echo "$out" | "$PY" -c "
import json,sys
d=json.load(sys.stdin)
assert d['wave_count']==3, d
alerts=set()
for w in d['waves']:
    alerts.update(w.get('alerts') or [])
assert 'SERIAL_COLLAPSE' in alerts, d
assert 'FAKE_PARALLEL' in alerts, d
# parallel wave should not be serial
par=[w for w in d['waves'] if w['wave_id']=='wv_parallel_demo'][0]
assert 'SERIAL_COLLAPSE' not in par['alerts'], par
print('ok alerts', sorted(alerts))
"

echo "==> observe poll-diff (synthetic)"
OBS_DIR="$(mktemp -d "${TMPDIR:-/tmp}/org-observe.XXXXXX")"
EVENTS="${OBS_DIR}/events.jsonl"
SNAP="${OBS_DIR}/snap.json"
GRAPH="${OBS_DIR}/graph.json"
cat >"$SNAP" <<'EOF'
{"work":[
  {"work_id":"root","status":"done","assignee_agent_id":"g"},
  {"work_id":"a","status":"done","assignee_agent_id":"x"},
  {"work_id":"b","status":"done","assignee_agent_id":"y"}
]}
EOF
cat >"$GRAPH" <<'EOF'
{"waves":[{"wave_id":"wv_live","root_work_id":"root","child_work_ids":["a","b"]}]}
EOF
"$PY" - <<PY
import sys
sys.path.insert(0, "${ORCH}")
from work_observe import ObservationStore
s = ObservationStore("${EVENTS}")
# serial a then b
s.observe([
  {"work_id":"root","status":"in_progress","assignee_agent_id":"g"},
  {"work_id":"a","status":"in_progress","assignee_agent_id":"x"},
  {"work_id":"b","status":"todo","assignee_agent_id":"y"},
], observed_at="2026-08-01T10:00:00+00:00")
s.observe([
  {"work_id":"root","status":"in_progress","assignee_agent_id":"g"},
  {"work_id":"a","status":"done","assignee_agent_id":"x"},
  {"work_id":"b","status":"in_progress","assignee_agent_id":"y"},
], observed_at="2026-08-01T10:10:00+00:00")
s.observe([
  {"work_id":"root","status":"done","assignee_agent_id":"g"},
  {"work_id":"a","status":"done","assignee_agent_id":"x"},
  {"work_id":"b","status":"done","assignee_agent_id":"y"},
], observed_at="2026-08-01T10:20:00+00:00")
# unchanged poll → 0 writes
assert s.observe([
  {"work_id":"root","status":"done","assignee_agent_id":"g"},
  {"work_id":"a","status":"done","assignee_agent_id":"x"},
  {"work_id":"b","status":"done","assignee_agent_id":"y"},
], observed_at="2026-08-01T10:21:00+00:00") == []
PY
rep="$("$PY" "${ORCH}/work_observe.py" report --events "$EVENTS" --snapshot "$SNAP" \
  --org-id org_smoke --wave-graph "$GRAPH")"
echo "$rep" | "$PY" -c "
import json,sys
d=json.load(sys.stdin)
assert d['window']['kind']=='window'
assert d['window']['alerts']==[]
assert d['wave_count']==1
assert 'SERIAL_COLLAPSE' in d['waves'][0]['alerts'], d
print('ok observe report')
"
rm -rf "$OBS_DIR"

echo "OK — smoke_org_swarm_metrics"
