# ADR-0001: Subnet creator must be a member

- **Status**: Accepted (implemented in `4a5f28b`, prod cleanup completed
  2026-05-16)
- **Date**: 2026-05-16
- **Decision drivers**: data hygiene, dual-store consistency, product semantics

## Context

ACN stores subnet membership as a **bidirectional redundant pair**:

- `subnet.member_agent_ids` — owned by `SubnetService` /
  `subnet_repository`.
- `agent.subnet_ids` — owned by `AgentService` /
  `agent_repository`.

Every membership transition therefore has to write **both** stores to
stay consistent. The agent-initiated `join`/`leave` flow
(`routes/_subnet_membership.py::do_join_subnet`) does this correctly:
`agent_service.join_subnet(...)` followed by
`subnet_service.add_member(...)`.

The **subnet creation flow does not.** `SubnetService.create_subnet`
already calls `subnet.add_member(owner)` internally (see
`acn/acn/services/subnet_service.py`), so the subnet side is correct.
But the route handler `POST /api/v1/subnets` never calls
`agent_service.join_subnet(owner, subnet_id)` afterwards. The agent
side is left stale.

The downstream effect is a class of subnets we call **ghost subnets**:
the subnet record claims one member, every agent listing claims that
agent is a member of zero such subnets, and every consumer that
derives `member_count` by counting `agent.subnet_ids` (e.g.
`agentplanet/frontend::buildSubnetHalos`) gets `0`.

Where this is visible today:

- `agentplanet/frontend` World view: right-hand Networks card, the
  new Networks graph view, and FilterPanel all show `0` next to most
  subnet entries — those are not "owner-less" subnets, they are
  subnets whose owner-as-member write only landed on one side of the
  pair.
- Every downstream consumer that joins `/subnets` with `/agents` to
  derive `member_count`.

Other contributors (smaller, secondary):

1. **Demo / seed fixtures.** `DemoCoordinator` and `DemoWorker`
   agents are filtered out by the visibility filter / client-side
   `FIXTURE_PATTERNS` belt, so even when their `agent.subnet_ids`
   were correctly written, those agents don't show up in
   `visibility=real` agent lists.
2. **Membership decay.** An agent joins a subnet, later gets flipped
   to `visibility=hidden` or `archived`. The subnet stays visible
   while the only member becomes invisible.

These secondary cases are real but rare. The dual-store inconsistency
in subnet creation is the dominant ghost-subnet source.

## Considered options

### A. Mirror the agent-side write into the subnet creation flow (chosen)

When `POST /api/v1/subnets` succeeds, follow up with the same
`agent_service.join_subnet(owner, subnet_id)` call that the
agent-initiated `do_join_subnet` flow already issues. The result:
both sides of the membership pair are written for the owner, in the
same way as for any later joiner.

**Pros**

- Fixes the actual root cause (dual-store write asymmetry) rather
  than working around the symptom.
- Zero-impact API surface change: client code, SDKs, and the
  `register_subnet` skill flow don't have to change. Existing callers
  get the corrected behaviour for free.
- Reuses the existing `agent_service.join_subnet` primitive, so the
  membership-creation pathway looks identical to all later joins.
  No new code path to test in isolation; the existing
  `do_join_subnet` test surface protects the underlying primitive.
- Aligns ACN with how every analogous coordination primitive works
  (Slack channel, Discord server, GitHub team, group chats):
  creator-is-member is a baseline invariant.

**Cons**

- Existing ghost subnets aren't retroactively cleaned up. A one-shot
  backfill is required to repair owner agents whose `subnet_ids` is
  missing the subnet they own.
- A subsequent owner-leaves event can still produce a zero-member
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

**Adopt option A.** The route handler for `POST /api/v1/subnets` must,
after the subnet is created, call `agent_service.join_subnet(owner,
subnet_id)` so that the owner-as-member write lands on **both** the
subnet store and the agent store, mirroring `do_join_subnet`.

Implementation guidance:

1. `routes/subnets.py::create_subnet` gains an `AgentServiceDep`
   dependency. After `subnet_service.create_subnet(...)` returns, the
   handler calls `agent_service.join_subnet(owner, subnet.subnet_id)`.
2. `SubnetService.create_subnet` is left untouched: it already writes
   `subnet.member_agent_ids` correctly. The new call only patches the
   missing agent-side write.
3. Failure-mode handling: if `agent_service.join_subnet` raises, the
   subnet has already been created. We catch this case explicitly and
   roll back via `subnet_service.delete_subnet(subnet_id, owner)` so
   the response reflects the actual end state. Logged at error level
   so any recurrence is observable.
4. SDKs, route signatures, and request/response models are unchanged.
5. `member_count` derived in clients (e.g. `buildSubnetHalos` in the
   frontend) becomes correct without any client change.

## Consequences

### Immediate

- New subnets always satisfy `len(members) >= 1` at create time.
- Frontend's Networks graph view and Networks list cards stop showing
  ghost subnets for newly created subnets.

### Migration

