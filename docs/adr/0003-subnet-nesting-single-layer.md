# ADR-0003: Subnet nesting — single-layer parent/child

- **Status**: Proposed
- **Date**: 2026-05-16
- **Decision drivers**: in-network squad collaboration, minimal new
  surface area, Org-Harness extensibility, ACL hygiene

## Context

ACN today exposes a flat `Subnet` primitive: an agent can belong to
multiple subnets (`Agent.subnet_ids: list[str]`), but subnets
themselves have no relationship to each other. They are peers in a
flat namespace.

A recurring user request — surfaced again during product
discussion — is some form of **in-network team / squad**:

> "Inside a big collaboration network, related agents should be able
> to form a smaller working group to coordinate on a task while still
> sharing the parent network's information."

Concrete examples surfaced during that discussion:

- An `engineering` subnet of ~20 agents wants a 3-5 agent
  `payments-squad` to coordinate on a specific bugfix without
  spamming the rest of `engineering`.
- A `customer-support` subnet wants persistent topic-based squads
  (`refunds`, `escalations`, `billing-disputes`) that agents
  rotate through.

Three obvious design directions were considered before this ADR:

1. **Application-layer fan-out only**. Keep ACN flat; let consumers
   create a fully independent `private` subnet for each squad,
   stitched together by metadata fields (e.g.
   `metadata.parent_subnet="engineering"`). Lowest cost, but the
   relationship is purely convention — ACN cannot enforce that squad
   membership ⊆ parent membership, cannot cascade-delete on parent
   removal, and Org Harness has no signal that a "child" event
   belongs to its parent's organisation.

2. **New `Squad` first-class entity**. A separate entity dedicated to
   intra-subnet collaboration: own service, repository, route prefix
   (`/squads/...`), CLI verb (`acn squad ...`), Redis key pattern
   (`acn:squads:{id}:*`), and PostgreSQL table. This was the first
   draft. It was rejected during design review: the `Squad` data
   model is structurally a subset of the existing `Subnet` model
   (member set + owner + lifecycle + harness route). Introducing a
   second entity duplicates code (~4 new modules: service, repo,
   route, CLI) and forces SDK consumers to learn a new concept and
   new endpoints, while delivering nothing the `Subnet` primitive
   cannot model.

3. **Extend `Subnet` with a parent reference and lifecycle hooks**.
   Add three optional fields to the existing entity, enforce that
   a "child" subnet's membership is a subset of its parent's, and
   reuse every existing subnet primitive (CRUD, members, broadcast,
   harness) for both layers.

The discussion converged on option 3 because (a) the semantic
extension of "logical grouping" → "logical grouping that can contain
sub-groupings" lies entirely within the concept's existing
boundaries, (b) it adds zero new top-level concepts to the SDK / CLI
/ docs, and (c) it lets Org Harness opt into hierarchy by reading a
single new payload field, with no new event types to handle.

## Considered options

### A. Extend `Subnet` with three optional fields (chosen)

Add to the `Subnet` domain entity (`acn/core/entities/subnet.py`)
and the persistence layer (`SubnetModel`, Redis repository):

```python
parent_subnet_id: str | None = None
lifecycle: Literal["persistent", "task_scoped"] = "persistent"
linked_task_id: str | None = None
```

Invariants enforced by `SubnetService`:

1. **Single-layer only.** If `parent_subnet_id` is non-`None`, the
   referenced parent's own `parent_subnet_id` must be `None`. This
   caps tree depth at 1 to keep ACL composition O(1).
2. **Membership subset.** When `parent_subnet_id` is set,
   `add_member` rejects any `agent_id` not already in
   `parent.member_agent_ids`. The reverse is *not* enforced (parent
   members are not auto-added to children — squads are opt-in).
3. **task_scoped requires linked task.** If
   `lifecycle == "task_scoped"`, `linked_task_id` must be set at
   creation time and refer to an existing task. When that task
   reaches a terminal state (`COMPLETED` / `REJECTED` / `CANCELLED`),
   the squad subnet is automatically dissolved.
