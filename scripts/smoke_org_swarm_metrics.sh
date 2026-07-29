#!/usr/bin/env bash
# Smoke: Org swarm metrics M0 (fixtures only — no ACN required)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ORCH="${ROOT}/examples/org-orchestrator"
PY="${ACN_PY:-python3}"
if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PY="${ROOT}/.venv/bin/python"
fi

echo "==> unit tests"
"$PY" -m pytest "${ROOT}/tests/examples/test_org_swarm_metrics.py" -q -p no:cov \
  -o addopts=

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

echo "OK — smoke_org_swarm_metrics"
