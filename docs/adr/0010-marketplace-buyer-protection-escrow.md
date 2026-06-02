# ADR-0010: Marketplace Buyer Protection — Acute-Window Escrow

**Status:** Accepted — **P0 and P1 fully implemented** (backend PRs #24, #25, #26). Universal escrow hold: all orders, all sellers, no instant-settlement tier. The graduated staking/deposit/reputation layer (originally split into ADR-0011) is **superseded** — it was only needed to safely allow sellers to skip escrow, a tier that no longer exists.
**Date:** 2026-06-01
**Deciders:** AgentPlanet platform owner + ACN core team + backend
**Related:** ADR-0009 (Commerce Layered Architecture — single ledger, reconciliation, AP2 events), ~~ADR-0011~~ (Superseded — seller capital layer no longer needed given universal hold), ADR-0007 (Unified Agent Identity); backend PRs #24 #25 #26 (`store_service.py`, `main.py`, `SKILL.md v1.5.0`)

> **Decision:** Buyer protection is provided **per order**, only for the **acute
> "paid → fulfilled-and-accepted" window**. **Universal escrow — all sellers,
> no exceptions, no instant-settlement tier:**
> - The buyer's payment is **held in escrow** at pay time; it releases to the
>   seller only on **buyer acceptance or an acceptance-window timeout (72 h
>   default)**, **never** on the seller's self-attested `fulfill`.
> - Abandonment / refund before release ⇒ refund from hold (seller was never
>   credited, so no seller balance is drawn).
> - Contested ⇒ arbitration ruling (Future Work; see ADR-0011).
>
> **Post-fulfillment prorated refunds are out of scope** — for providers that
> refund (Tencent/Aliyun/Huawei) they are self-funded by the upstream refund the
> seller received; for no-refund rails (AWS pay-as-you-go) the case largely does
> not arise.
>
> Hard constraints (from ADR-0009): **single ledger**; the escrow hold is a
> wallet-backed sub-account in the backend, never a second money truth.

---

## Context

### Why buyer protection is foundational

AgentMother is a **paid service provider** — it builds & deploys agents *for
paying customers*; it is itself a (first-party) seller, **not a charity
and not an underwriter** of the agents it creates. Whether a created agent then
opens its own shop is **that agent's and its owner's business** — an independent,
**owner-capitalized** economic actor. So the seller population is a long tail of
**independent agents, untrusted by the platform/buyers** — *not* a captive fleet
the platform must bootstrap or that AgentMother backstops. Buyer protection is
still foundational: the marketplace collapses the first time one of these
independent sellers takes payment and disappears.

Buyer protection is therefore **foundational infrastructure**, built as a
prerequisite for opening third-party seller onboarding, **not triggered by the
first bad actor**.

### The gap in current code (at design time)

- **Instant settlement, unconditionally.** `pay_order` debited the buyer and
  credited the seller's wallet in one transaction; the seller could spend
  immediately. (ADR-0009 assumed this for a single trusted seller; this ADR
  replaced it with universal escrow.)
- **Refund had no collateral behind it.** `refund_order` drew from the seller
  wallet; if the seller had spent the proceeds it failed with
  `409 "Seller cannot cover refund"`. A vanished seller left the buyer with no
  recourse.

### The fundamental trade (state it honestly)

Cloud-resale sellers must spend **real money up front** to fulfill. Protecting the
buyer for the provisioning window means **someone's capital is tied up** during
it:

| Mechanism | Whose capital | Cashflow effect on seller | Buyer protection |
|---|---|---|---|
| **Escrow the buyer's payment** ← chosen | the buyer's (already paid) | seller fronts provisioning itself | full, for that order, zero seller collateral |
| Seller deposit (collateral) | the seller's own | seller locks ≈ exposure *plus* funds provisioning | full, bounded by deposit |
| Instant settlement (old behavior) | none held | best (seller has the cash) | none |

There is no option that protects the buyer *and* costs the seller no capital
during provisioning. Universal escrow places the burden entirely on the buyer's
own already-paid funds — the seller never received the money to spend, so no
additional collateral is required. Credits are closed-loop (no withdrawal), so
the 72-hour delay only affects when credits enter the seller's wallet for
platform-internal use. There is no cashflow reason for sellers to prefer instant
settlement.

### Reusable in-house pattern

**Escrow:** `escrow_service.py` (`escrows` table, `lock()` → `balance ↓` +
`TASK_ESCROW`, `EscrowRelease`, status lifecycle, ruling callbacks) demonstrates
the **hold → release/refund pattern**, but the concrete service is **task-coupled**
(reviews, assignees, release-pools, per-amount release conditions) and is **not
cleanly reusable** for a plain store-payment hold. P1 therefore adds a **lightweight
store-payment hold** (buyer debited at pay time; the amount parked as a recorded
liability — held order sub-state, settled to the seller only at release) reusing
the *pattern*, not the task-escrow tables. The **arbitration** path (the
`ReviewService` jury + Escrow ruling) remains a candidate for resolving contested
disputes; its fee model and store-dispute wiring are deferred to a future ADR
(see Future Work in ~~ADR-0011~~).

### Interaction with ADR-0009 (what P1 must reconcile)

ADR-0009 settled in one transaction (**credit seller wallet + notify seller**) at
pay time. Escrow-hold breaks that coupling, so P1 must:

- **Split "notify the seller to fulfill" from "settle to the seller wallet."** A
  held order still notifies the seller at *pay* time (otherwise nobody fulfills),
  but the wallet credit happens only at **release** (acceptance window end). The
  AP2/`store.order_paid` notification fires on escrow-lock; ledger settlement to
  the seller fires on release.
- **Make the *already-shipped* `refund_order` escrow-aware.** It previously debited
  the **seller balance**; a held order's seller was never credited, so a refund
  must come from the **hold**, not the seller wallet (else it wrongly `409`s on
  funds sitting in escrow). ⇒ P1 is **not purely additive**: it modifies the P0
  refund path. The fulfillment-queue (P0 reconciliation) must also surface held
  orders.

---

## Decision

1. **Protect per order, for the acute paid→fulfilled-and-accepted window only.**
   Buyer protection covers "seller took payment but did not deliver."
   Post-fulfillment prorated refunds are out of scope (self-funded by the cloud
   refund where applicable); the existing `refund_order` handles them best-effort
   against seller balance.

2. **Universal escrow — all sellers, no instant-settlement tier.** Every
   `agent_service` order routes through escrow hold regardless of seller identity
   or history. No trust classifier is needed; no D8 allowlist exists. This
   eliminates the bust-out attack surface entirely (there is no "proven tier" to
   graduate into), removes the need for the ADR-0011 capital subsystem, and
   eliminates money-flow complexity from credits being closed-loop.

3. **Escrow release / refund trigger.** Seller `fulfill` does **not** release
   funds by itself — it **starts a buyer-acceptance window (D13)**. Release =
   **buyer confirms** ∨ **window elapses with no dispute**. Buyer refund before
   release ⇒ refund from hold (seller never credited; always covered). **Contested**
   ⇒ arbitration ruling (Future Work).
   Rationale: `fulfill` is seller-self-reported free-text, so releasing on it
   would let a lying seller drain the escrow with a fake fulfillment.

4. **Disputes go to the arbitration network**, not a manual admin button (ruling
   → release vs refund). Dispute-routing detail and juror fee model are **Future
   Work**, designed when the first real disputed order occurs (see ~~ADR-0011~~).

5. **Single ledger preserved.** The escrow hold is a backend wallet sub-account;
   not mirrored as money truth in ACN/AP2 (ADR-0009).

### Threat model — "bust-out" (fully neutralised)

The classic attack — *farm reputation with small orders, graduate, then take one
large payment and vanish* — requires a "large payment to abscond with." Under
universal hold, funds are **never released to the seller until the buyer accepts or
the window times out**. The money never arrives before the obligation is checked,
so there is nothing to abscond with. The attack is structurally impossible.

### Buyer-side fraud (friendly fraud)

Escrow + buyer-dispute lets a malicious **buyer** take delivery (real cloud value)
then refuse to confirm / dispute → refund, leaving a seller out the cloud cost.
Mitigations: contested cases go to **arbitration** (not auto-refund); buyers
accrue a dispute reputation score (confirm-then-dispute pattern down-weighted/
flagged). Full buyer-abuse governance is **Future Work** (trigger: first pattern
of suspected buyer abuse).

---

## Considered Options

| | A: progressive trust — escrow(unproven)/instant(proven) | B: universal seller deposit | **C: universal escrow (chosen)** | D: platform underwrites (negative balance) | E: status quo (instant + 409 refund) |
|---|---|---|---|---|---|
| Protects buyer from untrusted seller | ✅ (escrow) | ✅ | ✅ | ⚠️ platform bears default | ❌ |
| Proven seller keeps cashflow | ✅ instant | ⚠️ locks capital | ⚠️ 72h delay (credits only, closed-loop) | ✅ | ✅ |
| Capital burden falls fairly | ✅ on unproven only | ❌ on all sellers | ✅ none (buyer's own payment) | ❌ on platform | n/a |
| Bounds platform risk | ✅ | ✅ | ✅ | ❌ unbounded | ✅ (no protection) |
| Eliminates bust-out | ❌ only unproven | ✅ | ✅ | ❌ | ❌ |
| Needs trust classifier | ✅ needed | ❌ | ❌ | ❌ | ❌ |
| Build cost (P1) | medium (classifier + two paths) | medium | **low** (single path) | low | none |

**A** rejected: requires a trust classifier and a "proven" tier that can still bust
out post-graduation; needs ADR-0011 capital subsystem to be safe.
**B** rejected: over-charges thin-margin sellers; capital locked even when order
succeeds.
**D** rejected as primary: unbounded platform liability.
**E** is the current bug for untrusted sellers.
**C** is the simplest sufficient mechanism — one code path, full coverage,
zero additional collateral, bust-out structurally impossible.

---

## Consequences

### Positive
- Buyers are protected for **every** order from **day one of third-party
  onboarding**, with no per-seller classification logic.
- No trust classifier, no D8 allowlist, no capital mechanics — the escrow path is
  the only path.
- Bust-out attack is structurally impossible (no instant-settlement tier to
  graduate into).
- No ADR-0011 capital layer to build: universal hold made it unnecessary.

### Negative / costs
- Adds an **escrow-hold order sub-state** to `pay_order`/`fulfill` and makes
  `refund_order` escrow-aware (settlement-adjacent → transaction-safe + tested
  like wallet paths). **Not purely additive** (see Interaction with ADR-0009).
- All sellers (including first-party) wait up to 72 hours for credits to settle.
  Accepted: credits are closed-loop, so the delay has no real-money cashflow
  impact.
- **Depends on a neutral arbitration mechanism** for contested holds (Future Work;
  trigger: first real disputed order).

### Neutral
- The shipped refund API surface is unchanged for callers; for held orders the
  funding source is always the hold (always covered for the acute window).
- **The original "seller won't top up" gap is fully closed** for the acute window.
  Post-fulfillment prorated refunds remain best-effort against seller balance
  (the P0 `409`); this is an accepted scope limit, not a regression.

---

## Phased Plan

- **P0 — done:** refund API (`POST /orders/{id}/refund`) — full/partial refund
  from seller balance, terminal `refunded`, blocks re-fulfill; `409` if seller
  can't cover.
- **P1 — done:** universal acute-window escrow for **all** sellers. `pay_order`
  debits the buyer and parks the full amount as a hold (seller not credited);
  `fulfill` opens a 72-hour buyer-acceptance window; release on buyer confirm
  (`POST /orders/{id}/accept`) or window timeout (background
  `release_expired_holds`); refund before release draws from hold, never from
  seller balance.
- **Future Work (not scheduled):** dispute arbitration wiring; buyer-side fraud
  detection. Both designed as new standalone ADRs at their respective trigger events.

> Blast radius: P0/P1 backend-only.

---

## Open Sub-Decisions (this ADR's scope)

| # | Decision | Status |
|---|---|---|
| D2 | Acute window = escrow-hold scope | **Decided:** hold the full order amount; release after the buyer-acceptance window (D13), *not* on the seller's `completed` flag; refundable to buyer until release. All-or-nothing release (partial fulfillment out of scope). |
| D3 | Escrow release / refund trigger | **Decided:** release = buyer confirm ∨ window timeout; **never** seller self-attested `fulfill`; contested ⇒ arbitration (Future Work). |
| D4 | Abandonment / auto-release | **Decided:** fulfillment-SLA timeout ⇒ buyer may reclaim from hold; post-fulfill acceptance-window timeout ⇒ hold auto-releases to seller. Background task `release_expired_holds` runs on a 5-minute loop. |
| D13 | Acceptance-window length & buyer-no-response default | **Decided:** 72-hour post-fulfill acceptance window (configurable per product class). Buyer neither confirms nor disputes within 72h ⇒ hold auto-releases to seller (an absent buyer cannot freeze seller funds indefinitely). |
| D14 | Buyer-side fraud (friendly fraud) | **Acknowledged:** contested ⇒ arbitration (not auto-refund); buyers accumulate dispute reputation. Full governance → Future Work (new ADR at trigger event). |

> ~~D1 (trust classifier) and D8 (trusted-seller D8 allowlist) are withdrawn~~
> — universal escrow requires no classifier and no per-seller instant-settlement
> flag.
>
> Deposit sizing, forfeit semantics, `ap_points` role, reviewer-stake migration,
> rolling reserve, and the arbitration fee model were **ADR-0011** sub-decisions;
> ADR-0011 is now superseded.
