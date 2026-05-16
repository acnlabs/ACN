# ACN <-> Backend Operations Guide

This page is the production runbook for ACN and Agentplanet-backend integration.

## 1) Required Railway Variables

### ACN service

| Variable | Example | Notes |
|---|---|---|
| `BACKEND_URL` | `https://agentplanet-backend-production.up.railway.app` | Backend base URL used by ACN clients/integration logic |
| `WEBHOOK_URL` | `https://agentplanet-backend-production.up.railway.app/api/webhooks/acn/payment-events` | ACN webhook callback target |
| `WEBHOOK_SECRET` | `***` | HMAC secret for webhook signing |
| `INTERNAL_API_TOKEN` | `***` | Service-to-service token (must match backend) |

### Agentplanet-backend service

| Variable | Example | Notes |
|---|---|---|
| `ACN_URL` | `https://api.acnlabs.dev` | ACN base URL used by backend |
| `ACN_WEBHOOK_SECRET` | `***` | HMAC verification secret (must match ACN `WEBHOOK_SECRET`) |
| `INTERNAL_API_TOKEN` | `***` | Service-to-service token (must match ACN) |

## 2) Consistency Rules (Do Not Break)

1. `ACN.WEBHOOK_SECRET` **must equal** `BACKEND.ACN_WEBHOOK_SECRET`
2. `ACN.INTERNAL_API_TOKEN` **must equal** `BACKEND.INTERNAL_API_TOKEN`
3. `ACN.WEBHOOK_URL` must point to backend endpoint:  
   `/api/webhooks/acn/payment-events`
4. `BACKEND.ACN_URL` must point to the active ACN public/internal domain

## 3) Post-Deploy Verification

Use the smoke workflow or script after each deploy:

- GitHub Actions workflow: `Smoke Backend Integration`
- Local/manual script:

```bash
python3 scripts/smoke_backend_integration.py
```

Expected result:

- `acn_health.status_code = 200`
- `backend_health.status_code = 200`
- `task_create.status_code = 200`
- `payment_task_create.status_code = 200`

## 4) Alerting and Fast Triage

Track these keywords in Railway logs (ACN + Backend):

- `Webhook failed`
- `create_payment_task_failed`
- `Invalid webhook signature`
- `422 Unprocessable Entity`

Quick checks:

```bash
# ACN service
railway logs --service ACN --environment production --lines 300 --filter "Webhook failed OR create_payment_task_failed"

# Backend service
railway logs --service Agentplanet-backend --environment production --lines 300 --filter "Invalid webhook signature OR 422 OR payment-events"
```

## 5) Known Failure Patterns

- `All connection attempts failed` in ACN logs  
  -> `WEBHOOK_URL` is unreachable/misconfigured
- `Invalid webhook signature` in backend logs  
  -> `WEBHOOK_SECRET` and `ACN_WEBHOOK_SECRET` mismatch
- `create_payment_task_failed` in ACN logs  
  -> payment capability or route regression

---

## 6) Settlement Saga (v0.1) — Rollout

Design doc: `acn/docs/_drafts/settlement-saga-design.md`. The saga
moves task completion side effects (escrow release / reward / reputation)
from a synchronous best-effort path into a retried, idempotent
outbox-driven async worker. As of Todo 7 cleanup the saga is the
**sole** settlement path when `OUTBOX_ENQUEUE_REQUIRED=true` (default);
the legacy synchronous path remains in code as an in-place rollback
lever, activated only when the flag is flipped to `false` or when
the PG-mode outbox / unit-of-work deps aren't wired
(Redis-only / test fixtures). The two paths are mutually exclusive
— no double-write window remains.

### 6.1 Required environment variables (ACN service)

| Variable | Default | When to set |
|---|---|---|
| `DATABASE_URL` | — | Required. The saga uses Postgres for `settlement_outbox` + `reputation_events`. Redis-only deployments cannot run the saga. |
| `OUTBOX_ENQUEUE_REQUIRED` | `true` | Keep `true` in production. False allows `complete_task` to succeed even if the outbox INSERT fails (only useful during the very first deploy if you want to canary the producer path). |
| `SETTLEMENT_WORKER_ENABLED` | `false` | Flip to `true` once the migration has run AND you've verified at least one row in `settlement_outbox` via the smoke workflow. |
| `SETTLEMENT_POLL_INTERVAL_SEC` | `1.0` | Worker poll cadence. Raise to `5` if connection pool is constrained. |
| `SETTLEMENT_BATCH_SIZE` | `10` | Rows per claim. Raise if `pending` count grows faster than the worker drains. |
| `SETTLEMENT_MAX_ATTEMPTS` | `12` | Retries before DLQ. Backoff is `min(2 * 2^n, 900)` seconds. 12 attempts ≈ 2h of self-healing. |
| `SETTLEMENT_DLQ_ALERT_WEBHOOK` | unset | Required for production. Slack/PagerDuty incoming-webhook URL. Unset = operators must watch `acn_settlement_outbox_count{state="dead"}` themselves. |
| `SETTLEMENT_RECONCILER_ENABLED` | `true` | Keep on in production — the reconciler is the standing audit between saga `done` events and `reputation_events` rows; a non-zero `acn_settlement_reconcile_delta` is the first signal of saga drift. |
| `SETTLEMENT_RECONCILE_INTERVAL_SEC` | `86400` | 24h window. Don't shorten in production (each run is a real Postgres count); fine to shorten in staging. |

