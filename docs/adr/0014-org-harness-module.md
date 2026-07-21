# ADR-0014: Org Harness Module — Kernel ownership, fencing sync, and v0 ports

- **Status**: Accepted
- **Date**: 2026-07-19
- **Related**: [org-harness/design-v0.md](../org-harness/design-v0.md), ADR-0001, ADR-0002, ADR-0004, ADR-0005, ADR-0007, ADR-0013
- **Decision drivers**: unblock Org Harness implementation; keep ownership isomorphic to agents; reuse subnet fencing without dual-truth bugs; keep v0 small.

## Context

[design-v0.md](../org-harness/design-v0.md) defines Org Harness as an **ACN module**:

- Org is a first-class object; members are agents; Owner is optional (`none` | `human` | `agent`).
- Module = Kernel + pluggable Ports, layered as **Org Graph** (Kernel) · **Control Loop** (`IOrgLoop`) · **Work Graph** (`IWorkPattern` strategies). Graphs constrain/structure loops; they do **not** replace the org control plane or belong in Kernel as a session fan-out runtime.
- Hard fencing reuses Network Core subnets.

A design review flagged three **P0** blockers and several **P1** tensions. This ADR records the default decisions so API/schema work can start.

Normative product doc remains design-v0; this ADR owns the contested mechanics.

---

## Decisions

### D1 — Org Owner model (optional, isomorphic to agents)

`Org.owner.kind ∈ { none, human, agent }`.

| kind | Auth for governance | Notes |
|---|---|---|
| `none` | See **D2** | Unclaimed / autonomous; org may still run Loop/Work |
| `human` | Owner JWT (same audience family as agent claim) | Not an A2A peer |
| `agent` | Agent API key or agent JWT | Owner agent SHOULD be a member (see D3) |

Operations: `claim` / `transfer` / `release` mirror agent ownership semantics (exact HTTP shape in implementation PR).

**Not decided here:** whether `release` always goes to `none` or can require a successor (follow agent release policy when implemented).

---

### D2 — Governance when `owner.kind = none`（P0-1）

**Chosen: Creator-steward + freeze-on-orphan.**

1. Every Org stores `created_by`:
   - `{ kind: human|agent, subject }` — the principal that called `POST /orgs`.
2. While `owner.kind = none`:
   - **Allowed** for `created_by` only: `claim` (self or designated — see below), update `plugins` (non-destructive), pause/resume Loop, dissolve.
   - **Forbidden** for everyone else: dissolve, transfer, change charter, change fence binding, rotate harness secret.
3. `claim`:
   - `human` or `agent` may claim an unclaimed Org if they present valid credentials **and** either (a) are `created_by`, or (b) hold an **invite-to-own** token issued by `created_by` (v0 may ship only (a); (b) is additive).
4. If `created_by.kind = agent` and that agent is **deleted / hard-revoked** while Org remains `owner.kind = none`:
   - Org enters `status = orphaned` (or `frozen`): Loop stops assigning new work; dissolve requires platform/internal recovery path (out of band for v0 product UI). Documented as known gap (same class as ADR-0005 orphan subnet).

**Rejected alternatives**

| Option | Why rejected |
|---|---|
| Anyone can claim any `none` Org | Takeover / squatting |
| `none` Org cannot dissolve except admin | Blocks agent-only lifecycle |
| Open dissolve for any member | Hostile takeover by compromised worker |

---

### D3 — Subnet owner vs Org owner（P0-3）

**Chosen: Dual identity — Org owns the product truth; subnet keeps an agent technical owner.**

| Layer | Owner field | Rule |
|---|---|---|
| **Org** | `owner` optional human/agent/none | Governance of charter, plugins, claim/transfer |
| **Subnet** | `subnet.owner` **always an agent_id** (ADR-0002) | Technical owner for admission / harness PATCH / transfer |

**Bootstrap on `POST /orgs`:**