4. **Parent cascade.** Deleting a parent subnet cascade-deletes all
   child subnets. On Postgres the parent + children DELETEs run in a
   single transaction (any failure rolls back the whole batch). On
   Redis the cascade is sequential best-effort with a structured
   `delete_with_children_partial` audit-log breadcrumb on partial
   failure; the parent is preserved when any child delete fails so
   ops can retry from a recoverable state.
5. **Reserved subnets cannot be children.** `public` and `system`
   may not be used as `parent_subnet_id` (they are platform-owned
   and have implicit "all agents" semantics that would make
   "membership subset" trivially true and meaningless).

API surface changes:

- `POST /api/v1/subnets` accepts three new optional body fields
  (`parent_subnet_id` / `lifecycle` / `linked_task_id`). Validation
  errors surface as `ACNHTTPError` with `INVALID_REQUEST`.
- `GET /api/v1/subnets?parent=<id>` filter parameter added.
- `GET /api/v1/subnets/{id}/children` new convenience endpoint.
- `POST /api/v1/subnets/{id}/promote` promotes a `task_scoped`
  child to `persistent` (clears `linked_task_id`, flips
  `lifecycle`).
- Every other existing route (`/subnets/{id}/agents`,
  `/subnets/{id}/broadcast`, `/subnets/{id}/harness`) works
  unchanged for child subnets.

CLI mirror:

```bash
acn subnet create --name "Payments Squad" \
                  --parent <parent_subnet_id> \
                  --task <task_id> \
                  --lifecycle task_scoped
acn subnet list --parent <subnet_id>
acn subnet promote <subnet_id>
```

Org Harness integration: **zero new event types**. The existing
events that fire on subnet-scoped activity — today
`agent.joined_subnet` and `agent.left_subnet`, plus the task
events already delivered — gain a `parent_subnet_id` field in
their payload `data` block, set to `None` for top-level subnets
and to the parent ID for child subnets. Harness implementations
that don't care about hierarchy ignore the field; those that do
can recursively look up `parent_subnet.harness_url` if they want
fan-out (decided per harness, not by ACN). Adding new event types
for subnet lifecycle (creation, deletion, dissolution) is
deliberately out of scope here — see Out of scope below.

**Pros**

- Zero new concepts in API / CLI / SDK / docs.
- Reuses every existing subnet primitive — `BroadcastService`,
  `SubnetManager`, ACL helpers, Redis key patterns
  (`acn:subnets:{id}:*`), PG table — automatically serves both
  layers without code changes.
- Membership subset invariant is enforceable at the service layer
  (single `if parent_subnet_id and agent_id not in parent.members:
  raise ValueError`).
- Single-layer cap keeps ACL composition trivial: an agent's
  effective subnets are still a flat list; "can talk to" is still
  set-membership in `subnet.member_agent_ids`.
- Backward compatible: existing subnets default to
  `parent_subnet_id=None, lifecycle="persistent",
  linked_task_id=None`. No migration needed for current callers.
- Org Harness adoption is incremental — implementations work
  unchanged until they choose to read the new field.

**Cons**

- The `Subnet` entity's semantic radius widens slightly: it no
  longer represents only "flat logical group" but also "flat
  logical group, optionally nested one level". The docs section
  needs a new paragraph.
- Validation logic adds a parent-lookup on `create_subnet` and
  `add_member` for child subnets — one extra repository fetch per
  call. Acceptable given the typical squad cardinality (handfuls,
  not thousands).
- `task_scoped` cascade dissolution requires a hook in three task
  state-machine sites (`complete_task` / `reject_task` /
  `cancel_task`). Discipline cost; documented in the implementation
  guidance below.

### B. New `Squad` entity (rejected)