### 6.2 First-deploy checklist

Do these in order. Skipping any step risks splitting the producer
(async outbox) from the consumer (sync legacy) in a way that
silently double-spends.

- [ ] **6.2.1 Migration**: `alembic upgrade head` runs on deploy.
  Verify `settlement_outbox` and `reputation_events` tables exist.
- [ ] **6.2.2 Producer-only smoke** (initial PG-mode rollout only,
  no longer needed on subsequent deploys): set
  `SETTLEMENT_WORKER_ENABLED=false`, `OUTBOX_ENQUEUE_REQUIRED=false`
  so the legacy synchronous path stays active while you confirm the
  outbox migration is sound. After one `complete_task` succeeds,
  flip `OUTBOX_ENQUEUE_REQUIRED=true` and confirm
  `SELECT count(*) FROM settlement_outbox WHERE state='pending';`
  returns ≥ 1 on the next completion. The outbox row is the new
  audit trail.
- [ ] **6.2.3 Worker on**: flip `SETTLEMENT_WORKER_ENABLED=true`,
  redeploy. Within 30s the row should be `state='done'`. From this
  point the saga is the sole settlement path; the legacy inline
  branch only runs if you flip `OUTBOX_ENQUEUE_REQUIRED=false`
  again (emergency disarm).
- [ ] **6.2.4 DLQ webhook**: set `SETTLEMENT_DLQ_ALERT_WEBHOOK` and
  test by injecting a permanent failure in staging (e.g. point
  `BACKEND_URL` at an invalid host). After 12 retries the webhook
  must fire.
- [ ] **6.2.5 Reconciler healthy**: 24h after the first real
  `complete_task` lands, check `acn_settlement_reconcile_delta == 0`.

### 6.3 Prometheus alert rules (paste into your alertmanager)

```yaml
- alert: ACN_SettlementDeadLetter
  expr: acn_settlement_outbox_count{state="dead"} > 0
  for: 1m
  annotations:
    summary: "Settlement saga DLQ has {{ $value }} row(s)"
    runbook: "operations-acn-backend.md §7"

- alert: ACN_SettlementReconcileDelta
  expr: acn_settlement_reconcile_delta != 0
  for: 1h
  annotations:
    summary: "Saga done count diverged from reputation rows by {{ $value }}"
    runbook: "operations-acn-backend.md §7.4"

- alert: ACN_SettlementBacklog
  expr: acn_settlement_outbox_count{state="pending"} > 50
  for: 5m
  annotations:
    summary: "Settlement outbox pending={{ $value }} — worker not draining"
```

`for: 1h` on the reconcile-delta alert intentionally tolerates one
short divergence (legitimate when the worker is mid-saga at the
exact instant of the count query); only persistent drift pages.

### 6.4 Quick health checks

```bash
# Outbox state distribution
psql $DATABASE_URL -c "SELECT state, count(*) FROM settlement_outbox GROUP BY 1;"

# Most recent dead rows (highest-priority triage targets)
psql $DATABASE_URL -c "
  SELECT event_id, task_id, attempts, last_error, updated_at
  FROM settlement_outbox
  WHERE state='dead'
  ORDER BY updated_at DESC
  LIMIT 10;"

# Reconciler delta — should be 0
curl -s https://api.acnlabs.dev/metrics \
  | grep acn_settlement_reconcile_delta
```

---

## 7) Settlement DLQ — Handling Procedure

A row in `state='dead'` means **12 attempts failed**: the saga
worker has exhausted its retry budget and the settlement side
effects (escrow release / reward distribute / reputation write) for
that task have NOT all happened. Some steps may have partially
succeeded — `step_status` on the row tells you which ones. The
inline legacy path is OFF in production, so dead rows represent
genuine settlement debt that requires operator intervention; this
section is about diagnosing the cause and choosing between replay
(fix the upstream condition and let the worker retry) and mark-done
(operator manually completed settlement, mark the outbox row to
satisfy the reconciler).

