#!/usr/bin/env bash
# Smoke: Execution Workspace thin Kernel dogfood
#   create Org workspace → wake/handoff GET doorplate → close + 404
#   create Task workspace → owner attestation → submit hangs attestation_id
#
# Usage:
#   ACN_BASE_URL=http://127.0.0.1:8001 ./scripts/smoke_exec_workspace.sh
#   ACN_API_KEY=acn_steward_… ACN_MEMBER_API_KEY=acn_worker_… ./scripts/smoke_exec_workspace.sh
#
# If ACN_API_KEY is unset, joins a throwaway steward (and a worker unless
# ACN_MEMBER_API_KEY is set).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ORCH="${ROOT}/examples/org-orchestrator"
RAW_BASE="${ACN_BASE_URL:-http://127.0.0.1:8001}"
PY="${ACN_PY:-python3}"

if [[ "$RAW_BASE" == */api/v1 ]]; then
  API="$RAW_BASE"
else
  API="${RAW_BASE%/}/api/v1"
fi

if [[ "$API" == *127.0.0.1* || "$API" == *localhost* ]]; then
  export NO_PROXY="127.0.0.1,localhost,::1"
  export no_proxy="$NO_PROXY"
  unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy || true
fi

GIT_URI="${SMOKE_WS_URI:-https://github.com/acnlabs/agentplanet.git}"
TS=$(date +%s)
DUP_BODY=$(mktemp -t ws-dup.XXXXXX.json)
STR_BODY=$(mktemp -t ws-str.XXXXXX.json)
HIST=$(mktemp -t ws-hist.XXXXXX.json)
IDEM_ORCH=$(mktemp -t ws-orch.XXXXXX.json)
IDEM_WAKE=$(mktemp -t ws-wake.XXXXXX.json)
IDEM_HOFF=$(mktemp -t ws-hoff.XXXXXX.json)
IDEM_FO=$(mktemp -t ws-fo.XXXXXX.json)
CLOSE_BODY=$(mktemp -t ws-close.XXXXXX.json)
trap 'rm -f "$DUP_BODY" "$STR_BODY" "$HIST" "$IDEM_ORCH" "$IDEM_WAKE" "$IDEM_HOFF" "$IDEM_FO" "$CLOSE_BODY"' EXIT

join_agent() {
  local label="$1"
  local name="ws-smoke-${label}-${TS: -4}-runner"
  local json attempt=1
  while true; do
    json=$(curl -sS -w '\n%{http_code}' -X POST "${API}/agents/join" -H "Content-Type: application/json" \
      -d "{\"name\":\"${name}\",\"description\":\"Exec workspace smoke ${label} agent for org/task doorplate path\",\"tags\":[\"smoke\",\"workspace\",\"${label}\"],\"delivery\":\"relay\"}") || true
    local code json_body
    code=$(echo "$json" | tail -n1)
    json_body=$(echo "$json" | sed '$d')
    if [[ "$code" == "429" && "$attempt" -lt 4 ]]; then
      echo "    join ${label} 429 — wait ${attempt}0s" >&2
      sleep $((attempt * 10))
      attempt=$((attempt + 1))
      continue
    fi
    KEY=$(echo "$json_body" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d.get('api_key') or d.get('agent_api_key') or '')")
    AID=$(echo "$json_body" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['agent_id'])")
    if [[ -z "$KEY" || -z "$AID" ]]; then
      echo "join ${label} failed HTTP ${code}: ${json_body:0:300}" >&2
      return 1
    fi
    return 0
  done
}

http_code() {
  # args: extra curl args… URL  → prints status code, body on stderr via file
  local out="$1"
  shift
  curl -sS -o "$out" -w '%{http_code}' "$@"
}

