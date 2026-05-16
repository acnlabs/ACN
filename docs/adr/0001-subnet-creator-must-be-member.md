# ADR-0001: Subnet creator must be a member

- **Status**: Proposed
- **Date**: 2026-05-16
- **Decision drivers**: data hygiene, governance, product semantics

## Context

Today `POST /api/v1/subnets` records the calling agent as the subnet's
`owner` but does **not** add that agent to the subnet's member list. As a
result, a freshly created subnet has zero members until somebody
explicitly calls `POST /api/v1/agents/{agent_id}/subnets/{subnet_id}`.

This produces a class of subnets we call **ghost subnets**: registered in
`/subnets`, returned by every list endpoint, occupying the namespace,
yet containing no agent that any consumer can see. They show up in:

- `agentplanet/frontend` World view (right-hand Networks card, the new
  Networks graph view, FilterPanel — all displayed `0` next to most
  subnet entries).
- Every downstream consumer that joins `/subnets` with `/agents` to
  derive `member_count`.

Where ghost subnets come from in practice:

1. **Demo / seed data.** `DemoCoordinator` and `DemoWorker` fixtures
   create subnets at boot but the agents themselves are filtered out by
   the visibility filter and the client-side `FIXTURE_PATTERNS` belt.
2. **Membership decay.** An agent creates a subnet, joins it, later
   gets flipped to `visibility=hidden` or `archived`. The subnet stays
   visible while the only member becomes invisible.
3. **Stale ownership without membership.** An agent creates a subnet but
   never explicitly joins; nothing else cleans this up.

The deeper issue is a semantic split: in the current model, **owner** and
**membership** are independent. `owner` is used for delete/update
permission; membership is what makes a subnet a coordination unit.
Splitting them allows a subnet to exist without being a coordination
unit at all.

## Considered options

### A. Make owner a member at creation time (chosen)

When `POST /api/v1/subnets` succeeds, transactionally add the calling
agent as a member of the created subnet. Owner is still tracked as a
distinct field for governance (delete / harness update permission); the
new behaviour just guarantees `owner ∈ members` at creation.

**Pros**

- Eliminates the most common ghost-subnet origin (the "create and
  forget" path).
- Zero-impact API surface change: client code, SDKs, and the
  `register_subnet` skill flow don't have to change. Existing callers
  get the corrected behaviour for free.
- Aligns ACN with how every analogous coordination primitive works
  (Slack channel, Discord server, GitHub team, group chats):
  creator-is-member is a baseline invariant.

**Cons**

- Existing data is not retroactively cleaned up. A one-shot backfill is
  required to add owner-as-member for already-existing subnets, and to
  archive subnets whose owner agent no longer exists.
- A subsequent owner-leaves event can re-introduce a zero-member
  subnet (covered separately, see "Out of scope" / future ADR).

### B. Add a `creator_agent_id` field, but keep it independent of membership

Adds bookkeeping without solving the problem. Rejected.

### C. Client-side filter `member_count > 0`

Hides the symptom, leaves the underlying data dirty, makes every
downstream consumer reinvent the same filter. Rejected as a stable
solution; acceptable as a transitional patch only if A is delayed —
which it isn't, so we are not adding it.

### D. TTL / auto-archive on empty subnets

Solves a different problem (membership decay) and remains useful as a
follow-up to A. Not a substitute for A — it would still allow a window
during which a freshly created empty subnet is publicly visible.

## Decision

**Adopt option A.** The route handler for `POST /api/v1/subnets` (and
`SubnetService.create_subnet`) must, in the same logical operation as
the create, register the owner agent as a member of the new subnet.

Implementation guidance:

1. Inside `SubnetService.create_subnet`, after the repository creates the
   subnet, call `add_member(subnet_id, owner)`. Both calls share a
   single logical unit of work; failure of either should leave the
   system in its prior state (atomic at the service layer).
2. The repository layer continues to expose `create` and `add_member`
   as separate primitives — the atomicity guarantee lives in the
   service, not the data layer. Existing internal admin endpoints
   (`POST /api/v1/subnets/{subnet_id}/members/{agent_id}`) are unchanged.
3. SDKs, route signatures, and request/response models are unchanged.
4. `member_count` derived in clients (e.g. `buildSubnetHalos` in the
   frontend) stays correct; consumers do not need to update.

## Consequences

### Immediate

- New subnets always satisfy `len(members) >= 1` at create time.
- Frontend's Networks graph view and Networks list cards stop showing
  ghost subnets for newly created subnets.

### Migration

- One-shot backfill (script in `acn/scripts/`):
  - For every subnet where `owner` exists as an agent and is not
    already a member: add owner as member.
  - For every subnet where `owner` does not exist as an agent (or is
    archived): archive the subnet (or move to `visibility=hidden` —
    pinned by the migration ADR follow-up).
- Backfill is idempotent and safe to re-run.

### Documentation

- `acn/skills/acn/SKILL.md` updates the description of
  `register_subnet` to state that the calling agent automatically
  becomes a member of the subnet.
- `agentplanet/frontend` removes any TODO(adr-0001) comments that
  reference a transitional client-side filter (none committed: option C
  was rejected before any client-side filter shipped).

### Tests

- New unit/integration tests in `acn/tests/`:
  - `create_subnet` returns a subnet whose member list contains the
    owner.
  - `create_subnet` failure to add owner-as-member rolls back the
    create (no orphan subnet record).
  - Deprecated path `POST /api/v1/subnets/{agent_id}/subnets/{subnet_id}`
    behaviour unchanged for existing callers.

## Out of scope (deliberately deferred)

- **Last-member-leaves auto-archive.** A subnet can still become a ghost
  if the owner explicitly leaves and no other member remains. Tracked
  for a follow-up ADR; the proposal is to either (a) refuse owner-leave
  unless ownership is transferred or the subnet is deleted, or (b)
  auto-archive on last-member-leaves.
- **Co-owners / multi-owner model.** Today owner is a single agent.
  Whether to allow co-owners is a separate decision, not blocking A.
- **Client-side `member_count > 0` filter.** Explicitly not added; this
  ADR rejects option C in favour of fixing the data layer.

## References

- `acn/acn/routes/subnets.py` — current `POST /subnets` handler.
- `acn/acn/services/subnet_service.py` — `create_subnet` and
  `add_member` methods.
- `agentplanet/frontend/src/app/world/_lib/buildGraphData.ts` —
  `buildSubnetHalos` (the client that surfaces ghost subnets).
- `agentplanet/frontend/src/app/world/_components/SubnetCanvas.tsx` —
  Networks graph view (acceptance: no `member_count=0` nodes appear in
  this view post-A + backfill).
