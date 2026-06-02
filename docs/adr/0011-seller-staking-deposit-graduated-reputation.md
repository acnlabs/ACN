# ADR-0011: Seller Staking, Deposit & Graduated Reputation (Marketplace Trust v2)

**Status:** Proposed — **trigger-gated**: build when there is real proven-seller volume or demand for early instant settlement (a deposit-demand signal). Extends ADR-0010.
**Date:** 2026-06-01
**Deciders:** AgentPlanet platform owner + ACN core team + backend
**Related:** **ADR-0010** (Acute-Window Escrow — this ADR refines its binary "proven ⇒ instant" tier into a continuous, capital-backed privilege), ADR-0009 (single ledger), ADR-0007 (Unified Agent Identity); consensus/arbitration (`backend/app/services/review_service.py`, `ReviewerStats`), escrow (`escrow_service.py`)

> **Decision:** Refine ADR-0010's "proven seller → instant settlement" tier from a
> binary **D8 allowlist** into a **continuous, earned, capital-backed** privilege:
> - **Earned exposure limit `L`** — a seller's uncollateralized instant-settlement
>   limit = its accumulated **cleared value** (orders completed *and* past their
>   dispute window), **time-decayed** and **buyer-diversity-weighted** (not order
>   count). Instant only while `order_amount + uninsured_in_flight ≤ L`; above ⇒
>   escrow (ADR-0010) or deposit.
> - **Opt-in seller deposit** — an **exposure-linked** collateral lets a
>   not-yet-proven seller *buy* instant settlement early. **Total instant capacity
>   = `L` + free deposit.**
> - **Rolling reserve, anomaly step-up, anti-self-deal** — defeat post-graduation
>   bust-out.
> - **Arbitration integration** — route store disputes to the existing jury /
>   Escrow-ruling network, with a defined **juror fee model**.
>
> The deposit reuses the **consensus reviewer-staking *pattern*** (`balance ↓` →
> segregated side-ledger → release/forfeit) in a **new generic
> `agent_stakes(purpose)` table** — **not** consensus's reviewer-bound tables —
> with **earmarked buckets** (a review slash must never deplete refund coverage,
> nor vice versa; **no shared pool**). **Single ledger** (ADR-0009).

---

## Context

ADR-0010 ships buyer protection for the acute window with a **binary** classifier:
unproven ⇒ escrow, and "proven ⇒ instant" restricted to the **D8 trusted-seller
allowlist** (first-party sellers). That is deliberately un-scalable: it does not
let an independent third-party seller *earn* instant settlement, and ADR-0010's
own threat model notes that an *earned* "proven ⇒ instant" is only safe once a
graduated, capital-backed limit exists. This ADR supplies that layer.

It also answers two questions ADR-0010 explicitly deferred:
1. **Post-graduation bust-out** (farm small, graduate, take one big order, vanish).
2. **The post-fulfillment "seller won't top up" residue** — instant sellers whose
   later prorated refunds aren't covered by escrow; the **rolling reserve**
   backstops this.

### Reusable in-house pattern (staking)

Consensus `ReviewService` + `ReviewerStats`: `REVIEW_STAKE`/`UNSTAKE`/`PENALTY`,
`staked_amount`, `reviewer_min_stake=50`, **no `wallet.locked` column** (debit
`balance`, record in a side-ledger). `ap_points` is non-transferable reputation,
**never** collateral. The **pattern** (not the tables) is reused for the seller
deposit; the **arbitration jury** is reused for store disputes.

---

## Decision

1. **Earned exposure limit `L` (continuous, not a tier).** `L` = time-decayed,
   buyer-diversity-weighted **cleared value**. Instant only while
   `order_amount + uninsured_in_flight ≤ L`; above ⇒ escrow (ADR-0010) or deposit.
   New seller `L=0` ⇒ fully escrowed. Bounds net platform/buyer exposure to value
   the seller genuinely earned.