extract_typed() {
  # env: WANT_TYPE WORK_ID HIST_FILE
  "$PY" - <<'PY'
import json, os, sys

want = os.environ["WANT_TYPE"]
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
    for k in ("content", "message", "body", "payload", "raw"):
        if k in obj:
            out.extend(texts_from(obj[k]))
    out.append(json.dumps(obj))
    return out


def try_parse(text):
    if not isinstance(text, str):
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and obj.get("type") == want:
            return obj
    except json.JSONDecodeError:
        pass
    needle = f'{{"type": "{want}"'
    compact = f'{{"type":"{want}"'
    for n in (needle, compact):
        i = text.find(n)
        if i < 0:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == want:
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
    print(f"{want} not found in inbox history", file=sys.stderr)
    sys.exit(1)
print(json.dumps(found, ensure_ascii=False))
PY
}

extract_outer() {
  # Inbox row (transport sender). Bare typed JSON is not §4.3 proof.
  "$PY" - <<'PY'
import json, os, sys

want = os.environ["WANT_TYPE"]
work_id = os.environ["WORK_ID"]
hist = json.load(open(os.environ["HIST_FILE"]))
for m in reversed(hist.get("messages") or []):
    blob = json.dumps(m)
    if want not in blob or work_id not in blob:
        continue
    if isinstance(m, dict) and m.get("type") != want:
        print(json.dumps(m, ensure_ascii=False))
        sys.exit(0)
print(f"{want} outer message not found", file=sys.stderr)
sys.exit(1)
PY
}

echo "==> Health ${API%/api/v1}/health"
curl -fsS "${API%/api/v1}/health" >/dev/null

if [[ -n "${ACN_API_KEY:-}" ]]; then
  KEY_A="$ACN_API_KEY"
  ME_A=$(curl -fsS -H "Authorization: Bearer ${KEY_A}" "${API}/agents/me")
  AID_A=$(echo "$ME_A" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['agent_id'])")
else
  echo "==> Join throwaway steward A"
  join_agent steward
  KEY_A="$KEY"
  AID_A="$AID"
fi

if [[ -n "${ACN_MEMBER_API_KEY:-}" ]]; then
  KEY_B="$ACN_MEMBER_API_KEY"
  ME_B=$(curl -fsS -H "Authorization: Bearer ${KEY_B}" "${API}/agents/me")
  AID_B=$(echo "$ME_B" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['agent_id'])")
else
  echo "==> Join throwaway worker B"
  join_agent worker
  KEY_B="$KEY"
  AID_B="$AID"
fi

echo "==> Join stranger C (expect 404 on GET workspace)"
join_agent stranger
KEY_C="$KEY"
AID_C="$AID"
echo "    A=$AID_A B=$AID_B C=$AID_C"

AUTH_A=(-H "Authorization: Bearer ${KEY_A}" -H "Content-Type: application/json")
AUTH_B=(-H "Authorization: Bearer ${KEY_B}" -H "Content-Type: application/json")
AUTH_C=(-H "Authorization: Bearer ${KEY_C}")

NAME="smoke-ws-${TS}"
echo "==> Create Org: $NAME"
ORG=$(curl -fsS -X POST "${API}/orgs" "${AUTH_A[@]}" \
  -d "{\"display_name\":\"${NAME}\"}")
ORG_ID=$(echo "$ORG" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['org_id'])")

echo "==> Add B as member"
curl -fsS -X POST "${API}/orgs/${ORG_ID}/members" "${AUTH_A[@]}" \
  -d "{\"agent_id\":\"${AID_B}\",\"role\":\"worker\"}" >/dev/null || true

echo "==> POST workspace admit=org"
WS=$(curl -fsS -X POST "${API}/workspaces" "${AUTH_A[@]}" \
  -d "{\"display_name\":\"${NAME} yard\",\"admit\":\"org\",\"org_id\":\"${ORG_ID}\",\"execution_env\":{\"kind\":\"git\",\"uri\":\"${GIT_URI}\",\"hint\":\"smoke clone\"}}")
WS_ID=$(echo "$WS" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['workspace_id'])")
echo "    org_id=$ORG_ID workspace_id=$WS_ID"

echo "==> Org pointer has workspace_id"
curl -fsS -H "Authorization: Bearer ${KEY_A}" "${API}/orgs/${ORG_ID}" | "$PY" -c "
import json,sys
d=json.load(sys.stdin)
env=d.get('execution_env') or {}
assert env.get('workspace_id')=='${WS_ID}', env
assert env.get('uri')=='${GIT_URI}', env
print('    ok org.execution_env.workspace_id')
"

