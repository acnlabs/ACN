# Org Wallet v0

**Status:** Spec v0 — **decisions accepted**; S0–S6 done (2026-07-25)  
**Last updated:** 2026-07-24  
**Audience:** Org governors, Backend wallet owners, ACN Task/escrow

> Extend the existing **human | agent | platform** wallet model with
> **`wallet_type=org`**, so an Org can be a first-class economic subject on
> ACN — same Credits ledger, same escrow path, governance-gated spend.

---

## What this is / is not

| | In scope (v0) | Out of scope |
|---|---|---|
| Ledger | Platform Credits wallet (`WalletType.ORG`) | On-chain Org multisig / ERC-4337 |
| Subject | `user_id = org_id` | Separate “treasury agent” as the money holder |
| Spend auth | Org governance principals | Member agents spending Org funds by default |
| Task money | `creator_type=org` + escrow from Org wallet | Org work tickets carrying reward |
| Kernel | Pointer / lazy create only | Balance stored inside Org Harness tables |

**Not a new Port.** Money stays in Backend wallet + ACN escrow (Network Core
settlement). Org Harness only answers: *who may authorize moves for this Org*.

**Not P2b.** Work Port stays `builtin_work`. Publish/import bridge may *use*
Org wallet when reward/escrow > 0.

---

## Model (same pattern as agent)

### Backend `wallets` row

| Field | Org wallet |
|---|---|
| `wallet_type` | `org` (new enum value) |
| `user_id` | `org_id` (unique, same column convention as agent_id) |
| `owner_id` | Optional cache of current Org owner subject (human user_id or agent_id); updated on Org claim/transfer/release |
| `balance` | Credits (integer; same unit as human/agent) |
| `ap_points` | **Unused** (0); Org is not a reputation agent |
| `spend_autonomy` | Reuse enum; meaning = **delegate spend for non-owner callers** (see below) |

```text
WalletType = human | agent | platform | org
```

### Lifecycle

1. **Lazy create** on first money op (topup / escrow lock / transfer-in), or
   explicit `POST …/org-wallets/{org_id}` by a governor.
2. **No auto-create** on bare `POST /orgs` (Org without budget stays free).
3. **Dissolve Org:** wallet frozen; residual Credits withdrawable only to
   current owner (or `created_by` if `owner.kind=none`); hard-delete deferred.

### Ownership sync

Mirror agent `owner_changed` → wallet `owner_id`:

| Org event | Wallet `owner_id` |
|---|---|
| create (`owner.kind=none`) | `created_by.subject` (steward) |
| claim human/agent | new owner subject |
| transfer | new owner subject |
| release → none | fall back to `created_by.subject` |

---

## Who can move money

Org has **no** org JWT / org API key. Every debit/credit is authorized by a
**principal** that already authenticates (human JWT or agent API key).

### Governance matrix (v0)

| Action | `owner.kind=none` | `owner.kind=human` | `owner.kind=agent` |
|---|---|---|---|
| Read balance / tx | created_by **or** manager | owner **or** manager | owner **or** manager |
| Topup (from principal’s own wallet → Org) | created_by | owner | owner agent |
| Withdraw (Org → owner’s wallet) | created_by | human owner | owner agent |
| Escrow lock / Task create with `creator_type=org` | created_by | owner | owner agent |
| Delegate spend / manager mandate | **deferred** (post-v0) | — | — |
| Manager spend under mandate | **denied in v0** | **denied in v0** | **denied in v0** |
| Ordinary member | **denied** | **denied** | **denied** |

**Invariant:** Membership alone never spends Org funds (same spirit as
“membership ≠ create_work”).

**v0 debit principals only:** `owner` when claimed; else `created_by`.
Managers and `spend_autonomy` delegation are **post-v0**.

### Spend autonomy (reuse — post-v0)

Field exists on the row (default `disabled`) for forward compatibility.
**v0 ignores mandate paths** — only owner / created_by may debit.
Later:

| Value | Meaning for Org |
|---|---|
| `disabled` (default) | Only owner / created_by may debit |
| `limited` | Managers may debit within per-tx / window / reserve floor |
| `unlimited` | Managers may debit freely (still not ordinary members) |

Owner/created_by debits **bypass** the mandate (they are the grantor).

---

## ACN Task / escrow hook

### Creator subject

Extend Task (and escrow provider) creator:

```text
creator_type ∈ { human, agent, org }
creator_id   = user_id | agent_id | org_id
```

When `creator_type=org`:

1. Caller must pass Org governance check (table above).
2. Escrow lock / budget check hits **Org wallet**, not the caller’s personal wallet.
3. Refunds on cancel return to **Org wallet**.
4. Reward release still pays **assignee** (usually agent wallet); Org is payer, not payee.

### Bridge (`publish-task`) alignment

Today publish uses the **caller agent** as Task creator and optional
`metadata.org_id`. v0 Org Wallet adds an explicit mode:

| Mode | Behavior |
|---|---|
| **Legacy** (default) | `creator_type=agent`, agent pays; `metadata.org_id` attribution only |
| **Org-paid** | `creator_type=org`, `creator_id=org_id`; require governance + Org balance; keep `metadata.org_id` / `org_publish` |

CLI sketch:

```bash
acn org publish-task --org org_… -t "…" -d "…" --tags review \
  --pay-from org --reward 100 --escrow
```

Without `--pay-from org`, behavior unchanged (agent-paid attribution).

### Import