### 7.1 Triage in under 5 minutes

1. **What failed**: read `last_error` and `attempts` from the dead row.
2. **Which step**: read `step_status` JSONB. The first non-`done`
   step is the failure point. v0.1 steps: `escrow_release`,
   `reward_distribute`, `reputation_write`.
3. **Was the user paid**: cross-reference with backend logs for the
   same `task_id`. With the saga as the sole settlement path, a dead
   row means user-facing side effects did NOT all happen — pick
   either replay (after fixing the upstream condition) or mark-done
   (after manually completing settlement out-of-band) below.

### 7.2 Decision matrix

| step_status[failed_step] | Likely cause | Action |
|---|---|---|
| `escrow_release` fail + escrow status `rejected`/`refunded` | Terminal escrow state, fast-fail path triggered | Investigate why the escrow was rejected (Backend logs). Do NOT replay — the funds are gone. |
| `escrow_release` fail + escrow status `released` | Backend admin or out-of-band release fired between worker tries — worker's read-before-write should have short-circuited but timing window race | Manual `mark_done` (see 7.3); no funds movement needed. |
| `escrow_release` fail + escrow status `locked`/`in_progress` | Backend 5xx or network blip exhausted retries | Investigate Backend. If healthy, manual replay (see 7.3) or one-shot manual release via Backend admin. |
| `reward_distribute` fail | Should not happen in v0.1 (handler is a no-op). | Investigate worker logs; this is a code bug — file an issue. |
| `reputation_write` fail | Postgres outage or `reputation_events` UNIQUE violation | If PG is healthy: manual replay (idempotent on `(agent_id, task_id, kind)`). If unique-violation: row already exists — manual `mark_done`. |

### 7.3 Manual operations

```sql
-- Manual mark_done (use when the side effect was already
-- completed out-of-band by an operator — typically a manual
-- backend release or an admin tool — and the saga retry would
-- otherwise be redundant)
UPDATE settlement_outbox
SET state='done', updated_at=now()
WHERE event_id='<event_id>' AND state='dead';

-- Manual replay (reset to retrying so the worker re-picks it)
UPDATE settlement_outbox
SET state='retrying',
    attempts=0,
    last_error=NULL,
    next_attempt_at=now(),
    updated_at=now()
WHERE event_id='<event_id>' AND state='dead';
```

After either operation, watch the worker's structured log for
`settlement_outbox_event_done` with the same `event_id` to
confirm the new state.

### 7.4 Reconciler delta non-zero

`acn_settlement_reconcile_delta != 0` means the saga done count
and the reputation feedback count diverged over the last 24h
window. Most likely causes (in descending probability):

1. **Manual `mark_done` without writing reputation**: someone fixed
   a DLQ row in 7.3 by marking it done but didn't backfill the
   reputation row. Action: `INSERT` the missing reputation row
   manually (use the `(agent_id, task_id, 'feedback')` from the
   outbox payload).
2. **Worker crashed between `update_step_status('reputation_write', 'done')`
   and `mark_done`**: should be impossible because janitor will
   resurrect the row to `retrying`. If you see this, the janitor
   is broken — investigate `settlement_worker_janitor_failed` logs.
3. **Out-of-band reputation insert** (e.g. someone hand-inserted
   for testing): use `SELECT * FROM reputation_events WHERE
   event_metadata->>'smoke_test'='true';` to find rows that
   should have been smoke-flagged.

### 7.5 Rollback to legacy-only

If the saga is misbehaving and the on-call wants to disable it
**without redeploying**:

```bash
# Stop the worker (settlement halts; flip OUTBOX_ENQUEUE_REQUIRED=false
# in the same command if you also want producers to fall back to the
# legacy synchronous path while you triage)
railway variables set --service ACN \
  SETTLEMENT_WORKER_ENABLED=false \
  SETTLEMENT_RECONCILER_ENABLED=false
railway restart --service ACN
```

After this, `complete_task` still INSERTs to `settlement_outbox`
(so no events are lost) but nothing consumes them. The legacy
synchronous path continues unaffected. Flip back to `true` once
the issue is resolved; the worker will catch up on the backlog.

**Do NOT set `OUTBOX_ENQUEUE_REQUIRED=false` in production** unless
the outbox INSERTs themselves are erroring — that flag lets
`complete_task` silently swallow producer-side failures, which
defeats the audit trail.