echo "==> Second active org workspace → 409"
CODE=$(http_code "$DUP_BODY" -X POST "${API}/workspaces" "${AUTH_A[@]}" \
  -d "{\"display_name\":\"dup\",\"admit\":\"org\",\"org_id\":\"${ORG_ID}\",\"execution_env\":{\"kind\":\"git\",\"uri\":\"${GIT_URI}\"}}")
[[ "$CODE" == "409" ]] || { echo "expected 409 got $CODE $(cat "$DUP_BODY")" >&2; exit 1; }
"$PY" -c "
import json
d=json.load(open('${DUP_BODY}'))
assert d.get('error_code')=='resource_conflict', d
assert (d.get('details') or {}).get('reason')=='org_workspace_active', d
print('    ok org_workspace_active')
"

echo "==> GET as steward / member / stranger"
curl -fsS -H "Authorization: Bearer ${KEY_A}" "${API}/workspaces/${WS_ID}" \
  | "$PY" -c "import json,sys; d=json.load(sys.stdin); assert d['status']=='active', d; print('    A GET active')"
curl -fsS -H "Authorization: Bearer ${KEY_B}" "${API}/workspaces/${WS_ID}" \
  | "$PY" -c "import json,sys; d=json.load(sys.stdin); assert d['workspace_id']=='${WS_ID}', d; print('    B GET ok')"
CODE=$(http_code "$STR_BODY" "${AUTH_C[@]}" "${API}/workspaces/${WS_ID}")
[[ "$CODE" == "404" ]] || { echo "stranger GET expected 404 got $CODE" >&2; exit 1; }
echo "    C GET 404"

echo "==> Create work assigned to B + orchestrator wake"
WORK=$(curl -fsS -X POST "${API}/orgs/${ORG_ID}/work" "${AUTH_A[@]}" \
  -d "{\"title\":\"workspace doorplate smoke\",\"assignee_agent_id\":\"${AID_B}\"}")
WORK_ID=$(echo "$WORK" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['work_id'])")
echo "    work_id=$WORK_ID"

export ACN_BASE_URL="$API"
export ACN_ORG_ID="$ORG_ID"
export ACN_API_KEY="$KEY_A"
export ORCHESTRATOR_IDEM_PATH="$IDEM_ORCH"
export HANDLE_WAKE_SKIP_KB=1
export HANDLE_HANDOFF_SKIP_KB=1
"$PY" "${ORCH}/run_orchestrator.py" --once

curl -fsS -H "Authorization: Bearer ${KEY_B}" \
  "${API}/communication/history/${AID_B}" >"$HIST"
export WANT_TYPE="acn.org.work_wake" WORK_ID HIST_FILE="$HIST"
WAKE_JSON=$(extract_typed)
echo "$WAKE_JSON" | "$PY" -c "
import json,sys
d=json.load(sys.stdin)
assert d.get('workspace_id')=='${WS_ID}', d
print('    wake envelope workspace_id ok')
"

echo "==> B handle_wake (GET doorplate)"
export ACN_API_KEY="$KEY_B"
export HANDLE_WAKE_IDEM_PATH="$IDEM_WAKE"
echo "$WAKE_JSON" | "$PY" "${ORCH}/handle_wake.py" | tee /tmp/handle_wake_ws.log
grep -q '\[handle_wake\] OK' /tmp/handle_wake_ws.log
grep -q "workspace ${WS_ID} status=" /tmp/handle_wake_ws.log

echo "==> Governance reassign B → A + B send_handoff"
curl -fsS -X PATCH "${API}/orgs/${ORG_ID}/work/${WORK_ID}" "${AUTH_A[@]}" \
  -d "{\"status\":\"in_progress\",\"assignee_agent_id\":\"${AID_A}\"}" >/dev/null
export ACN_API_KEY="$KEY_B"
"$PY" "${ORCH}/send_handoff.py" --work "$WORK_ID" --to "$AID_A" --note "ws smoke handoff"

echo "==> A handle_handoff (GET doorplate)"
curl -fsS -H "Authorization: Bearer ${KEY_A}" \
  "${API}/communication/history/${AID_A}" >"$HIST"