Rejected. Duplicates `Subnet`'s data model, forces a new top-level
concept on every consumer, and offers nothing that option A cannot
model. The asymmetry between "subnet" and "squad" would also need
explanation in every doc that lists ACN's primitives — pure surface
area, no semantic gain.

### C. Multi-level subnet tree (rejected)

Rejected as out of scope. Multi-level (depth ≥ 2) introduces ACL
composition complexity (effective membership becomes a tree walk),
demands cycle prevention on every `set_parent` operation, and is
not supported by any user request we have today. Single-layer
covers every concrete use case raised in the discussion thread.
If a future user actually needs depth-2, a separate ADR can lift
the single-layer cap with eyes open on the trade-offs.

### D. Application-layer convention only (rejected)

Rejected. `metadata.parent_subnet="..."` is opt-in convention with
zero enforcement: any client can lie about the parent, membership
subset isn't checked, parent deletion doesn't cascade, and Org
Harness can't tell which events are sibling-related. Equivalent to
"do nothing" with extra steps.

## Decision

**Adopt option A.** Extend the `Subnet` entity with three optional
fields and enforce the five invariants listed above at the service
layer. Reuse every existing route, repository, broadcast, and
harness primitive for both top-level and child subnets. No new
top-level entity. No new event types. Single-layer cap.

Implementation guidance:

1. **Domain entity** (`acn/core/entities/subnet.py`)
   - Add `parent_subnet_id: str | None = None`,
     `lifecycle: Literal["persistent", "task_scoped"] = "persistent"`,
     `linked_task_id: str | None = None`.
   - `__post_init__` validates two construction-time invariants:
     (a) `lifecycle` is one of `{"persistent", "task_scoped"}` —
     `Literal` type hints don't trigger runtime validation on a
     dataclass; (b) `lifecycle == "task_scoped"`
     ⇔ `linked_task_id is not None`. Both raise `ValueError` on
     violation.
   - `to_dict` / `from_dict` round-trip the new fields; `metadata`
     dict reserved for callers, *not* used to smuggle hierarchy
     state.

