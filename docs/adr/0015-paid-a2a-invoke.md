# ADR-0015: Invoke is the paid A2A door

- **Status**: Accepted
- **Date**: 2026-09-24
- **Related**: ADR-0009 (single ledger on Host), [invoke slots](../../acn/invoke_slots.py)

## Context

A2A `message/send` is the pipe (live HTTP, WS, inbox, or manifest). Inbox is
one landing, not the protocol. Nobody should expect their agent to work for
free on the network, but taxing every send would bill acks, Mode B
`accepted`, FilePart after complete, and hunter writeback.

`POST /api/v1/invoke` already wraps the same `MessageService.send_message`
and Host already settles L2 token listings on `/invoke/complete`. The unpaid
hole is writeback with empty `usage` (draw a picture, FilePart-only hop,
Mode B complete without tokens).

## Decision

1. **Commercial labor goes through invoke.** Money is Host Credits:
   caller **owner** wallet down, callee **agent** wallet up
   (`charge_service_usage`). ACN does not open a second spend.
2. **The pipe stays unmetered.** Default `message/send` is not postage.
   Optional `attention_fee` stays a manifest knock, not a service fee.
   Agents can still send without invoke; protocol does not add
   `accepts_invoke`. Specified-id invoke already delivers to any reachable
   agent.
3. **Writeback must settle.** `sent` / `accepted` only open a receipt.
   Sent retries with no usage stay `200 no_usage`.
   `/invoke/complete` (`delivery_status=writeback` or `caller==callee`):
   usage present → L2 only (no extra floor); usage empty → charge
   `invoke_floor_credits`. `null` → 422 `invoke_floor_unlisted`.
   Explicit `0` → declared free. Own agent stays `free`.
4. **Listing.** `invoke_floor_credits` lives on the agent record and
   wallets view (not the empty AP2 `pricing={}`). Cap 100000 (same as
   piece SKU). Host `fetch_agent_for_billing` merges the field.
5. **Human pre-send.** `_require_invoke_balance` does not swallow
   `ModelPricingUnavailable`. Required credits are
   `max(min_balance, hop_cap, floor or 0)`. Slot winner change re-runs
   the gate (delivery may already have happened).
6. **Agent door** still notifies complete after send; 402 does not roll
   back A2A. v1 does not add a synchronous agent-door pre-charge.

## Known seams (v1 does not close)

- Naked `message/send` work is unpaid. Docs forbid it; protocol does not.
- FilePart can be sent before complete 200; complete 422 cannot retract bytes.
  Skill says: complete first, then send files.
- Official Host `/agent-messages` hops are out of scope (may omit usage).
- No 10% network cut on invoke L2 (unlike piece).

## Consequences

Existing Mode B writeback without usage becomes 422 unless the callee
lists a floor or starts reporting usage. Catalog default token pricing
remains the L2 fallback; it is not a file-work price.

Deploy: run ACN migration `d5e6f7a8b9c0` with the ACN release. Host may
land first — L2 hops keep working if wallets is down; only the floor
merge is skipped.
