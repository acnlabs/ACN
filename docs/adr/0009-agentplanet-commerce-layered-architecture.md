# ADR-0009: AgentPlanet Commerce — Layered Architecture (Ledger vs Protocol/Event vs Reliability vs Rail)

**Status:** Accepted — P0 implemented, P1/P2 phased
**Date:** 2026-05-31
**Deciders:** AgentPlanet platform owner + ACN core team + backend
**Related:** ADR-0007 (Unified Agent Identity), ADR-0008 (Unified API Audience); ACN AP2 (`acn/acn/protocols/ap2/`); backend store (`backend/app/services/store_service.py`); AgentMother seller integration

> **Decision:** AgentPlanet commerce is split into four layers, each owned by the
> system best suited to it:
> 1. **Ledger / source-of-truth** = backend store + wallet (the *only* place
>    money/order state is authoritative).
> 2. **Reliability** = reconciliation (seller-pollable fulfillment queue +
>    idempotent fulfill). Money correctness **never** depends on a push.
> 3. **Protocol / event / discovery** = ACN + AP2 (capability discovery,
>    standardized signed+retried webhooks, agent↔agent semantics).
> 4. **Payment rail / authorization** = Alipay AI Pay (MCP), PayPal, credits —
>    how a buyer (human *or* agent) actually pays.
>
> Hard constraint: **single ledger.** AP2 carries *events*, not a second copy of
> the money truth.

---

## Context

The store was initially reasoned about as a human→agent shop. Two facts widen
that framing:

1. **The store is not limited to human↔agent.** A quote (`create_quote`) is
   raised by a seller *agent*; the buyer can be a human **or** another agent
   (`pay_order` accepts `buyer_type ∈ {user, agent}`). The store is already an
   agent-commerce marketplace, not just a human storefront.
2. **Payment rails are going agentic.** Alipay launched (2026-05) a full-stack
   AI-native payment suite — **AI付 / AI收 / Token Pay / AI钱包** — integrated via
   an **MCP Server** (`@alipay/mcp-server-alipay`), with **A2A (agent↔agent)** and
   **A2M** payment frameworks (ACT 2.0) and scoped/autonomous authorization
   tiers. Ant's stated position: agent↔agent transactions are becoming a
   mainstream payment form.

### The reliability gap in the current code

`pay_order` (`store_service.py`) settles credits in a single DB transaction
(debit buyer, credit seller, mark `state="fulfilling"`), then calls
`_notify_seller_paid` — a **fire-and-forget** POST to ACN
`/api/v1/communication/internal/send`. That is ACN's *weakest* channel: one
shot, no retry, no delivery history. If it fails, the money is already moved but
the seller agent **never learns the order is paid**; the order sits in
`fulfilling` forever and the buyer gets nothing.

Meanwhile ACN already ships a **strong** channel that the store does not use:

- `acn/acn/protocols/ap2/webhook.py` — `WebhookService`: HMAC-signed delivery,
  **automatic retries with exponential backoff**, delivery history, manual
  retry.
- `acn/acn/protocols/ap2/core.py` — `PaymentTaskManager` / `PaymentTask` with
  explicit `buyer_agent` / `seller_agent` and a payment lifecycle.

So today the store moves money on its strongest guarantee (DB transaction) but
notifies on ACN's weakest one (fire-and-forget), with nothing in between to
recover a lost notification.

### Layers people conflate

| Layer | Question it answers | Owner | Reliability property |
|---|---|---|---|
| **Ledger / truth** | "what is owed / paid / fulfilled?" | backend store + wallet | ACID, authoritative |
| **Reliability** | "how does the seller never miss a paid order?" | backend + seller | pull/reconcile, push-independent |
| **Protocol / event** | "how do agents discover & get notified, uniformly?" | ACN + AP2 | signed, retried, audited |
| **Payment rail** | "how does the buyer actually pay?" | Alipay AI Pay / PayPal / credits | external, rail-specific |

The bug above comes from collapsing *reliability* into *protocol* (trusting a
single push for money-critical state).

---

## Decision

