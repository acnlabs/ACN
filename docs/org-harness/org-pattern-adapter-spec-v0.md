# Org Pattern Adapter Spec v0 (Paperclip → ACN Core)

**Status:** Spec v0 — external Pattern adapter contract  
**Last updated:** 2026-07-22  
**Depends on:** [design-v0.md](./design-v0.md), [api-surface-tiers.md](./api-surface-tiers.md), [org-model-v0.md](./org-model-v0.md), [phase2-work-port-v0.md](./phase2-work-port-v0.md), [ADR-0014](../adr/0014-org-harness-module.md)

> **Naming / ownership:** Canonical architecture is [design-v0.md](./design-v0.md).
> Org Harness is an **ACN module** (optional Org Owner: none/human/agent;
> agent members). This document only covers **external** Pattern adapters
> (e.g. Paperclip plugin). It does **not** mean Org lives only in Paperclip.

> Goal: let a company-style Org Pattern (Paperclip) plug into ACN Org Harness
> / Network Core — preferably via Work/Loop ports — without mistaking the
> adapter for the Org Harness module itself.
>
> **Work Port (Phase 2):** New adapter paths MUST use Org work
> (`POST/PATCH /api/v1/orgs/{id}/work*`) and prefer `org.work_*` /
> `org.loop_tick` harness events. ACN `task.*` ↔ Paperclip issue mirroring is a
> **legacy bridge** only; do not treat `/api/v1/tasks*` as the long-term Pattern API
> (ADR-0014 D5/D8).

---

## 1. Roles

| Role | Responsibility |
|---|---|
| **ACN Network Core** | Identity, fencing, A2A, settlement-read, harness webhook delivery; Execution Workspace registry ([exec-workspace-v0.md](./exec-workspace-v0.md)) |
| **ACN Org Harness** | Org graph, membership, Work/Loop ports, `org.*` events |
| **Org Pattern (Paperclip)** | Company, org chart, issues, budgets, heartbeat wakeups, approvals UI |
| **L1 Agent** | Executes work when woken; speaks ACN with its own key |
| **Adapter** | Glue: maps Paperclip entities ↔ Org Harness + Core; verifies HMAC events |

