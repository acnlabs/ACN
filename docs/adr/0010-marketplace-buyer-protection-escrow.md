# ADR-0010: Marketplace Buyer Protection — Acute-Window Escrow

**Status:** Accepted (direction) — P0 done; **P1 trigger-gated on the first untrusted seller onboarding** (until then AgentMother stays on the trusted-instant path, D8). Release *mechanism* decided (D3); window length (D13) set during P1 build. The graduated staking/deposit/reputation layer is split into **ADR-0011**.
**Date:** 2026-06-01
**Deciders:** AgentPlanet platform owner + ACN core team + backend
**Related:** ADR-0009 (Commerce Layered Architecture — single ledger, reconciliation, AP2 events; this ADR *refines* its instant-settlement assumption), **ADR-0011** (Seller Staking / Deposit / Graduated Reputation — the capital-backed refinement of this ADR's "proven seller" tier), ADR-0007 (Unified Agent Identity); backend store (`backend/app/services/store_service.py`), refund API (`POST /api/store/orders/{order_id}/refund`), escrow (`backend/app/services/escrow_service.py`)

> **Decision:** Buyer protection is provided **per order**, only for the **acute
> "paid → fulfilled-and-accepted" window**. This ADR uses **two tiers**:
> - **Unproven seller →** the buyer's payment is **held in escrow**; it releases
>   to the seller only on **buyer acceptance or an acceptance-window timeout**
>   (D3/D13), **never** on the seller's self-attested `fulfill`. Abandonment /
>   dispute refunds the buyer from the hold.
> - **Proven / trusted seller →** **instant settlement** (today's behavior).
>
> The *graduated* trust model that refines "proven" into a continuous,
> capital-backed privilege (earned exposure limit, opt-in deposit, rolling
> reserve, anomaly step-up) lives in **ADR-0011**.
>
> **Post-fulfillment prorated refunds are out of scope** — for providers that
> refund (Tencent/Aliyun/Huawei) they are self-funded by the upstream refund the
> seller received; for no-refund rails (AWS pay-as-you-go) the case largely does
> not arise. Disputes are settled by the **existing arbitration network** (detail
> + fee model in ADR-0011), not a manual admin path.
>
> Hard constraints (from ADR-0009): **single ledger**; the escrow hold is a
> wallet-backed sub-account in the backend, never a second money truth.

---

## Context

### Why buyer protection is foundational

AgentMother is a **paid service provider** — it builds & deploys agents *for
paying customers*; it is itself a (first-party, trusted) seller, **not a charity
and not an underwriter** of the agents it creates. Whether a created agent then
opens its own shop is **that agent's and its owner's business** — an independent,
**owner-capitalized** economic actor. So the seller population is a long tail of
**independent agents, untrusted by the platform/buyers** — *not* a captive fleet
the platform must bootstrap or that AgentMother backstops. Buyer protection is
still foundational: the marketplace collapses the first time one of these
independent sellers takes payment and disappears.

### The gap in current code

- **Instant settlement, unconditionally.** `pay_order` debits the buyer and
  credits the seller's wallet in one transaction (`state="fulfilling"`); the
  seller can spend immediately. (ADR-0009 assumed this for a single trusted
  seller — this ADR refines it: instant settlement becomes a *trust-tier
  privilege*.)
- **Refund has no collateral behind it.** `refund_order` draws from the seller
  wallet; if the seller spent/cashed-out the proceeds it fails with
  `409 "Seller cannot cover refund"`. A vanished seller leaves the buyer with no
  recourse.

### The fundamental trade (state it honestly)

Cloud-resale sellers must spend **real money up front** to fulfill. Protecting the
buyer for the provisioning window means **someone's capital is tied up** during
it:

| Mechanism | Whose capital | Cashflow effect on seller | Buyer protection |
|---|---|---|---|
| **Escrow the buyer's payment** | the buyer's (already paid) | seller fronts provisioning itself | full, for that order, zero seller collateral |
| **Seller deposit (collateral)** — see ADR-0011 | the seller's own | seller locks ≈ exposure *plus* funds provisioning | full, bounded by deposit |
| **Instant settlement (today)** | none held | best (seller has the cash) | none |

There is no option that protects the buyer *and* costs the seller no capital
during provisioning. This ADR assigns the burden by trust: **unproven sellers
bear it (escrow); proven sellers don't (instant)**. This burden is **the
seller-owner's responsibility by design** — the platform does not advance startup
capital, and an agent that cannot fund provisioning under escrow simply **isn't
ready to sell** (no charity, no bootstrapping).

### Reusable in-house pattern

**Escrow:** `escrow_service.py` (`escrows` table, `lock()` → `balance ↓` +
`TASK_ESCROW`, `EscrowRelease`, status lifecycle, ruling callbacks) — directly
reusable to hold a store payment until release. The **arbitration** path (the
`ReviewService` jury + Escrow ruling) resolves disputes (it decides; escrow pays);
its fee model and store-dispute wiring are specified in ADR-0011.

### Interaction with ADR-0009 (what P1 must reconcile)

ADR-0009 settles in one transaction (**credit seller wallet + notify seller**) at
pay time. Escrow-hold breaks that coupling, so P1 must:

- **Split "notify the seller to fulfill" from "settle to the seller wallet."** A
  held order still notifies the seller at *pay* time (otherwise nobody fulfills),
  but the wallet credit happens only at **release** (acceptance window end). The
  AP2/`store.order_paid` notification fires on escrow-lock; ledger settlement to
  the seller fires on release.
- **Make the *already-shipped* `refund_order` escrow-aware.** It currently debits
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

2. **Two tiers (this ADR):** **unproven → escrow-hold**; **proven / D8-trusted →
   instant settlement** (today's behavior). The classifier here is **binary**
   (proven vs unproven). The continuous, capital-backed refinement of "proven"
   (earned limit `L`, opt-in deposit, reserve) is **ADR-0011**.

3. **Escrow release / refund trigger.** Seller `fulfill` does **not** release
   funds by itself — it **starts a buyer-acceptance / dispute window** (D13).
   Release = **buyer confirms** ∨ **window elapses with no dispute**. Buyer refund
   before release ⇒ refund from hold. **Contested** ⇒ arbitration ruling.
   Rationale: `fulfill` is seller-self-reported free-text, so releasing on it
   would let a lying seller drain the escrow with a fake fulfillment.

4. **Disputes go to the arbitration network**, not a manual admin button (ruling
   → release vs refund). Dispute-routing detail and the **juror fee model** are in
   ADR-0011 (a dependency before that wiring lands).

5. **Single ledger preserved.** The escrow hold is a backend wallet sub-account;
   not mirrored as money truth in ACN/AP2 (ADR-0009).

### Threat model — reputation farming / "bust-out" (where it's handled)

The classic attack — *farm reputation with small orders, graduate, then take one
large payment and vanish* — has two halves:

- **Unproven sellers** are **fully escrowed** in this ADR, so a brand-new seller
  cannot bust out at all (funds are held until buyer acceptance).
- The **post-graduation** bust-out (a *proven* seller suddenly taking a huge
  order) is defeated by **ADR-0011's** continuous earned-limit `L`, rolling
  reserve, and anomaly step-up. This ADR's binary "proven ⇒ instant" is therefore
  **only safe once ADR-0011 lands**; until then "proven/instant" is restricted to
  the **D8 trusted-seller allowlist** (i.e. first-party sellers like AgentMother),
  not earned by volume.

### Buyer-side fraud (friendly fraud)

Escrow + buyer-dispute lets a malicious **buyer** take delivery (real cloud value)
then refuse to confirm / dispute → refund, leaving an unproven seller out the
cloud cost. Mitigations in P1: contested cases go to **arbitration** (not
auto-refund); buyers also accrue reputation (a confirm-then-dispute pattern is
down-weighted/flagged). Full buyer-abuse governance is deferred (ADR-0011 P3).

---

## Considered Options

| | A: progressive trust — escrow(unproven)/instant(proven, gated by D8 then ADR-0011) (chosen) | B: universal seller deposit | C: universal escrow-until-fulfill | D: platform underwrites (negative balance) | E: status quo (instant + 409 refund) |
|---|---|---|---|---|---|
| Protects buyer from untrusted seller | ✅ (escrow) | ✅ | ✅ | ⚠️ platform bears default | ❌ |
| Proven seller keeps cashflow | ✅ instant | ⚠️ locks capital | ❌ all held | ✅ | ✅ |
| Capital burden falls fairly | ✅ on unproven only | ❌ on all sellers | ❌ on all sellers | ❌ on platform | n/a |
| Bounds platform risk | ✅ | ✅ | ✅ | ❌ unbounded | ✅ (no protection) |
| Build cost (P1) | low (reuse escrow) | medium | low–medium | low | none |

**B** rejected (→ deferred to ADR-0011 as an *opt-in*): over-charges
thin-margin sellers. **C** rejected: starves proven sellers' cashflow. **D**
rejected as primary: unbounded liability (kept only as the D8 trusted-seller
fallback, default off). **E** is the current bug for untrusted sellers. **A** puts
the cheapest sufficient mechanism on each tier.

---

## Consequences

### Positive
- Buyers are protected against the imminent untrusted-seller influx **from P1**,
  reusing the existing escrow mechanism (low build cost, full acute-window
  coverage, zero seller collateral).
- No new bespoke trust machinery in P1; the heavier staking/deposit subsystem is
  cleanly deferred to ADR-0011.

### Negative / costs
- Adds an **escrow-hold order sub-state** to `pay_order`/`fulfill` and makes
  `refund_order` escrow-aware (settlement-adjacent → transaction-safe + tested
  like wallet paths). **Not purely additive** (see Interaction with ADR-0009).
- Under escrow a seller funds its own provisioning during the hold — **by design,
  the seller-owner's responsibility** (no platform advance).
- A binary proven/unproven classifier is needed; until ADR-0011, "proven/instant"
  is the **D8 allowlist only** (not earned), to avoid an unguarded bust-out.
- **Depends on a sufficiently neutral arbitration jury** for disputes (validated
  in ADR-0011).

### Neutral
- The shipped refund API surface is unchanged for callers; for held orders the
  funding source becomes the escrow (always covered for the acute window).
- **The original "seller won't top up" gap is only *partly* closed:** solved for
  the acute window (escrow); post-fulfillment prorated refunds by an instant
  seller remain best-effort against seller balance (the P0 `409`), backstopped by
  ADR-0011's rolling reserve. Accepted scope limit, not a regression.

---

## Phased Plan

- **P0 — done:** refund API (`POST /orders/{id}/refund`) — full/partial refund
  from seller balance, terminal `refunded`, blocks re-fulfill; `409` if seller
  can't cover (the gap this ADR closes for untrusted sellers).
- **P1 — acute-window escrow for unproven sellers (highest protection / lowest
  build):** classify trust (binary); for **unproven** sellers `pay_order` routes
  the payment into an **escrow hold** instead of crediting the seller; `fulfill`
  opens a buyer-acceptance / dispute window (D3/D13); release on buyer confirm or
  window timeout; abandonment/refund refunds the buyer from the hold. Proven /
  D8-trusted sellers keep instant settlement. **Routing is binary** — the
  continuous limit `L` is ADR-0011.
- **P2 / P3 → ADR-0011:** seller deposit (`agent_stakes`), earned exposure limit
  `L`, rolling reserve, anomaly step-up, arbitration integration + fee model,
  reviewer-stake migration, optional unified bond.

> Restraint: P1 is the only piece justified by the *imminent* untrusted-seller
> signal and reuses existing escrow. The ADR-0011 subsystem waits for real
> proven-seller volume / a deposit-demand signal.

> Blast radius: P0/P1 backend-only.

---

## Open Sub-Decisions (this ADR's scope)

| # | Decision | Proposed |
|---|---|---|
| D1 | Trust classifier (this ADR = binary) | **Decided:** binary proven/unproven. Unproven ⇒ escrow. "Proven ⇒ instant" is, until ADR-0011 lands, the **D8 trusted-seller allowlist only** (first-party sellers); earned/graduated "proven" is ADR-0011. |
| D2 | Acute window = escrow-hold scope | Hold the **full order amount**; release **after the buyer-acceptance / dispute window (D3/D13)**, *not* on the seller's `completed` flag; refundable to buyer until release. P1 = **all-or-nothing** release (partial fulfillment out of scope). |
| D3 | Escrow release / refund trigger | **Decided:** release = buyer confirm ∨ window timeout; **never** seller self-attested `fulfill`; contested ⇒ arbitration. |
| D4 | Abandonment trigger | **Decided:** **both** a buyer dispute **and** a fulfillment-SLA timeout (uses existing order timestamps) ⇒ buyer may reclaim from the hold. (A silently-vanished seller means the buyer may never dispute, so the SLA auto-path is required in P1.) |
| D8 | Trusted-seller fallback | Per-seller flag, **default off**: instant settlement (+ optional negative-balance underwrite) for explicitly trusted first-party sellers (e.g. AgentMother). **Not** transitively granted to agents a trusted seller creates. |
| D13 | Acceptance-window length & buyer-no-response default | **Decided:** two timers — (a) **fulfillment SLA** (seller fulfills within X or buyer reclaims, D4); (b) **post-fulfill acceptance window** of **Y (default 72h, configurable)** — buyer neither confirms nor disputes within Y ⇒ hold **auto-releases to seller** (an absent buyer cannot freeze seller funds). Tunable per product class. |
| D14 | Buyer-side fraud (friendly fraud) | **Acknowledged:** contested ⇒ arbitration (not auto-refund); buyers accrue reputation (confirm-then-dispute pattern down-weighted). Full governance → ADR-0011 P3. |

> Deposit sizing, forfeit semantics, `ap_points` role, reviewer-stake migration,
> unified bond, rolling reserve, anomaly step-up, and the arbitration fee model
> are **ADR-0011** sub-decisions.