1. **Backend store + wallet is the single source of truth.** Order state and
   settlement (credits, refunds, Alipay/PayPal top-ups) live in the backend
   ledger and nowhere else. This is **rail-agnostic** and **buyer-agnostic**
   (human or agent, any rail). Settlement truth is never duplicated into ACN.

2. **Reliability comes from reconciliation, not push (P0).** The seller can
   always recover paid-but-unfulfilled orders by **polling** a seller-scoped
   fulfillment queue; `fulfill` is idempotent. A lost notification degrades
   latency, never correctness. This is the unconditional floor and is built
   first.

3. **ACN/AP2 is the agent-commerce protocol/event layer (P1).** Because the
   store is already agent↔agent, the store's notification path is upgraded from
   the fire-and-forget internal-send to ACN's `WebhookService` (signed + retried
   + audited), and agent↔agent store orders are represented in AP2 terms
   (`buyer_agent`/`seller_agent`) for discovery and a uniform integration shape.
   AP2 earns its place here — **as events and discovery, not as a ledger.**

4. **Payment rails plug into the buyer side (P2).** Alipay AI Pay (via its MCP
   server) and ACT 2.0 A2A/A2M let an agent buyer pay autonomously into the
   store; PayPal/Stripe/credits remain for humans. The rail is swappable and
   sits *below* the ledger — a successful rail charge results in a ledger
   settlement, which is still the truth.

5. **Single-ledger constraint is explicit.** AP2's `PaymentTask` may *mirror* an
   order for discovery/standardized events, but the backend wallet remains the
   sole settlement record. No double bookkeeping of money.

### Why not move settlement into ACN/AP2

AP2's `PaymentTaskManager` is Redis-backed task tracking + discovery + webhook —
not an ACID ledger with refunds/escrow. Moving money truth there would (a)
create two ledgers to reconcile, (b) couple settlement correctness to ACN
availability, and (c) duplicate the wallet that already exists. ACN is the
*network*; the backend is *this platform's books*.

---

## Considered Options

| | A: layered — ledger=backend, event=ACN/AP2, reliability=reconcile (chosen) | B: store keeps bespoke push only | C: move settlement into AP2 |
|---|---|---|---|
| Survives a lost notification | ✅ (poll/reconcile) | ❌ stuck `fulfilling` | ✅ but new ledger |
| Single money truth | ✅ backend | ✅ backend | ❌ two ledgers |
| Uniform for agent↔agent sellers | ✅ AP2 events | ❌ one-off per integration | ✅ |
| Couples settlement to ACN uptime | ❌ no | ❌ no | ✅ yes (bad) |
| Reuses ACN's signed+retried delivery | ✅ (P1) | ❌ | ✅ |

**B** rejected: it is the current bug — money-critical state on a best-effort
push. **C** rejected: violates single-ledger and couples settlement to the
network. **A** keeps each guarantee where it is strongest.

---

## Consequences

### Positive
- A lost seller notification can no longer strand a paid order (P0).
- Store joins the agent-commerce event bus; any future seller agent integrates
  the same standardized, signed, retried way (P1).
- Buyer side can adopt agentic rails (Alipay AI Pay) without touching the ledger
  (P2).

### Negative / costs
- P1 requires wiring the store into ACN `WebhookService` and an AP2 mapping —
  more moving parts than a single POST.
- P2 (Alipay AI Pay / ACT 2.0) is a real product/integration investment
  (restricted keys, sandbox, authorization-tier UX in an "AI wallet" sense).

### Neutral
- P0 is purely additive (a read endpoint + idempotent fulfill already exists);
  no change to settlement logic.

---

## Phased Plan

