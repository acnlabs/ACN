#!/usr/bin/env bash
# Smoke: Org knowledge sidecar (no ACN required)
#
# Usage:
#   ./scripts/smoke_org_knowledge.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KB="${ROOT}/examples/org-knowledge"
ORCH="${ROOT}/examples/org-orchestrator"
PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="python3"
fi

echo "==> Unit tests (org-knowledge)"
"$PY" -m pytest "${ROOT}/tests/examples/test_org_knowledge_kb.py" -q --no-cov 2>/dev/null \
  || "$PY" -m pytest "${ROOT}/tests/examples/test_org_knowledge_kb.py" -q -p no:cov 2>/dev/null \
  || "$PY" -m pytest "${ROOT}/tests/examples/test_org_knowledge_kb.py" -q

echo "==> read_kb.py --org org_demo"
out="$("$PY" "${KB}/read_kb.py" --org org_demo)"
echo "$out" | grep -q Charter

echo "==> reject cross-org ref"
set +e
"$PY" "${KB}/read_kb.py" --org org_demo --ref 'orgkb://org_other/charter.md' >/dev/null 2>&1
rc=$?
set -e
[[ "$rc" -eq 1 ]]

echo "==> handle_wake SKIP_FETCH + kb_refs (uses sample tree)"
export ORG_KB_ROOT="${KB}/data"
export HANDLE_WAKE_SKIP_FETCH=1
wake="$(cat <<EOF
{"type":"acn.org.work_wake","schema_version":1,"org_id":"org_demo","work_id":"work_smoke","assignee":"agt_x","idempotency_key":"org_demo:work_smoke:wake:1:agt_x","kb_refs":[{"uri":"orgkb://org_demo/sop/release.md","title":"发版"}]}
EOF
)"
out="$(echo "$wake" | "$PY" "${ORCH}/handle_wake.py")"
echo "$out" | grep -q "knowledge bundle"
echo "$out" | grep -qiE "Release|SOP"

echo "==> OK smoke_org_knowledge"
