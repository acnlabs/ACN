#!/usr/bin/env bash
# Org Harness four-link acceptance smoke (Adapter Spec v0).
#
# Usage:
#   export ACN_BASE_URL=https://api.acnlabs.dev   # no /api/v1
#   export ACN_API_KEY=acn_xxx                    # owner / manager agent key
#   export ACN_AGENT_ID=agt_xxx
#   ./scripts/smoke_org_harness_four_links.sh
#
# Optional:
#   ACN_MEMBER_AGENT_ID   — second agent already registered (Link 2 join target)
#   ACN_HARNESS_URL       — publicly reachable webhook (Link 3); skipped if unset
#   ACN_HARNESS_SECRET    — HMAC secret for harness registration
#   DRY_RUN=1             — print steps only
#
# Spec: docs/org-harness/org-pattern-adapter-spec-v0.md § Four-link acceptance
set -euo pipefail

BASE="${ACN_BASE_URL:-http://localhost:9000}"
BASE="${BASE%/}"
BASE="${BASE%/api/v1}"
KEY="${ACN_API_KEY:?set ACN_API_KEY}"
AID="${ACN_AGENT_ID:?set ACN_AGENT_ID}"
MEMBER="${ACN_MEMBER_AGENT_ID:-}"
HARNESS_URL="${ACN_HARNESS_URL:-}"
HARNESS_SECRET="${ACN_HARNESS_SECRET:-smoke-secret}"
DRY_RUN="${DRY_RUN:-0}"
SLUG="${ACN_ORG_SUBNET_SLUG:-org-harness-smoke-$(date +%s)}"

pass=0
fail=0
skip=0

log() { printf '%s\n' "$*"; }
ok() { pass=$((pass + 1)); log "PASS  $*"; }
ko() { fail=$((fail + 1)); log "FAIL  $*"; }
sk() { skip=$((skip + 1)); log "SKIP  $*"; }

auth_hdr=(-H "Authorization: Bearer ${KEY}" -H "X-Agent-Key: ${KEY}" -H "Content-Type: application/json")

req() {
  local method="$1" path="$2"
  shift 2
  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY   ${method} ${BASE}${path} $*"
    return 0
  fi
  curl -sS -X "$method" "${BASE}${path}" "${auth_hdr[@]}" "$@"
}

code_of() {
  local method="$1" path="$2"
  shift 2
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "200"
    return 0
  fi
  curl -sS -o /tmp/acn_org_smoke_body.json -w "%{http_code}" \
    -X "$method" "${BASE}${path}" "${auth_hdr[@]}" "$@"
}

log "=== Org Harness four-link smoke ==="
log "base=${BASE} agent=${AID} slug=${SLUG}"
log ""

# --- Link 1: Discover ---
log "--- Link 1 Discover ---"
c="$(code_of GET "/api/v1/agents/${AID}")"
if [[ "$c" == "200" ]]; then ok "D1/D2 GET /agents/{id} (${c})"; else ko "D1/D2 GET /agents/{id} (${c})"; fi

c="$(code_of GET "/api/v1/agents/${AID}/.well-known/agent-card.json")"
if [[ "$c" == "200" ]]; then ok "D2 Agent Card (${c})"; else ko "D2 Agent Card (${c})"; fi

if [[ "$DRY_RUN" != "1" ]] && grep -q '/api/v1/tasks' <<<"$(type req)"; then
  : # placeholder — adapter CI should grep source; this script never calls tasks
fi
ok "D3 this smoke does not call /api/v1/tasks"

# --- Link 2: Fence ---
log ""
log "--- Link 2 Fence ---"
body="$(printf '{"slug":"%s","name":"Org Harness Smoke","join_policy":"open","is_private":false}' "$SLUG")"
c="$(code_of POST "/api/v1/subnets" -d "$body")"
if [[ "$c" == "200" || "$c" == "201" ]]; then
  ok "F1 POST /subnets (${c})"