1. Resolve **fence steward agent** `S`:
   - If caller is agent → `S = caller`.
   - If caller is human → require body `steward_agent_id` (human-owned or explicitly authorized agent); that agent becomes subnet owner and is added as Org member with role `manager` (or `steward`).
2. Create or bind subnet with `owner = S`.
3. Set Org `fencing.subnet_id`; register harness webhook using `S`'s credentials (service may impersonate via internal path only if already authenticated as `S`).

**When Org.owner is human:** subnet.owner remains steward agent `S`; human governs Org; steward agent (or Org Harness service using steward key material stored for the org) performs subnet owner-only Network Core calls.

**When Org.owner is agent `A`:** prefer `subnet.owner = A` (transfer subnet to `A` on claim if different — ADR-0005). If transfer fails, Org claim fails (atomic).

**When Org.owner is none:** subnet.owner stays `created_by` agent or steward `S`; no human in the loop.

**Invariant:** Org Harness never leaves a subnet without an agent owner (Network Core constraint unchanged).

---

### D4 — Membership ↔ subnet sync（P0-2）

**Chosen: Subnet membership is the fence truth; Org membership is the org truth; write path is transactional with compensating leave.**

| Concern | Source of truth |
|---|---|
| Can this agent pass fence / private visibility? | **Subnet** membership |
| Role, reports_to, plugin-facing member list | **OrgMembership** |

**Write rules**

1. **Add member** (invite accept / owner-add / open join mapped into Org):
   - Begin: ensure subnet join **succeeds** (or already member).
   - Then: upsert `OrgMembership(status=active)`.
   - If Org upsert fails after subnet join: **compensate** with subnet leave (best-effort + retry log). API returns 503 if compensate also fails (operator alert).
2. **Remove member**:
   - Soft-set `OrgMembership.status=inactive` first, then subnet leave.
   - If leave fails: membership stays inactive; background reconciler retries leave (v0: sync retry in request + warn metric).
3. **Read path for “is in org”**: `OrgMembership.status=active` **AND** subnet member. UI/CLI should treat mismatch as `degraded` and offer reconcile.
4. **Subnet deleted out-of-band**: Org → `status=fence_missing`; Loop stops; owner/creator may rebind subnet or dissolve.
5. **No dual public join APIs:** product docs expose Org-oriented join/invite; implementation calls subnet admission under the hood. Raw subnet join **does not** auto-create OrgMembership (avoids surprise roles). Optional later: webhook `agent.joined_subnet` → suggest membership (not auto in v0).

**Rejected:** OrgMembership-only without subnet (breaks fence). Subnet-only without OrgMembership (no roles).

---

### D5 — Task Pool dual role（P1）

**Chosen: Task Pool is a Builtin WorkPattern implementation, not a public dependency for external Patterns.**

- Inside ACN: `plugins.work = task_pool` may call Task Pool services **in-process** (not required to go through public “Pattern must use /tasks” rule).
- External adapters (Paperclip plugin, etc.): continue to follow [api-surface-tiers.md](../org-harness/api-surface-tiers.md) — **MUST NOT** build their own lifecycle on `/api/v1/tasks/*` unless they deliberately select Task Pool mode.
- Transition: `paperclip-acn-plugin` Task mirroring is **legacy bridge**; target is WorkPort events + Org APIs.

---

### D6 — Phase 1 scope vs Loop/Work（P1）

**Chosen: Phase 1 ships Kernel + minimal Work queue + thin Loop + Events.**

| Phase 1 includes | Notes |
|---|---|
| Org CRUD, claim/transfer/release | Per D1–D2 |
| Membership add/remove synced to subnet | Per D4 |
| **Minimal work items** | `work_id`, title, assignee, status ∈ {todo, in_progress, done, cancelled}; stored by Org Harness (not full Task Pool) |
| Thin Loop | Tick: list open work → noop or invoke configured wakeup hook (v0 may only emit events) |
| EventSink | Reuse subnet harness webhook; add `org.*` event types |