- **P0 — reconciliation floor (this ADR's implementation):** backend
  `GET /api/store/orders/fulfillment-queue` (seller-scoped: orders in
  `paid`/`fulfilling` for the calling seller agent, with the same fields the
  `store.order_paid` event carries — incl. private `metadata`). Seller polls +
  idempotent `fulfill`. Push stays as a latency optimization.
- **P1 — ACN/AP2 event layer (chosen: P1-A, route through AP2):** on store
  settlement, drive an AP2 `PaymentTask` (`payment_method=platform_credits`,
  `currency=credits`) so ACN fires `payment_task.payment_confirmed` to the
  **seller's registered webhook** (signed + retried), replacing the
  fire-and-forget internal-send. Backend ledger unchanged (the task is an
  event-mirror, not a second ledger).
- **P2 — agentic rails:** integrate Alipay AI Pay (MCP) + ACT 2.0 A2A/A2M for
  autonomous agent buyers; keep PayPal/Stripe/credits for humans. Rail charge →
  ledger settlement (truth stays in backend).

> Blast radius: P0 backend-only (additive). P1 backend + ACN. P2 backend + ACN +
> Alipay open-platform config.

### P1-A — chosen path and findings (2026-05-31 code audit)

Routing store events through ACN's AP2 layer (vs a bespoke backend webhook) was
chosen for architectural coherence: AP2 already models `buyer_agent`/
`seller_agent`, already has per-agent webhook registration, and gives discovery
for free. Audit findings that scope the work:

1. **`platform_credits` already exists.** AP2 ships
   `SupportedPaymentMethod.PLATFORM_CREDITS`, a `credits` currency, and
   `CREDITS_PER_USD = 100` (the backend's exact convention). Store settlement
   maps onto AP2 with **no new payment method** — clean fit.
2. **Per-seller webhook delivery is NOT wired (the real gap).** Sellers can
   register `webhook_url` on their payment capability
   (`POST /payments/{agent_id}/payment-capability`), but **nothing consumes it**:
   `PaymentTaskManager._send_webhook` calls `WebhookService.send_event`, which
   delivers only to the **platform-wide `default_config` URL** — never to the
   seller's registered `webhook_url` via `send_to`. P1-A's core ACN change is to
   deliver seller-facing task events to the seller's own `webhook_url`.
3. **No per-agent webhook secret today.** The capability stores `webhook_url`
   but no HMAC secret; one must be added (generated at registration, shown once)
   so `send_to` can sign per seller.
4. **Retries are in-process, not a durable outbox.** `WebhookService` retries
   3× (~15s, exponential) in-process and persists delivery records to Redis as
   *history*, not as a work queue. A process crash mid-retry loses the push.
   Therefore **P0 reconciliation remains the correctness guarantee**; P1-A
   webhook is a latency optimization. True at-least-once needs a durable outbox
   worker — tracked as a separate ACN hardening item (does not block P1-A).

**Human buyers:** AgentMother's real orders are mostly human→agent (a human pays
the checkout link). AP2 needs a `buyer_agent`, so human-buyer settlements mirror
with a **system pseudo buyer-agent** (e.g. `system:agentplanet-backend`), the
real human `buyer_id` carried in metadata. This means P1-A fires the seller
webhook for **all** paid orders, not only agent↔agent — refining C1 below.

---

## Open Sub-Decisions

| # | Decision | Default / status |
|---|---|---|
| C1 | Does AP2 `PaymentTask` mirror *all* store orders or only agent↔agent ones | **Revised: mirror all paid orders** — human buyers use a system pseudo buyer-agent, so the seller webhook fires for human→agent orders too (AgentMother's main case) |
| C2 | Polling cadence / queue pagination for P0 | Default: seller polls on its own cadence; queue returns oldest-first, capped (e.g. 50) |
| C3 | Keep the fire-and-forget push after P1 webhook lands | Keep as best-effort low-latency hint; webhook + reconcile are the guarantees |
| C5 | P1-A delivery shape: full AP2 `PaymentTask` (created+confirmed) vs lightweight "notify seller webhook" call | **Open** — full task gives discovery/history + buyer/seller model but more surface; lightweight only fires `send_to`. Lean: full task (the P1-A intent) |
| C6 | Per-agent webhook secret model | **Open** — generate at capability registration, return once, store hashed; used by `send_to` for `X-ACN-Signature` |
| C7 | Durable webhook outbox in ACN `WebhookService` | **Deferred follow-up** — in-process retries today; P0 poll is the guarantee until an outbox worker lands |
| C4 | Alipay AI Pay authorization tier for agent buyers | Deferred to P2 design (supervised vs scoped vs autonomous) |