The adapter MAY live inside Paperclip (preferred) or as a sidecar service.
Reference implementation: [`paperclip-acn-plugin`](https://github.com/acnlabs/paperclip-acn-plugin) (P2c C0–C3).

---

## 2. Entity mapping

| Paperclip | Org Model v0 | ACN |
|---|---|---|
| `company.id` | Pattern-local | — (not stored on ACN) |
| `company.name` / goal | `display_name` / `charter.mission` | `POST /api/v1/orgs` fields; subnet name optional mirror |
| `company` fence | `fencing.subnet_id` | Org bind: create Org with `subnet_id`, or reuse bound Org |
| harness receiver URL | `harness_webhook.url` | `PATCH /api/v1/subnets/{slug}/harness` |
| `agent` / employee | `OrgMembership.agent_id` | registered `agent_id` |
| role / title / reports_to | `role` / `reports_to` | Org membership + Pattern-only fields |
| budget | `budget` | — (Pattern only) |
| `issue` / ticket | `OrgWorkItem` | `POST/PATCH /api/v1/orgs/{org_id}/work*` |
| heartbeat wakeup | Org Loop tick | `POST /api/v1/orgs/{org_id}/loop/tick` → `org.loop_tick`; Pattern wakes L1 |
| board approval | Org RBAC | — ; may gate join via subnet invitations |

### Anti-mappings (do not do)

| Temptation | Why not |
|---|---|
| Create an ACN **Task Pool** task for every Paperclip issue | Couples Pattern to Reference Tier 2; Phase 2 default is `builtin_work` |
| Use subnet membership as the only org chart | No roles / budgets / mission |
| Store Paperclip API keys in ACN | Wrong trust boundary |
| Call `/communication/internal/send` | Internal tier |
| Treat harness envelope `task_id` as a Task Pool id on `org.*` events | For Org events that field carries **`org_id`** (legacy payload shape) |

---

## 3. Wire protocol: harness webhook

When `PATCH /subnets/{slug}/harness` registers a URL, ACN POSTs signed lifecycle
events (same HMAC-SHA256 scheme as payment webhooks). Adapter MUST:

1. Verify signature with `harness_secret`.
2. Idempotently apply by event id / `(type, org_id|agent_id, subnet_id, ts)`.
3. Ignore unknown event types (forward-compatible).
4. Route by `event` string — for `org.*`, read work/org fields from `data`, not from
   the overloaded top-level `task_id`.

### Events the adapter MUST handle (v0 — preferred)

| Event | Adapter action |
|---|---|
| `org.work_created` | Upsert Issue ↔ `work_id` map; create Issue if inbound and not an outbound echo |
| `org.work_updated` | Sync Issue status (`todo` / `done` / `cancelled`; `in_progress` may be comment-only) |
| `org.loop_tick` | Pattern-side wakeup / audit (optional throttled comments); no Task Pool |
| `agent.joined_subnet` | Ensure `OrgMembership` active; sync role defaults |
| `agent.left_subnet` | Mark membership inactive / paused |

### Events the adapter MAY handle (legacy)

| Event | Adapter action |
|---|---|
| `task.*` / `participation.*` | Optional Task Pool mirror — **not** required for Org work conformance |

### Events the adapter SHOULD emit into ACN (outbound)

| Pattern action | ACN call |
|---|---|
| Human creates issue | `POST /api/v1/orgs/{org_id}/work` |
| Issue → done / cancelled | `PATCH /api/v1/orgs/{org_id}/work/{work_id}` |
| Manager DMs worker | `POST /communication/send` |
| Need attention-fee notify | Convention: `manifest/send` (optional) |
| Read payment state | `GET /payments/tasks/…` / `stats` (settlement-read; not work dispatch) |

Inbound work to relay agents uses Mode B / gateway — not this webhook.

---

## 4. Bootstrap sequence

Canonical bootstrap uses **Org Harness create**, not “subnet-only then invent Org
elsewhere”. Subnet fencing remains required; Org is the durable handle.

```
1. Operator creates Paperclip company (Pattern-local id, charter.mission)
2. Ensure ACN subnet exists (reuse or create):
     POST /api/v1/subnets   (if needed)
     { "id": "<slug>", "name": "...", "join_policy": "approval", ... }
3. Create (or resolve) Org bound to that subnet:
     POST /api/v1/orgs
     { "display_name": "...", "subnet_id": "<slug>", "join_policy": "open"|"approval" }
   On 409 subnet_already_bound: reuse the bound org_id from the error message
   (or require operator to set acnOrgId explicitly).
4. Register harness webhook on the Org fence subnet:
     PATCH /api/v1/subnets/<slug>/harness
     { "harness_url": "https://…/hooks/acn", "harness_secret": "…" }
5. For each employee agent:
     a. Ensure agent registered on ACN (or discover existing agent_id)
     b. Invite / allowlist / join subnet (admission Core APIs)
     c. Create OrgMembership { role, reports_to, budget }
     d. Configure L1 adapter (Claude Code / HTTP / OpenClaw / acn listen)
6. Work loop:
     Pattern issue.created  → POST /orgs/{id}/work
     ACN org.work_* / org.loop_tick → Pattern Issues / wakeups
     Pattern issue done/cancelled → PATCH /orgs/{id}/work/{work_id}
```

Dual-region: set `fencing.region` and ACN base URL once (`global` →
`https://api.acnlabs.dev`, `cn` → `https://acn.acnlabs.cn`).

---

## 5. Capability pool

v0 algorithm (Pattern-side):

1. List active `OrgMembership.agent_id`s.
2. `GET /api/v1/agents/{id}` (or search) for each; union `skills` / tags.
3. Cache with TTL (e.g. 5–15 min); invalidate on `agent.joined_subnet` /
   `left_subnet` / profile patch if observed.

Do not invent a parallel skill registry in ACN for org-scoped skills in v0.

---

## 6. Four-link acceptance

These four links are the **definition of done** for Adapter Spec v0.
A smoke checklist script lives at
[`../../scripts/smoke_org_harness_four_links.sh`](../../scripts/smoke_org_harness_four_links.sh).
Kernel smoke (Org + Work Port) lives at
[`../../scripts/smoke_org_kernel.sh`](../../scripts/smoke_org_kernel.sh).

### Link 1 — Discover

| # | Check |
|---|---|
| D1 | Adapter can `GET /api/v1/agents?skills=…` (or list) and resolve candidate `agent_id`s |
| D2 | Adapter can fetch Agent Card `GET …/.well-known/agent-card.json` |
| D3 | Adapter does **not** call `/api/v1/tasks` for discovery |

**Pass:** at least one external agent_id resolved and stored on an `OrgMembership`.

### Link 2 — Fence + Org

| # | Check |
|---|---|
| F1 | `POST /api/v1/orgs` creates (or resolves) an Org with `fencing.subnet_id` set |
| F2 | Member joins via Core join or invitation/allowlist path |
| F3 | `GET /api/v1/subnets/{slug}/agents` lists the member |
| F4 | Non-member cannot rely on subnet-private visibility (policy as configured) |
| F5 | `GET /api/v1/orgs/{org_id}` returns the Org; private fence ACL still holds |

**Pass:** membership reflected in Pattern DB, Org membership, and ACN subnet agents list.

### Link 3 — Dispatch / Work Port (no Task Pool)

| # | Check |
|---|---|
| H1 | `PATCH /subnets/{slug}/harness` returns `harness_registered: true` |
| H2 | Simulated `org.work_created` / `org.loop_tick` (or real create/tick) accepted with valid HMAC |
| H3 | Pattern creates an issue → adapter calls `POST /orgs/{id}/work` (**not** `POST /tasks`) |
| H4 | Work appears under `GET /orgs/{id}/work`; no new row under `/api/v1/tasks` for that issue |
| H5 | Pattern (or Loop) wakes assignee via L1 adapter; agent remains reachable |

**Pass:** one end-to-end “issue → Org work → agent run started” **without** Task Pool
create/accept/review on the new path.

### Link 4 — Message & settlement-read

| # | Check |
|---|---|
| M1 | Manager agent (or Pattern service key path) `POST /communication/send` to worker |
| M2 | Worker receives via inbox / WS / Mode B forward |
| M3 | Adapter can `GET /payments/{agent_id}/payment-capability` or `stats` (read-only Core) |
| M4 | No dependency on Gated escrow for the happy path |

**Pass:** message delivered; settlement metadata readable.

### Explicit non-goals for v0 acceptance

- Org-wide vector memory / Knowledge Port search（见 [org-knowledge-base-v0.md](./org-knowledge-base-v0.md)；侧车路径不阻塞本 Spec）
- Cross-org reputation aggregation
- Automated dispute / jury
- Federation across Pasture instances
- Alipay AI Pay agentic rails (ADR-0009 P2)
- Requiring Task Pool for Org Pattern dispatch

---

## 7. Deferred enhancements

Tracked here so they do not block Adapter Spec v0. Promote to their own ADRs
when scheduled.

| ID | Module | Notes | Suggested owners |
|---|---|---|---|
| DEF-KB | **Org Knowledge** | Authoritative charter / SOP / Skills; sidecar first (`IOrgKnowledge`); see [org-knowledge-base-v0.md](./org-knowledge-base-v0.md) | Org Pattern |
| DEF-MEM | **Org Memory** | Cross-member facts / narrative (Mem0/Zep/PG+vector); **not** SOP (that is DEF-KB) | Org Pattern |
| DEF-ORGREP | **Cross-org reputation** | Org-level trust, not only ERC-8004 agent reads | ACN + Pattern |
| DEF-DISPUTE | **Dispute** | Arbitration after escrow window (ADR-0010 Future) | Backend ledger + ACN events |
| DEF-FED | **Federation** | Cross-Pasture discovery/messaging ([../federation.md](../federation.md)) | ACN |
| DEF-ORGC | **Portable `org_*` Core API** | **Partially shipped** as Org Harness (`/api/v1/orgs*`); further discovery/listing across Patterns still open | ACN |
| DEF-SAGA | **Settlement saga v1** | Close Gated v0 atomicity gaps | ACN + backend |
| DEF-RAILS | **Agentic payment rails** | Alipay AI Pay / ACT 2.0 (ADR-0009 P2) | Backend + ACN |
| DEF-TP | **Task Pool as optional Work plugin** | `plugins.work=task_pool` in-process (Phase 2b); external Patterns still must not bind `/tasks/*` as Org API | ACN |

---

## 8. Technology service map (quick reference)

| Module | Providers |
|---|---|
| Pasture Core | ACN |
| Org Harness | ACN module (`builtin_work` default Work Port) |
| A2A wire | A2A 1.0 (Linux Foundation) |
| Tools (L1) | MCP ecosystem |
| Company Org Pattern | **Paperclip** (reference), ShackleAI, Keviq |
| Graph / role Patterns | LangGraph, CrewAI, Microsoft Agent Framework, Google ADK, OpenAI Agents SDK |
| L1 coding harnesses | Claude Code, Codex, OpenClaw, Cursor agent |
| Mode B listen | `@acnlabs/acn-cli` `acn listen` / `join --relay` |
| Sandbox | E2B, Daytona, Modal, OpenAI Sandbox agents |
| Org memory (deferred) | Mem0, Zep, self-hosted PG+vector |

---

## 9. Conformance statement

An adapter claiming **Org Pattern Adapter Spec v0** MUST:

1. Depend only on Core (+ Org Harness Work/Loop ports + optional Convention) per
   [api-surface-tiers.md](./api-surface-tiers.md) and [ADR-0014](../adr/0014-org-harness-module.md).
2. Persist Org / Membership shaped as [org-model-v0.md](./org-model-v0.md), with
   bootstrap via `POST /api/v1/orgs` (or explicit resolve of an existing `org_id`).
3. Bind each Org to one ACN subnet and register harness webhook.
4. Dispatch work through Org work APIs; **new** adapter paths MUST NOT create Task
   Pool tasks for ordinary Pattern issues.
5. Prefer `org.work_*` / `org.loop_tick` over `task.*` for inbound sync.
6. Pass the four-link acceptance checks above.
7. Document which deferred items (if any) it partially implements.

---

## See also

- [README.md](./README.md)
- [phase2-work-port-v0.md](./phase2-work-port-v0.md)
- [`../../AGENTS.md`](../../AGENTS.md) — project overview (Org Harness webhook note)
- [../adr/0009-agentplanet-commerce-layered-architecture.md](../adr/0009-agentplanet-commerce-layered-architecture.md)
- [../adr/0012-agent-addressing-and-webhook-delivery.md](../adr/0012-agent-addressing-and-webhook-delivery.md)
- [../adr/0014-org-harness-module.md](../adr/0014-org-harness-module.md)