Full Task Pool as an **optional** `IWorkPattern` moves to **Phase 2** ([phase2-work-port-v0.md](../org-harness/phase2-work-port-v0.md)): default remains Phase 1 minimal work as `builtin_work`; Task Pool is opt-in via `plugins.work=task_pool`.

**Rejected:** Phase 1 Loop with zero work model (empty ticker).

---

### D7 — `IOrgLoop` vs `IWorkPattern` boundary（P1）

| Layer / Port | Owns |
|---|---|
| **Org Graph (Kernel)** | Org identity, owner, membership, subnet fence — not a work DAG |
| **IWorkPattern (Work Graph host)** | Work item state machine, assign/checkout/complete APIs, optional dependency / DAG fields |
| **IOrgLoop (Control Loop)** | Schedule cadence, who to wake, backoff, stalled-work detection; **calls** WorkPattern reads/writes |

Control Loop never executes L1 tools or session-scoped subagent fan-out. WorkPattern never owns subnet/Org identity. Ephemeral run workers ≠ `OrgMembership`.

---

### D8 — Adapter doc status（P1）

[org-pattern-adapter-spec-v0.md](../org-harness/org-pattern-adapter-spec-v0.md) is **external Pattern adapter** guidance only.  
Canonical module architecture: design-v0 + **this ADR**.  
Task-mirror flows in the adapter spec are marked transitional (implementation note in that file).

---

## Auth matrix (v0 summary)

| Action | owner=none | owner=human | owner=agent |
|---|---|---|---|
| create Org | — (sets created_by) | — | — |
| read Org (public fields) | any auth / public policy TBD | same | same |
| update charter / plugins / fence | created_by only | human owner JWT | owner agent key |
| claim | created_by (+ invite later) | n/a if already owned | n/a if already owned |
| transfer / release | forbidden | human owner | owner agent |
| dissolve | created_by | human owner | owner agent |
| add/remove member | created_by **or** member with `manager` role if policy allows (v0 default: created_by / owner only) | owner or manager | owner or manager |
| subnet admission admin APIs | steward agent `S` | steward agent `S` | subnet owner agent (aligned with Org owner when possible) |

Exact HTTP status codes follow ACN flat error schema (`OWNERSHIP_MISMATCH`, `AUTHENTICATION_REQUIRED`, …).

---

## Consequences

### Positive

- Unblocks schema/API without renaming ACN or forcing human owners.
- Preserves ADR-0002 (subnet owner is always an agent).
- Avoids silent dual-truth by defining sync order + compensate.
- Phase 1 Loop is meaningful (minimal work exists).

### Negative / costs

- Steward agent required when a human creates an Org (extra field).
- Orphan Org when creator agent dies unclaimed — same class of ops burden as orphan subnets.
- Two ownership surfaces (Org + subnet) must be taught in docs/CLI.

### Neutral

- Plugin host, Memory/SOP split, dispute, federation remain later phases (design-v0 §10 Phase 3 / deferred).

---

## Implementation plan (after Accepted)

1. ~~Persist `orgs`, `org_memberships`, minimal `org_work_items`~~ — done (`d3e4f5a6b7c8`, Redis keys `acn:org:*`).
2. ~~Routes under `/api/v1/orgs*` + CLI `acn org …`~~.
3. ~~Wire create → subnet create/bind + steward~~.
4. ~~Emit `org.created` / `org.member_*` / `org.work_*` / `org.loop_tick` via harness webhook~~.
5. ~~Update design-v0 §10 Phase 1 checklist to match D6~~.

---

## Changelog

- 2026-07-21: Phase 2 sequencing locked in [phase2-work-port-v0.md](../org-harness/phase2-work-port-v0.md) — default `builtin_work`, Task Pool optional.
- 2026-07-19: Proposed — resolves design review P0/P1 for Org Harness Module.