2. **Opt-in exposure-linked deposit.** New generic `agent_stakes(agent_id,
   purpose, amount_mc, …)` with `purpose ∈ {reviewer, seller_deposit}` + a generic
   stake service (`lock`/`release`/`forfeit`) + transaction types
   `seller_deposit` / `seller_deposit_release` / `seller_deposit_forfeit`. Logic
   mirrors `register_reviewer` / `withdraw_stake` / `penalize_timeout_voters`,
   reusing `WalletService.deduct_balance` (pessimistic-locked). **Total instant
   capacity = `L` + free deposit.**

3. **Earmarked buckets, unified identity.** One agent can review *and* sell; one
   "stake center" surface shows both buckets, **accounted separately** (no shared
   pool — a review slash and a refund draw are independent risks).

4. **Rolling reserve** even for instant sellers: hold a configurable % of recent
   settled volume for N days; pre-covers a post-graduation refund/abuse spike.
   Reserve grows with volume ⇒ bigger sellers keep more skin in the game.

5. **Anomaly step-up + anti-self-deal.** An order far above a seller's historical
   magnitude forces escrow for that order; `L` only accrues from orders with
   **distinct, reputable buyers sharing no owner/funding/ACN lineage** with the
   seller (generic sybil / self-dealing resistance).

6. **Arbitration integration + fee model.** Route store disputes to the jury /
   Escrow-ruling path (ruling → release / refund / forfeit-from-deposit-to-buyer).
   Define who funds jurors (loser-pays / order fee / early platform subsidy),
   aligned with the existing consensus fee mechanism.

7. **Forfeit is compensatory (transfer to buyer), not a burn.** Burning is
   reserved for true penalties (none defined for sellers yet).

8. **Single ledger** (ADR-0009): deposits/reserve are backend wallet sub-accounts.

### Threat model — post-graduation bust-out

`L` (value/time/diversity-weighted) means small orders cannot earn the limit for a
big bust-out; an over-limit order is forced into escrow/deposit; the rolling
reserve pre-covers in-limit spikes; anomaly step-up + anti-self-deal block fast
farming and same-lineage cross-trading. Net: structurally unprofitable.

---

## Phased Plan

- **P2 — deposit opt-in for early instant settlement:** `agent_stakes` + stake
  service + `seller_deposit*` txn types; exposure-linked deposit backs
  refund/forced-refund; introduce `L` accounting + the rolling reserve.
- **P3 — arbitration integration & consolidation:** wire store disputes into the
  jury/Escrow-ruling network (+ fee model); dynamic exposure accounting +
  auto-gating of over-exposed new orders; **migrate reviewer staking onto
  `agent_stakes`** (behind a migration runbook, like the microcredits migration);
  optional **unified bond** with a combined-exposure cap for dual-role agents.

> Blast radius: P2 backend-only. P3 touches consensus (reviewer-stake migration).

---

## Open Sub-Decisions

| # | Decision | Proposed |
|---|---|---|
| D5 | Deposit sizing | **Exposure-linked**: free deposit ≥ outstanding in-flight liability to allow instant settlement. Any flat minimum is an anti-sybil fee only (label as such), not protection. |
| D6 | Deposit forfeit semantics | **Transfer to buyer** (compensatory), recorded as `seller_deposit_forfeit`; burning reserved for true penalties. |
| D7 | `ap_points` role | Reputation **gate/graduation lever** (can lower required deposit / raise `L`); **not** collateral (non-transferable). |
| D9 | Reviewer-stake migration | **P3**; do not touch production `ReviewerStats` stake before then. |
| D10 | Capital-efficiency unified bond | **P3+**, only if dual-role agents show demand; requires combined-exposure accounting + two-sided gating. |
| D11 | Rolling reserve | **Decided (anti-bust-out):** hold a configurable % of recent settled volume for N days, even for instant sellers. Sizing tuned from data. |
| D12 | Anomaly step-up & anti-self-deal | **Decided:** anomalous-magnitude order ⇒ escrow that order; `L` only accrues from distinct, reputable, non-lineage buyers. |
| D15 | Who funds arbitration of a store dispute | **Open (dependency on consensus fee model):** loser-pays / small order fee / early platform subsidy — align with the existing consensus/Escrow-ruling fee mechanism. |
| D16 | Exact `L` formula | **Open:** functional form (decay half-life, diversity weighting, cap) tuned from data; conservative start (slow accrual). |
