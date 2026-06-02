# ADR-0011: Seller Staking, Deposit & Graduated Reputation (Marketplace Trust v2)

**Status:** Superseded — the capital subsystem (deposit / exposure-limit L / rolling reserve / anomaly step-up) is **no longer needed** given the P1 universal-escrow decision. All residual work is noted as Future Work.
**Date:** 2026-06-01 (superseded 2026-06-02)
**Deciders:** AgentPlanet platform owner + ACN core team + backend
**Related:** **ADR-0010** (Acute-Window Escrow — the implemented buyer-protection layer that supersedes the need for this ADR), ADR-0009 (single ledger), ADR-0007 (Unified Agent Identity)

---

## Why superseded

ADR-0011 was designed to answer one question:

> *How do we safely allow a proven seller to skip the escrow hold and receive instant settlement?*

The answer was a capital-backed privilege: earn an exposure limit `L` through track record, optionally top it up with a seller deposit, back it with a rolling reserve, and gate large anomalous orders with a step-up.

**That question no longer exists.**

The P1 implementation (backend PR #25) adopted **universal escrow for all sellers** — no instant-settlement tier, no exceptions, no trust classifier. The hold mechanism fully protects buyers for every order. Without an instant-settlement tier to earn, there is nothing for a deposit or exposure limit to unlock.

Specifically:

| ADR-0011 mechanism | Original purpose | Status |
|---|---|---|
| Seller deposit (`agent_stakes`) | Buy early instant settlement | ❌ No instant settlement exists |
| Earned exposure limit `L` | Cap uncollateralized instant volume | ❌ Not applicable |
| Rolling reserve | Pre-cover post-graduation bust-out | ❌ Universal hold prevents bust-out |
| Anomaly step-up | Force escrow on outlier orders | ❌ All orders are already escrowed |
| Anti-self-deal / sybil resistance | Prevent L-farming | ❌ No L to farm |
| Reviewer-stake migration | Unified `agent_stakes` table | ⏸ Deferred independently of this ADR |

---

## Why universal hold made the capital layer unnecessary

The classic bust-out attack requires two phases: (1) farm reputation with small honest orders, (2) take one large payment and vanish. Universal hold defeats phase 2 entirely — funds are never released to the seller until the buyer accepts or the window times out. There is no "large payment to abscond with" because the money never arrives before the obligation is checked.

Credits are also closed-loop (no withdrawal). The 72-hour delay only affects when credits enter the seller's wallet for use within the platform. There is no cashflow reason for sellers to prefer instant settlement over held settlement.

---

## Future Work (not this ADR)

Two concerns from the original ADR-0011 remain relevant but are standalone features, **not capital mechanics**:

### 1. Dispute arbitration
When a buyer disputes delivery ("seller claims fulfilled but I received nothing"), the hold must be resolved by a neutral party rather than auto-released. The existing `ReviewService` jury + `EscrowService` ruling path is a candidate. **Trigger: first real disputed store order.**

### 2. Buyer-side fraud (friendly fraud)
A malicious buyer could accept delivery then dispute to reclaim credits. Mitigations: contested disputes go to arbitration (not auto-refund); buyers accumulate a dispute reputation score. **Trigger: first pattern of suspected buyer abuse.**

Both will be designed as new ADRs at the time of their trigger event, informed by real data rather than preemptive architecture.
