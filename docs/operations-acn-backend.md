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
| `ACN_URL` | `https://acn-production-9ae5.up.railway.app` | ACN base URL used by backend |
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