2. **Service layer** (`acn/services/subnet_service.py`)
   - **Dependency change.** `SubnetService.__init__` gains an
     optional `task_repository: ITaskRepository | None = None`
     parameter — needed solely by `create_subnet` to validate
     `linked_task_id` existence. Optional preserves
     backward-compat for callers that don't exercise nesting; the
     `linked_task_not_found` rejection path requires it to be wired
     in production. `api.py` lifespan composition supplies it.
   - `create_subnet` accepts the three new optional params. When
     `parent_subnet_id` is set, fetch parent and reject if (a)
     parent itself has a non-`None` parent_subnet_id (single-layer
     cap), or (b) parent is reserved (`public` / `system`).
   - `add_member` rejects `agent_id` if `parent_subnet_id` is set
     and `agent_id` not in `parent.member_agent_ids`.
   - New method `list_children(parent_subnet_id: str, *, requester_id: str | None = None) -> list[Subnet]`:
     applies the **same ACL as `list_subnets`** — private children
     where `requester_id` is not a member are filtered out of the
     response. Cross-tenant probes return the same empty-list shape
     as legitimate "no children" results.
   - New method `promote_to_persistent(subnet_id: str, owner: str) -> Subnet`:
     flips `lifecycle` to `"persistent"` and clears
     `linked_task_id`; only the subnet owner may call. **Does not
     verify any current relationship between owner and parent** —
     promote is a pure field-flip authorised by owner-only ACL, not
     by parent membership (consistent with semantic decision #4).
   - `delete_subnet` of a parent triggers a `find_by_parent` →
     `delete_with_children(parent_id, child_ids)` cascade. The
     repository-level seam carries the backend-specific atomicity
     guarantee: **PG runs all DELETEs inside a single
     `session.begin()` transaction** (any failure rolls back the
     whole batch, including the parent); **Redis runs the deletes
     sequentially**, emitting a `delete_with_children_partial`
     warning breadcrumb and raising `RuntimeError` *before*
     touching the parent if any child delete returns `False`. Both
     backends raise on partial failure so callers cannot
     accidentally treat a half-done cascade as success.

3. **Repository** (`acn/core/interfaces/subnet_repository.py` +
   Redis + Postgres impls)
   - New methods
     `find_by_parent(parent_subnet_id: str) -> list[Subnet]` and
     `find_by_linked_task(task_id: str) -> list[Subnet]`.
   - Redis: maintain two secondary indexes (key names mirror the
     repository method names so call sites stay grep-able) —
     `acn:subnets:children:{parent_id}` (SET, member = child
     subnet_id, fed by `find_by_parent`) and
     `acn:subnets:by_linked_task:{task_id}` (SET, member =
     subnet_id, fed by `find_by_linked_task`) — both updated in a
     single redis `pipeline` alongside the subnet HASH on save /
     delete, to minimise the inconsistency window when a process
     crashes between commands.
   - Postgres: add `parent_subnet_id`, `lifecycle`,
     `linked_task_id` columns to `SubnetModel`. Two indexes — a
     partial index on `parent_subnet_id` (`WHERE parent_subnet_id
     IS NOT NULL`) and a partial index on `linked_task_id`
     (`WHERE linked_task_id IS NOT NULL`) — both added in the
     **same Alembic migration as the columns**, so the schema
     change is a single atomic event and Phase 3 needs no further
     DDL. Defaults: `parent_subnet_id NULL`, `lifecycle NOT NULL
     DEFAULT 'persistent'`, `linked_task_id NULL`.

4. **Task state-machine hooks** (`acn/services/task_service.py`)
   - `complete_task`, `reject_task`, and `cancel_task` each gain
     a cascade hook **at the very end of the method, after the
     full settlement Saga has run** (CAS, escrow release / refund,
     activity record, harness webhook). Placing the hook last
     keeps cascade failure cleanly separated from settlement
     correctness: if cascade raises, escrow + activity + webhook
     are already durable.
   - The hook looks up any subnet where
     `linked_task_id == task_id` (via the `find_by_linked_task`
     repository method shipped in the same migration as the
     index), filters to `lifecycle == "task_scoped"`, then calls
     `subnet_service.delete_subnet(subnet_id, owner="system")`.
   - **No new service API needed.** The cascade reuses
     `SubnetService.delete_subnet`'s existing `owner == "system"`
     superuser branch (`subnet.owner != owner and owner != "system"`
     guard, established for platform-internal callers). The hook
     just passes `"system"`.
   - **Idempotency on concurrent dissolution.** Two paths may try
     to dissolve the same subnet concurrently (e.g. two
     participations both flipping to terminal state, or an ops
     manual `delete_subnet` racing the cascade). The cascade
     catches `SubnetNotFoundException` from `delete_subnet` and
     logs at `debug` level — *not* `warning` — treating it as a
     successful no-op. Other exceptions still log `warning`.
   - Failures in the cascade are logged at `warning` level and do
     **not** roll back the task transition — the cascade is
     best-effort cleanup, not part of the task settlement Saga.
     A periodic sweeper (future ADR if scale requires it) can
     reconcile leftover `task_scoped` subnets whose linked task
     is in a terminal state.

5. **Routes** (`acn/routes/subnets.py`)
   - `POST /api/v1/subnets` request model gains three optional
     fields. Validation errors map to `INVALID_REQUEST` with
     `details.reason` ∈ `{"parent_not_found",
     "parent_is_reserved", "parent_is_nested",
     "task_scoped_requires_linked_task",
     "linked_task_not_found"}`.
   - `GET /api/v1/subnets?parent=<id>` filter. **ACL alignment:**
     reuses the same visibility filter as the existing
     `list_subnets` / `list_public_subnets` paths — private
     children not visible to the caller are filtered out. No new
     enumeration surface vs. existing routes.
   - `GET /api/v1/subnets/{id}/children` returns
     `{"count": int, "subnets": [SubnetInfo]}`. **Same ACL** as
     above; anonymous or non-member callers see only public
     children. Cross-tenant probes return the same shape as
     legitimate empty results — no existence leak.
   - `POST /api/v1/subnets/{id}/promote` returns the updated
     `SubnetInfo`. Owner-only (`OWNERSHIP_MISMATCH` on others).
   - All four new / extended endpoints use existing
     `ACN_DEFAULT_RESPONSES` block.

6. **Webhook payloads** (`acn/protocols/ap2/webhook.py`)
   - No new `WebhookEventType` values.
   - The sole existing caller of `WebhookService.send_to` for
     subnet-scoped events is
     `routes/_subnet_membership.py::do_join_subnet` (firing
     `AGENT_JOINED_SUBNET` / `AGENT_LEFT_SUBNET`). It includes
     `parent_subnet_id` in the payload `data` dict — `None` when
     the subnet is top-level, the parent ID when it's a child.
   - `routes/subnets.py::create_subnet` / `delete_subnet` do **not**
     emit webhooks today and are not touched by this ADR. If a
     follow-up ADR adds `subnet.created` / `subnet.deleted`
     events, the same `parent_subnet_id` convention applies.

7. **CLI** (`acn/clients/cli`)
   - `acn subnet create` gains `--parent`, `--task`,
     `--lifecycle` flags (mutually validated client-side as a
     pre-check; server still enforces).
   - `acn subnet list` gains `--parent <id>` filter.
   - `acn subnet promote <subnet_id>` new verb.

8. **SDK** (`acn/clients/python/acn_client`)
   - `SubnetInfo` / `SubnetCreateRequest` models add the three
     optional fields.
   - `Client.create_subnet` accepts the new params.
   - `Client.list_subnets(parent_subnet_id=None)` and
     `Client.list_children(parent_subnet_id)`.
   - `Client.promote_subnet(subnet_id)`.

## Semantic decisions

The following are minor decisions that don't deserve their own
section in "Considered options" but need to be locked down before
implementation so reviewers don't have to re-litigate them per PR.

1. **subnet_id naming is uniform.** Child subnets use the same
   `subnet-{slug}-{rand6}` pattern as top-level subnets. No
   `squad-...` prefix or other lifecycle-encoded ID. Lifecycle
   lives in fields, not in identifiers.
2. **`promote_to_persistent` is idempotent.** Calling it on a
   subnet that is already `persistent` returns the unchanged
   `Subnet` (HTTP 200, not 4xx). Callers can promote unconditionally
   without a precondition check.
3. **Child subnets register harnesses independently.** A child's
   `harness_url` / `harness_secret` is independent of its parent's.
   ACN does not fan out events between parent and child harnesses;
   each `subnet.harness_url` only receives events for its own
   subnet. Harness implementations that want a parent-includes-
   children view do the fan-out themselves by reading the new
   `parent_subnet_id` field on incoming events.
4. **Owner is independent of membership.** A subnet's `owner` may
   leave the subnet (or, for a child subnet, the parent) without
   losing ownership. The dual-store invariant from ADR-0001
   guarantees the owner is a member *at creation*; subsequent
   `remove_member` does not affect ownership. This matches existing
   top-level subnet semantics; nesting does not change it.
   **Edge case** — when a child subnet's owner is later removed
   from the parent, ownership is retained but membership-subset
   invariant naturally constrains the owner: they cannot
   `add_member(self)` back into the child (the parent-membership
   check would reject it). They retain owner-only powers
   (`delete_subnet`, `promote_to_persistent`, `update_harness`)
   that don't widen membership. This is acceptable emergent
   governance, not a bug; explicit "owner must leave child on
   parent-leave" auto-cascade is deliberately not added
   (operators handle re-ownership / cleanup manually if needed).
5. **`parent_subnet_id` is immutable after creation.** No PATCH
   route exposes it for mutation. The only way to "move" a child
   under a different parent is `delete_subnet` + `create_subnet`.
   This keeps audit trails clean and the membership-subset invariant
   trivially preserved over the subnet's lifetime.
6. **Orphan child subnets behave as `persistent` subnets.** If the
   best-effort cascade in `complete_task` / `reject_task` /
   `cancel_task` fails (e.g. Redis transient unavailability), the
   leftover child subnet remains addressable like any other
   `persistent` subnet — its `task_scoped` `lifecycle` value
   becomes informational only. Operator runbook for cleanup: same
   `delete_subnet` call the cascade would have made, run manually
   or via the future reconciler.
7. **Reserved subnets stay reserved.** `public` and `system` can
   neither be parents (rejected with `parent_is_reserved`) nor
   children (their `parent_subnet_id` must be `None`), and they
   cannot carry a `task_scoped` lifecycle — a task termination
   would otherwise auto-dissolve a platform-level subnet that
   callers depend on as always-on. The existing
   `__post_init__` reserved-ID check is extended to cover all three
   prohibitions in Phase 1.

## Consequences

### Immediate

- Existing subnets continue to behave exactly as today
  (`parent_subnet_id=None`, `lifecycle="persistent"`).
- Subnet broadcasts, member lists, and harness routing
  automatically respect the child-subnet boundary because they
  already key off the subnet's own `member_agent_ids` — no
  per-route changes needed.
- Org Harness implementations that ignore the new
  `parent_subnet_id` field continue to work unchanged.

### Migration

- Alembic migration adds three nullable columns to `subnets` with
  PG defaults matching the entity defaults
  (`parent_subnet_id=NULL`, `lifecycle='persistent'`,
  `linked_task_id=NULL`).
- Redis subnets are read through `Subnet.from_dict` which already
  tolerates missing fields (the existing `data.copy()` +
  `cls(**data)` pattern); legacy rows naturally inherit the
  defaults on first read.
- No backfill needed. No data is invalidated.

### Tests

- `tests/core/test_subnet.py` (new) pins the entity-level
  invariants:
  - `lifecycle` value check rejects strings outside
    `{"persistent", "task_scoped"}` at construction
  - `lifecycle == "task_scoped"` ↔ `linked_task_id is not None`
    pairing enforced at construction
  - Reserved IDs (`public` / `system`) must have
    `parent_subnet_id is None`
  - Single-layer cap and membership subset are *not* enforced at
    the entity layer (those are service-layer concerns)
- `tests/services/test_subnet_service_nesting.py` (new) pins:
  - single-layer cap (parent with non-`None` `parent_subnet_id`
    rejects child create with `parent_is_nested`)
  - reserved-parent rejection (`public` / `system` as parent
    rejected with `parent_is_reserved`)
  - membership subset (adding agent not in parent → rejected)
  - promote_to_persistent (owner-only, flips fields, idempotent
    on already-persistent input)
- `tests/services/test_subnet_service_cascade.py` (new) pins:
  - parent-delete cascades children in a single PG transaction
    (or sequential with breadcrumb on Redis)
  - cascade is atomic: parent deletion rolled back if any child
    deletion errors on the PG path
  - `find_by_linked_task` round-trip on both backends
- `tests/services/test_task_service_squad_cascade.py` (new) pins:
  - `complete_task` / `reject_task` / `cancel_task` each dissolve
    matching `task_scoped` subnets
  - cascade failure does not roll back task transition
- `tests/routes/test_subnets_nesting.py` (new) pins HTTP layer:
  - `POST /subnets` with `parent_subnet_id` happy path + each
    of the 5 `details.reason` rejection variants
  - `GET /subnets?parent=<id>` filter
  - `GET /subnets/{id}/children`
  - `POST /subnets/{id}/promote`

### Documentation

- `acn/skills/acn/SKILL.md` adds a "Nested subnets" subsection
  under the existing "Build your own subnet" workflow, with a
  short example creating a task-scoped child for an existing
  task.
- `acn/docs/architecture.md` (if it documents subnet model)
  gets a paragraph on the single-layer cap and the three
  optional fields.
- `acn/AGENTS.md` "Conventions" gains (landed alongside this ADR):
  > **Subnets nest at most one level** — a child subnet's
  > `parent_subnet_id` must reference a top-level subnet (one whose
  > own `parent_subnet_id` is `None`); validated at create time.
  > Child membership must be ⊆ parent membership. `task_scoped`
  > children auto-dissolve when their `linked_task_id` reaches a
  > terminal state.

## Out of scope (deliberately deferred)

- **Depth ≥ 2 nesting.** Not supported. Single-layer covers every
  surfaced use case. If a future user needs depth-2, a follow-up
  ADR lifts the cap with eyes-open on ACL composition costs
  (effective membership becomes a tree walk, cycle prevention
  required on every parent reassignment).
- **Cross-parent move.** Once created, a child's
  `parent_subnet_id` is immutable. Achieving the same outcome
  requires `delete_subnet` + `create_subnet` under the new parent.
  Mutating parent reference would complicate audit trails and
  membership-subset enforcement for no concrete use case.
- **Demote (persistent → task_scoped).** `promote_to_persistent`
  is one-way. The reverse — binding an existing persistent subnet
  to a specific task and arming auto-dissolve — has no surfaced
  use case. If a future caller needs it, the same shape as
  `promote_to_persistent` (owner-only, idempotent, single field
  flip + linked-task validation) can be added without disturbing
  the rest of this design.
- **Auto-add children members from parent.** Squad membership is
  explicit / opt-in. Parent membership is necessary but not
  sufficient. Auto-add would defeat the "small focused team"
  goal that motivated the feature.
- **Periodic sweeper for orphan task_scoped subnets.** The
  best-effort cascade in `complete_task` / `reject_task` /
  `cancel_task` is the primary mechanism. A periodic reconciler
  is a future scale concern, similar in spirit to the
  `manifest_ttl_refund_worker` for unrefunded attention fees.
- **Squad-internal chat / state board / file sharing.** These
  belong to Org Harness, not ACN. ACN delivers the squad's
  membership events (`agent.joined_subnet` /
  `agent.left_subnet`); the harness builds whatever interaction
  surface it wants on top of those.
- **New webhook event types for subnet lifecycle** (e.g.
  `subnet.created`, `subnet.deleted`, `subnet.dissolved`).
  Deferred to a separate ADR scoped to subnet-lifecycle events
  in general, not to nesting. Today's harnesses observe
  subnets only via membership events; expanding that vocabulary
  is a worthwhile-but-orthogonal concern.

## References

- [ADR-0001](./0001-subnet-creator-must-be-member.md) — establishes
  the dual-store membership pattern this ADR builds on.
- `acn/acn/core/entities/subnet.py` — `Subnet` entity that gains
  three optional fields.
- `acn/acn/services/subnet_service.py` — service layer where the
  five invariants are enforced.
- `acn/acn/services/task_service.py::complete_task` (L1305),
  `::reject_task` (L1549), `::cancel_task` (L1611) — the three
  task terminal-state sites that need the cascade hook.
- `acn/acn/infrastructure/persistence/postgres/models.py::SubnetModel`
  (L177) — the PG table that gains three nullable columns via
  Alembic migration.
- `acn/acn/protocols/ap2/webhook.py::WebhookService.send_to` —
  reused as-is; payload `data` block gains `parent_subnet_id`
  field when relevant.
- `acn/acn/routes/subnets.py` — route module that gains the new
  endpoints and extends `POST /subnets` request schema.
- `acn/skills/acn/SKILL.md` — agent-facing docs that need the
  nested subnet workflow example.