export WANT_TYPE="acn.org.work_handoff" WORK_ID HIST_FILE="$HIST"
export ACN_API_KEY="$KEY_A"
export HANDLE_HANDOFF_IDEM_PATH="$IDEM_HOFF"
set +e
HOFF_OUTER=$(extract_outer)
extract_rc=$?
set -e
if [[ "$extract_rc" -eq 0 && -n "$HOFF_OUTER" ]]; then
  echo "$HOFF_OUTER" | "$PY" "${ORCH}/handle_handoff.py" | tee /tmp/handle_handoff_ws.log
else
  echo "==> history miss — bare envelope + HANDOFF_TRUSTED_SENDER"
  HOFF_JSON=$(AID_A="$AID_A" AID_B="$AID_B" ORG_ID="$ORG_ID" WORK_ID="$WORK_ID" WS_ID="$WS_ID" GIT_URI="$GIT_URI" "$PY" - <<'PY'
import json, os
print(json.dumps({
  "type": "acn.org.work_handoff",
  "schema_version": 1,
  "idempotency_key": f"{os.environ['ORG_ID']}:{os.environ['WORK_ID']}:handoff:1:{os.environ['AID_B']}:{os.environ['AID_A']}",
  "org_id": os.environ["ORG_ID"],
  "work_id": os.environ["WORK_ID"],
  "from_agent": os.environ["AID_B"],
  "to_agent": os.environ["AID_A"],
  "title": "workspace doorplate smoke",
  "workspace_id": os.environ["WS_ID"],
  "execution_env": {
    "kind": "git",
    "uri": os.environ["GIT_URI"],
    "workspace_id": os.environ["WS_ID"],
  },
}, ensure_ascii=False))
PY
)
  export HANDOFF_TRUSTED_SENDER="$AID_B"
  echo "$HOFF_JSON" | "$PY" "${ORCH}/handle_handoff.py" | tee /tmp/handle_handoff_ws.log
  unset HANDOFF_TRUSTED_SENDER
fi
grep -qE '\[handle_handoff\] OK|deduped' /tmp/handle_handoff_ws.log
grep -q "workspace ${WS_ID} status=" /tmp/handle_handoff_ws.log

echo "==> Close workspace; member 404, owner still readable; Org pops pointer"
curl -fsS -X POST "${API}/workspaces/${WS_ID}/close" "${AUTH_A[@]}" >/dev/null
CODE=$(http_code "$CLOSE_BODY" -H "Authorization: Bearer ${KEY_B}" "${API}/workspaces/${WS_ID}")
[[ "$CODE" == "404" ]] || { echo "closed member GET expected 404 got $CODE" >&2; exit 1; }
curl -fsS -H "Authorization: Bearer ${KEY_A}" "${API}/workspaces/${WS_ID}" \
  | "$PY" -c "import json,sys; d=json.load(sys.stdin); assert d['status']=='closed', d; print('    owner GET closed')"
curl -fsS -H "Authorization: Bearer ${KEY_A}" "${API}/orgs/${ORG_ID}" | "$PY" -c "
import json,sys
env=(json.load(sys.stdin).get('execution_env') or {})
assert env.get('workspace_id') in (None, ''), env
assert env.get('uri')=='${GIT_URI}', env
print('    org pointer popped, uri kept')
"

echo "==> Closed id: member GET 404 does not block work (fail-open)"
curl -fsS -X PATCH "${API}/orgs/${ORG_ID}/work/${WORK_ID}" "${AUTH_A[@]}" \
  -d "{\"status\":\"in_progress\",\"assignee_agent_id\":\"${AID_B}\"}" >/dev/null
