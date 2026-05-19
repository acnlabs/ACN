# Runbook: ADR-0004 subnet join policy

Operational guide for the `join_policy` admission flow shipped across
PRs [#67](https://github.com/acnlabs/ACN/pull/67),
[#72](https://github.com/acnlabs/ACN/pull/72),
[#74](https://github.com/acnlabs/ACN/pull/74),
[#76](https://github.com/acnlabs/ACN/pull/76),
[#77](https://github.com/acnlabs/ACN/pull/77),
[#78](https://github.com/acnlabs/ACN/pull/78),
[#79](https://github.com/acnlabs/ACN/pull/79),
[#80](https://github.com/acnlabs/ACN/pull/80). For design rationale see
[`docs/adr/0004-subnet-join-policy.md`](../adr/0004-subnet-join-policy.md).

## 1) What ships

- `Subnet.join_policy` column on `subnets` (default `'open'`).
- Two new tables: `subnet_join_requests`, `subnet_allowlist`.
- `JoinFlowService` six-branch admission decision tree.
- 14 HTTP endpoints under `/api/v1/subnets/{id}/{allowlist,join-requests,invitations}`.
- 8 new webhook events (`subnet.join_*`, `subnet.invitation_*`) delivered to the subnet's `harness_url`.
- CLI verbs: `acn subnet create --join-policy`, `acn subnet {requests,invitations,allowlist} ...`.

## 2) Deployment

### Order
PRs landed in this order on `main`; **deploy in the same order** if
you're staging a fresh rollout to a fork:

```
#67 → #72 → #74 → #76 → #77 → #78 → #79 → #80
```

Each PR was independently CI-green and the on-disk diff is additive
(no rename, no signature break). The data-layer slices (#74, #76)
run their migrations idempotently — repeating them is a no-op.

### Required env vars

None. The feature is implicit: every existing subnet defaults to
`join_policy='open'` (backward compatible), and there is no new env
flag or feature toggle. `is_private=True` subnets created before this
ADR are read-time coerced to `join_policy='approval'` — no
backfill migration needed.

### PG migration

`subnet_join_requests` + `subnet_allowlist` are created by the
standard Alembic chain that runs on container start. Both have
ADR-0003-style cascade-delete relationships on `subnets.id`. The
migration is idempotent; re-running on an already-migrated DB is a
no-op.

### Redis keys (hot-cache)

```
subnet:{subnet_id}:allowlist          # SET of agent_ids
subnet:{subnet_id}:join_requests      # ZSET of (request_id, created_at)
```

Both are best-effort caches. Repository `is_member` / `pending_for_subnet` falls back to PG on miss; no data loss if Redis is flushed.

## 3) Post-deploy verification

```bash
# 1. Service is up
curl -fsS https://api.acnlabs.dev/health

# 2. New endpoints registered (each should return 401/403/404, NOT 405)
curl -sI https://api.acnlabs.dev/api/v1/subnets/probe-id/allowlist
curl -sI https://api.acnlabs.dev/api/v1/subnets/probe-id/join-requests
curl -sI https://api.acnlabs.dev/api/v1/subnets/probe-id/invitations

# 3. Existing join path is unchanged for `open` subnets
acn subnet join <public-open-subnet-id>   # 200 + joined_directly=true

# 4. CLI verbs wired
acn subnet --help | grep -E "requests|invitations|allowlist"
```

The smoke test script (`scripts/smoke_backend_integration.py`) also
exercises a probe agent through the open-subnet path; no
modifications required.

## 4) Monitoring

### Log keys to watch

All keys verified emitted at the source files listed in the rightmost
column. Greppable in production logs as the literal string.

| `structlog` event | Emitted from | What it means | Severity |
|-------------------|--------------|---------------|----------|
| `join_flow_webhook_delivery_failed` | `acn/services/webhook_join_flow_event_publisher.py` | Webhook `send_to` raised or returned `False`; **state machine still advanced** (per ADR §Cross-slice acceptance) | warn — only alert on sustained rate ≥ N/min |
| `join_flow_webhook_skipped_no_harness` | `acn/services/webhook_join_flow_event_publisher.py` | Subnet has no `harness_url`; webhook intentionally skipped | debug |
| `join_flow_webhook_unmapped_event` | `acn/services/webhook_join_flow_event_publisher.py` | Brand-new `JoinFlowEventType` member not in `_EVENT_MAP` — should be impossible (pinned by test) | **error → page** |
| `subnet_join_request_approved` / `_rejected` / `_withdrawn` | `acn/services/subnet_service.py` | Lifecycle audit trail | info |
| `subnet_invitation_sent` / `_accepted` / `_rejected` / `_canceled` | `acn/services/subnet_service.py` | Lifecycle audit trail | info |

The `visibility_policy_conflict` reason is **not** a structlog event
— it's a string surfaced on the wire inside `ACNHTTPError.details.reason`
for the `is_private=True + join_policy='open'` 4xx rejection at
entity construction (raised from `acn/core/entities/subnet.py`). Find
it in production via HTTP access logs / 4xx dashboards, not a log
key search.

### Prometheus

No new histograms / counters were added in this ADR — webhook
delivery latency is already captured by the existing `webhook_*`
series in `acn/monitoring/`. If `join_flow_webhook_delivery_failed`
fires more than ~1/min sustained, page the operator and inspect the
`harness_url` config for the affected subnets.

## 5) Rollback

The slices are independent and merged in order. Roll back by
reverting individual PRs, **last-merged-first**:

| Slice | Rollback impact |
|-------|-----------------|
| **PR D** (docs) | Documentation only, no behaviour change. |
| **#80** (webhook wiring) | Single-block diff at `acn/api.py:531-553`. Reverting drops back to `NoOpJoinFlowEventPublisher`; admission still works, Harnesses just stop receiving the 8 new events. Existing `agent.joined_subnet` / `agent.left_subnet` webhooks unaffected. |
| **#79** (CLI) | CLI-only; no server impact. Old `acn subnet join` still works against new server. |
| **#78** (HTTP routes) | Removes the 14 new endpoints + reverts canonical join path to ADR-0003 behaviour. **Caveat**: any in-flight `subnet_join_requests` / `subnet_invitations` rows become unreachable via HTTP. Drain them first (`acn subnet requests pending`, then `--reject` or `--approve`) before reverting. |
| **#77** (service layer) | Drops `JoinFlowService` + 10 thin service methods. Requires reverting #78 first (route depends on service). |
| **#76** / **#74** (data layer) | Drops both tables. **Destructive** — back up `subnet_join_requests` + `subnet_allowlist` before rolling back if you care about audit trail. Existing `subnets.join_policy` column can be left in place (it has a default and is harmless). |
| **#67** (`join_policy` column) | Drops the column. Requires reverting all the above first. |

In practice, expected rollback would be #80 only (webhook wiring) if
a Harness integration partner is misbehaving — that single revert
keeps the admission flow but stops sending them events. The full
chain has never had to be reverted; the slices were sized for
independent rollback specifically because the operational cost of
reverting #74's PG migration is high.

## 6) Known gotchas

- **Allowlist add/remove has no webhook**. ADR §"Webhook event catalogue" classifies allowlist mutation as configuration state. If a Harness needs a snapshot, replay via `GET /api/v1/subnets/{id}/allowlist` (owner-only). Don't add webhooks for these without re-reading the ADR's reasoning.
- **`Invited?` is evaluated before `Allow?`**. If both an invitation and an allowlist entry exist for the same agent, the invitation's `accept` is the canonical path; the allowlist entry is "absorbed" as `via=allowlist` on the invitation row, `decided_by=system:allowlist`, and **no separate `allowlist_auto` row is written** (the merge is the test contract). This prevents two membership events for one join. Pinned by `tests/services/test_join_flow_service.py::TestBranch4InvitationWithAllowlistMerge::test_invitation_wins_over_allowlist_auto` (the merge invariant) and `TestBranchOrderNormativity` (general branch precedence).
- **`is_private` is read visibility, `join_policy` is admission**. Don't conflate them. A `is_private=False, join_policy='approval'` subnet (curated public community) is a legitimate configuration. The two axes are intentionally orthogonal; see `acnlabs/ACN#68` for the orthogonal read-side ACL fix.
- **Webhook failures must not roll back state transitions**. This is pinned twice in `tests/services/test_join_flow_webhook_composition.py`. If you're adding a new emit site to `SubnetService` or `JoinFlowService`, the publish call must be the **last** thing the method does after commit, and must not re-raise. Use `WebhookJoinFlowEventPublisher` as the reference.

## 7) Related runbooks

- [`docs/operations-acn-backend.md`](../operations-acn-backend.md) — overall ACN ↔ Backend operations including webhook signature verification.
- [`docs/adr/0003-subnet-nesting-single-layer.md`](../adr/0003-subnet-nesting-single-layer.md) — the cascade-deletion idiom this ADR reuses.