Import Task → Org work stays **money-neutral**. Import does not move Credits.

---

## API surface (sketch)

### Backend (ledger)

```http
GET    /api/org-wallets/{org_id}
POST   /api/org-wallets/{org_id}              # explicit create
GET    /api/org-wallets/{org_id}/transactions
POST   /api/org-wallets/{org_id}/topup          # JWT human treasury
POST   /api/org-wallets/{org_id}/topup-internal # internal (agent treasury)
POST   /api/org-wallets/{org_id}/withdraw
POST   /api/org-wallets/{org_id}/withdraw-internal
POST   /api/org-wallets/{org_id}/check
POST   /api/org-wallets/{org_id}/spend          # internal non-escrow debits
```

`spend-mandate` — **post-v0** (D3).

Escrow lock/release/refund: existing Labs escrow v2 with `creator_type=org`
(no separate Org WalletClient in v0 — **D**).

### ACN (authorization + Task)

- Org service: `assert_treasury_principal` (= governance; no managers in v0).
- **Org-paid only via** `POST /orgs/{org_id}/publish-task` with `pay_from_org=true`
  (**B**). Generic task create must not honor `creator_type=org`.
- Guard **C**: Org-paid forces `credits` + escrow when reward > 0.

### Org Harness Kernel

No balance column on `orgs`. Read-only proxy (**S6**):

```http
GET /api/v1/orgs/{org_id}/wallet   # treasury-gated; Backend summary (exists/balance/status)
```

Requires ACN `BACKEND_URL` + `INTERNAL_API_TOKEN`. Missing wallet →
`exists=false`, `balance=0` (not 404).

---

## Events / audit

| Event | When |
|---|---|
| `org.wallet_created` | First wallet row |
| `org.wallet_topup` / `withdraw` | Human-visible moves |
| `org.wallet_escrow_lock` / `refund` | Task money path |
| Existing ledger `transactions` | Source of truth for amounts |

Emit via existing outbox / webhook style used for agent wallet where possible.

---

## Rollout slices

| Slice | Deliverable | Depends | Status |
|---|---|---|---|
| **S0** | This spec accepted | — | done |
| **S1** | Backend `WalletType.ORG` + CRUD/topup/withdraw + tests | Backend | done |
| **S2** | `POST /orgs/{id}/publish-task` (`pay_from_org`) + escrow org lazy-create (**A/B/C**) | S1 | done |
| **S3** | CLI `--pay-from org` | S2 | done |
| **S4** | Paperclip Issue ACN tab **Pay from Org wallet** (thin; fund via Backend) | S3 | done — `@acnlabs/paperclip-plugin-acn@0.3.2` |
| **S4b** | Paperclip inbound without public URL (poll fallback) | S4 | done — `@acnlabs/paperclip-plugin-acn@0.3.3` |
| **S5** | Ownership sync (`owner_id` on claim/transfer/release) + dissolve freeze | S1 | done — CN soft-val 2026-07-24 |
| **S6** | `GET /orgs/{id}/wallet` proxy + Paperclip balance display | S5 | done — `@acnlabs/paperclip-plugin-acn@0.3.4` |
| **S6b** | Paperclip / ACN topup UX (optional; external fund still OK) | S6 | next |

Soft-validate on a Paperclip instance:
[quickstart-org-paperclip.md § Org-paid](./quickstart-org-paperclip.md#org-paid-soft-validate)
(plugin ≥ **0.3.3**; local inbound via poll).
Live API smoke (no UI):
[`scripts/smoke_org_wallet.sh`](../../scripts/smoke_org_wallet.sh) (Org-paid publish/refund);
[`scripts/smoke_org_wallet_s5.sh`](../../scripts/smoke_org_wallet_s5.sh) (claim/transfer/release/dissolve → Backend wallet).

**v0 non-goals (later):** on-chain Org address, member payroll splits, budget
policies soft-warn/hard-stop (Pasture draft), multi-currency; one-click tunnel.

---

## Decisions (accepted 2026-07-24)

| # | Decision | Choice |
|---|---|---|
| D1 | Wallet create | **Lazy** on first money op; optional explicit `POST` |
| D2 | Default `spend_autonomy` | **`disabled`** |
| D3 | Manager debit in v0 | **No** — owner / created_by only; mandate later |
| D4 | Legacy `publish-task` | **Agent-paid by default**; `--pay-from org` opt-in |
| D5 | Withdraw destination | **Only** current owner wallet (`created_by` if `owner.kind=none`) |

### Implementation guards (accepted 2026-07-24)

| # | Guard | Choice |
|---|---|---|
| **A** | Escrow lock + missing Org wallet | **`_get_or_create` for `creator_type=org`** (empty wallet → insufficient balance, not “not found”) |
| **B** | Who may set `creator_type=org` | **Only** Org-scoped publish API after treasury governance — **not** generic `POST /tasks` / `X-Creator-Type` |
| **C** | Currency on Org-paid publish | Force **`credits`**; if `reward > 0` force **`use_escrow=true`** |
| **D** | WalletClient org wrappers | **Skip in v0** — EscrowProvider is enough |

---

## Related

- Wallet model: `backend/app/models.py` (`WalletType`, `SpendAutonomy`)
- Org ownership: [ADR-0014](../adr/0014-org-harness-module.md)
- Publish/import: [org-task-bridge-v0.md](./org-task-bridge-v0.md)
- Boundary: [design-v0.md](./design-v0.md) §7 (settlement stays Network Core / Backend)