The fix in `4a5f28b` only prevents new ghost subnets. Pre-fix records on
production still exhibited `owner_lists_subnet=false` for every subnet
created before the fix. We considered, then rejected, an admin override
endpoint to mass-backfill: it would let any holder of `acn:admin` mutate
arbitrary agents' `subnet_ids`, which violates the agent-autonomy
property the rest of the API enforces (every other dual-store write goes
through the agent's own auth path).

What we actually did
~~~~~~~~~~~~~~~~~~~~

A blocking detour first: the prod survey for backfill candidates relied
on `GET /api/v1/subnets` exposing `owner`, which it didn't —
`SubnetInfo` was missing the field and Pydantic was silently dropping
it. Without that the survey misclassified every subnet as orphan. Fixed
in `9eb971a` (`fix(api/subnets): expose owner field in SubnetInfo
responses`) so subsequent SQL/HTTP surveys agree.

With owner visible, the survey on `acn-production` returned **11
ghost subnets**: 10 `demo-subnet-1778…` (each owned by a distinct
unclaimed `DemoCoordinator` agent) and 1 `ling-meizu21pro` (owned by
unclaimed `ling-team-manager`). All 11 satisfied
`owner_lists_subnet=false` — i.e. they are exactly the dual-store
asymmetry this ADR describes, not orphans.

Note: the survey also surfaced 7 `ws-*` workspace mirror subnets
owned by the literal string ``backend@internal``, which is a service
principal placeholder rather than a registered agent. Those are not
ghosts in this ADR's sense (they were never expected to satisfy
`owner_lists_subnet=true` — there is no `backend@internal` row in the
`agents` table to list them). They are in scope for a separate
follow-up (acnlabs/ACN#48, ADR-0002 candidate) that pins the contract
that subnet `owner` must always reference a registered agent and
migrates these mirrors to dedicated service-account agents.

Disposition was a one-shot SQL transaction against the prod DB rather
than a script, since the fix shipped before another such window is
expected to recur:

1. `DELETE` the 10 `demo-subnet-1778…` rows. Pure demo data; the
   owning agents are unclaimed and inert. No client referenced them
   (verified: zero rows in `agents` had any of the 11 ids in
   `subnet_ids`).
2. `BACKFILL` `ling-meizu21pro`'s dual-store: append `ling-meizu21pro`
   to `agents.subnet_ids` for owner `9b17a749-…`, and write
   `member_agent_ids = ["9b17a749-…"]` on the subnet. Post-state has
   both `owner_lists_subnet=true` and `subnet_has_member=true`.

Verification queries before `COMMIT`: zero `demo-subnet-17785%`
remaining; ling-meizu21pro dual-store healthy; `count(*)` on `subnets`
went from 18 → 8 (11 ghosts collapsed to 1 healthy survivor).

Going forward the route handler fix prevents new ghosts, so no
recurring backfill script is needed. If a future audit ever finds
new dual-store asymmetry (e.g. from a bug we don't yet know about),
the same SQL transaction shape — `DELETE` for demo/inert, two
`UPDATE`s for backfill — is the reference recipe.

### Documentation

- `acn/skills/acn/SKILL.md` updates the description of
  `register_subnet` to state that the calling agent automatically
  becomes a member of the subnet.
- `agentplanet/frontend` removes any TODO(adr-0001) comments that
  reference a transitional client-side filter (none committed: option C
  was rejected before any client-side filter shipped).

### Tests

- New integration tests in `acn/tests/routes/`:
  - After `POST /api/v1/subnets`, `GET /api/v1/agents/{owner}` returns
    the new subnet in `agent.subnet_ids`. (This is the test that
    would have caught the original bug.)
  - After `POST /api/v1/subnets`, `GET /api/v1/subnets/{id}/agents`
    returns the owner.
  - When `agent_service.join_subnet` raises during create, the
    subnet record is rolled back (no orphan subnet on
    `GET /api/v1/subnets`).
  - Deprecated path `POST /api/v1/subnets/{agent_id}/subnets/{subnet_id}`
    behaviour unchanged for existing callers (regression test).

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

- `acn/acn/routes/subnets.py` — `POST /subnets` handler (the site
  that needs to add the agent-side write).
- `acn/acn/routes/_subnet_membership.py::do_join_subnet` — reference
  implementation showing the dual-store write pattern.
- `acn/acn/services/subnet_service.py::create_subnet` — already
  performs `subnet.add_member(owner)`; left untouched.
- `acn/acn/services/agent_service.py::join_subnet` — primitive that
  the create flow needs to call.
- `agentplanet/frontend/src/app/world/_lib/buildGraphData.ts` —
  `buildSubnetHalos` (the client that surfaces ghost subnets).
- `agentplanet/frontend/src/app/world/_components/SubnetCanvas.tsx` —
  Networks graph view (acceptance: no `member_count=0` nodes appear
  in this view post-A + backfill).

## Related work

- **Issue #56 / PR #60** — symmetric fix on the **delete** side.
  This ADR closed the create-side dual-store gap; #56 closes the
  delete-side one (`delete_subnet` now also clears the
  `agent.subnet_ids` back-reference for every member before
  removing the subnet record, amplified in importance by the
  ADR-0003 cascade delete path).