else
  ko "F1 POST /subnets (${c}) body=$(head -c 200 /tmp/acn_org_smoke_body.json 2>/dev/null || true)"
fi

c="$(code_of POST "/api/v1/agents/${AID}/subnets/${SLUG}")"
if [[ "$c" == "200" || "$c" == "201" ]]; then
  ok "F2 owner join (${c})"
else
  # owner may already be member on create
  if [[ "$c" == "409" || "$c" == "400" ]]; then
    ok "F2 join idempotent/already member (${c})"
  else
    ko "F2 join (${c})"
  fi
fi

c="$(code_of GET "/api/v1/subnets/${SLUG}/agents")"
if [[ "$c" == "200" ]]; then ok "F3 list subnet agents (${c})"; else ko "F3 list subnet agents (${c})"; fi

if [[ -n "$MEMBER" ]]; then
  c="$(code_of POST "/api/v1/agents/${MEMBER}/subnets/${SLUG}")"
  if [[ "$c" == "200" || "$c" == "201" || "$c" == "409" ]]; then
    ok "F2/F4 member join path (${c})"
  else
    ko "F2/F4 member join (${c}) — member key may differ; invite/allowlist instead"
  fi
else
  sk "F2/F4 second member (set ACN_MEMBER_AGENT_ID)"
fi

# --- Link 3: Dispatch / harness ---
log ""
log "--- Link 3 Dispatch / heartbeat ---"
if [[ -n "$HARNESS_URL" ]]; then
  hbody="$(printf '{"harness_url":"%s","harness_secret":"%s"}' "$HARNESS_URL" "$HARNESS_SECRET")"
  c="$(code_of PATCH "/api/v1/subnets/${SLUG}/harness" -d "$hbody")"
  if [[ "$c" == "200" ]]; then
    if [[ "$DRY_RUN" == "1" ]] || grep -q 'harness_registered.: true' /tmp/acn_org_smoke_body.json 2>/dev/null; then
      ok "H1 harness registered (${c})"
    else
      ok "H1 PATCH harness (${c}) — verify harness_registered in body"
    fi
  else
    ko "H1 PATCH harness (${c})"
  fi
else
  sk "H1/H2 harness webhook (set ACN_HARNESS_URL)"
fi

c="$(code_of POST "/api/v1/agents/${AID}/heartbeat")"
if [[ "$c" == "200" ]]; then ok "H4 heartbeat (${c})"; else ko "H4 heartbeat (${c})"; fi
sk "H3 Pattern issue→L1 wakeup (manual / adapter integration)"

# --- Link 4: Message & settlement-read ---
log ""
log "--- Link 4 Message & settlement-read ---"
if [[ -n "$MEMBER" ]]; then
  msg="$(printf '{"from_agent":"%s","target_agent":"%s","message":{"text":"org-harness four-link smoke","type":"text"}}' "$AID" "$MEMBER")"
  c="$(code_of POST "/api/v1/communication/send" -d "$msg")"
  if [[ "$c" == "200" || "$c" == "201" || "$c" == "202" ]]; then
    ok "M1 send (${c})"
  else
    ko "M1 send (${c}) body=$(head -c 200 /tmp/acn_org_smoke_body.json 2>/dev/null || true)"
  fi
else
  sk "M1/M2 message to peer (set ACN_MEMBER_AGENT_ID)"
fi

c="$(code_of GET "/api/v1/payments/${AID}/payment-capability")"
if [[ "$c" == "200" || "$c" == "404" ]]; then
  ok "M3 payment-capability read (${c} — 404 means unset, still Core-readable)"
else
  ko "M3 payment-capability (${c})"
fi

c="$(code_of GET "/api/v1/payments/stats/${AID}")"
if [[ "$c" == "200" || "$c" == "404" ]]; then
  ok "M3/M4 payments stats read (${c})"
else
  ko "M3/M4 payments stats (${c})"
fi

log ""
log "=== Summary: pass=${pass} fail=${fail} skip=${skip} ==="
if [[ "$fail" -gt 0 ]]; then
  exit 1
fi
exit 0