export ACN_API_KEY="$KEY_B"
export HANDLE_WAKE_IDEM_PATH="$IDEM_FO"
FO_WAKE=$(ORG_ID="$ORG_ID" WORK_ID="$WORK_ID" AID_B="$AID_B" WS_ID="$WS_ID" GIT_URI="$GIT_URI" "$PY" - <<'PY'
import json, os
print(json.dumps({
  "type": "acn.org.work_wake",
  "schema_version": 1,
  "idempotency_key": f"{os.environ['ORG_ID']}:{os.environ['WORK_ID']}:wake:failopen:{os.environ['AID_B']}",
  "org_id": os.environ["ORG_ID"],
  "work_id": os.environ["WORK_ID"],
  "assignee": os.environ["AID_B"],
  "workspace_id": os.environ["WS_ID"],
  "execution_env": {
    "kind": "git",
    "uri": os.environ["GIT_URI"],
    "workspace_id": os.environ["WS_ID"],
  },
}, ensure_ascii=False))
PY
)
echo "$FO_WAKE" | "$PY" "${ORCH}/handle_wake.py" | tee /tmp/handle_wake_ws_fo.log
grep -q 'work continues' /tmp/handle_wake_ws_fo.log
grep -qE '\[handle_wake\] OK|deduped' /tmp/handle_wake_ws_fo.log

echo "==> Task workspace + attestation + submit"
TASK=$(curl -fsS -X POST "${API}/tasks/agent/create" "${AUTH_A[@]}" \
  -d "{\"title\":\"workspace attest smoke\",\"description\":\"Smoke test for exec-workspace-v0 submit with attestation_id hung from owner slip.\",\"required_tags\":[\"smoke\"],\"deadline_hours\":48,\"reward\":\"0\",\"reward_currency\":\"ap_points\",\"task_type\":\"general\"}")
TASK_ID=$(echo "$TASK" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['task_id'])")
echo "    task_id=$TASK_ID"

curl -fsS -X POST "${API}/tasks/agent/${TASK_ID}/accept" "${AUTH_B[@]}" -d '{}' >/dev/null

TWS=$(curl -fsS -X POST "${API}/workspaces" "${AUTH_A[@]}" \
  -d "{\"display_name\":\"${NAME} task yard\",\"admit\":\"task\",\"task_id\":\"${TASK_ID}\",\"execution_env\":{\"kind\":\"git\",\"uri\":\"${GIT_URI}\"}}")
TWS_ID=$(echo "$TWS" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['workspace_id'])")
echo "    task workspace_id=$TWS_ID"

ATT=$(curl -fsS -X POST "${API}/workspaces/${TWS_ID}/attestations" "${AUTH_A[@]}" \
  -d "{\"agent_id\":\"${AID_B}\",\"run_id\":\"smoke-run-${TS}\",\"task_id\":\"${TASK_ID}\",\"artifact\":{\"git_sha\":\"deadbeef\"}}")
ATT_ID=$(echo "$ATT" | "$PY" -c "import json,sys; d=json.load(sys.stdin); assert d.get('kind')=='workspace_owner', d; print(d['attestation_id'])")
echo "    attestation_id=$ATT_ID"

curl -fsS -H "Authorization: Bearer ${KEY_B}" \
  "${API}/workspaces/${TWS_ID}/attestations/${ATT_ID}" \
  | "$PY" -c "import json,sys; d=json.load(sys.stdin); assert d['attestation_id']=='${ATT_ID}', d; print('    B GET attestation ok')"

SUB=$(curl -fsS -X POST "${API}/tasks/${TASK_ID}/submit" "${AUTH_B[@]}" \
  -d "{\"submission\":\"workspace smoke deliverable hung with owner slip\",\"attestation_id\":\"${ATT_ID}\"}")
echo "$SUB" | "$PY" -c "
import json,sys
d=json.load(sys.stdin)
meta=d.get('metadata') or {}
assert meta.get('attestation_id')=='${ATT_ID}', meta
arts=d.get('submission_artifacts') or d.get('artifacts') or []
assert any(a.get('attestation_id')=='${ATT_ID}' for a in arts if isinstance(a, dict)), d
print('    submit hung attestation_id')
"

echo "==> Governance PATCH work done"
curl -fsS -X PATCH "${API}/orgs/${ORG_ID}/work/${WORK_ID}" "${AUTH_A[@]}" \
  -d '{"status":"done"}' >/dev/null

echo "OK — exec workspace smoke passed (org=$ORG_ID ws=$WS_ID task=$TASK_ID att=$ATT_ID)"
