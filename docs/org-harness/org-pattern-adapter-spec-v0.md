# Org Pattern Adapter Spec v0 (Paperclip → ACN Core)

**Status:** Spec v0 — external Pattern adapter contract  
**Last updated:** 2026-07-19  
**Depends on:** [design-v0.md](./design-v0.md), [api-surface-tiers.md](./api-surface-tiers.md), [org-model-v0.md](./org-model-v0.md)

> **Naming / ownership:** Canonical architecture is [design-v0.md](./design-v0.md).
> Org Harness is an **ACN module** (optional Org Owner: none/human/agent;
> agent members). This document only covers **external** Pattern adapters
> (e.g. Paperclip plugin). It does **not** mean Org lives only in Paperclip.

> Goal: let a company-style Org Pattern (Paperclip) plug into ACN Org Harness
> / Network Core — preferably via Work/Loop ports — without mistaking the
> adapter for the Org Harness module itself.
>
> **Transitional:** ACN `task.*` ↔ Paperclip issue mirroring is a **legacy bridge**.
> Target per [ADR-0014](../adr/0014-org-harness-module.md) D5/D8: Org work ports +
> `org.*` events; do not treat `/api/v1/tasks*` as the long-term Pattern API.

---

## 1. Roles

| Role | Responsibility |
|---|---|
| **ACN Network Core** | Identity, fencing, A2A, settlement-read, harness webhook delivery |
| **Org Pattern (Paperclip)** | Company, org chart, issues, budgets, heartbeat wakeups, approvals UI |
| **L1 Agent** | Executes work when woken; speaks ACN with its own key |
| **Adapter** | Glue: maps Paperclip entities ↔ ACN Core calls + verifies HMAC events |

The adapter MAY live inside Paperclip (preferred) or as a sidecar service.

---

## 2. Entity mapping

| Paperclip | Org Model v0 | ACN Core |
|---|---|---|
| `company.id` | `org_id` | — (not stored) |
| `company.name` / goal | `display_name` / `charter.mission` | subnet `name` / `description` (optional mirror) |
| `company` fence | `fencing.subnet_id` | `POST /api/v1/subnets` → slug |
| harness receiver URL | `harness_webhook.url` | `PATCH /api/v1/subnets/{slug}/harness` |
| `agent` / employee | `OrgMembership.agent_id` | registered `agent_id` |
| role / title / reports_to | `role` / `reports_to` | — (Pattern only) |
| budget | `budget` | — (Pattern only) |
| `issue` / ticket | `OrgWorkItem` | — ; optional A2A message / payment task correlation |
| heartbeat wakeup | Org Loop tick | agent `POST …/heartbeat` side effect; Pattern invokes L1 adapter |
| board approval | Org RBAC | — ; may gate join via subnet invitations |

### Anti-mappings (do not do)

| Temptation | Why not |
|---|---|
| Create an ACN task for every Paperclip issue | Couples Pattern to Reference Tier 2 |
| Use subnet membership as the only org chart | No roles / budgets / mission |
| Store Paperclip API keys in ACN | Wrong trust boundary |
| Call `/communication/internal/send` | Internal tier |

---

## 3. Wire protocol: harness webhook

When `PATCH /subnets/{slug}/harness` registers a URL, ACN POSTs signed lifecycle
events (same HMAC-SHA256 scheme as payment webhooks). Adapter MUST:

1. Verify signature with `harness_secret`.
2. Idempotently apply by event id / `(type, agent_id, subnet_id, ts)`.
3. Ignore unknown event types (forward-compatible).

### Events the adapter MUST handle (v0)

| Event | Adapter action |
|---|---|
| `agent.joined_subnet` | Ensure `OrgMembership` active; sync role defaults |
| `agent.left_subnet` | Mark membership inactive / paused |
| `task.*` (if Task Pool also used) | Optional — ignore for pure Paperclip mode |

### Events the adapter SHOULD emit into ACN (outbound)

| Pattern action | ACN call |
|---|---|
| Manager DMs worker | `POST /communication/send` |
| Need attention-fee notify | Convention: `manifest/send` (optional) |
| Read payment state | `GET /payments/tasks/…` / `stats` |

Inbound work to relay agents uses Mode B / gateway — not this webhook.

---

## 4. Bootstrap sequence

```
1. Operator creates Paperclip company (org_id, charter.mission)
2. Adapter creates ACN subnet:
     POST /api/v1/subnets
     { "id": "<slug>", "name": "...", "join_policy": "approval", ... }
3. Adapter registers webhook:
     PATCH /api/v1/subnets/<slug>/harness
     { "harness_url": "https://…/hooks/acn", "harness_secret": "…" }
4. For each employee agent:
     a. Ensure agent registered on ACN (or discover existing agent_id)
     b. Invite / allowlist / join subnet (admission Core APIs)
     c. Create OrgMembership { role, reports_to, budget }
     d. Configure L1 adapter (Claude Code / HTTP / OpenClaw / acn listen)
5. Org Loop starts: issues → heartbeat wakeups → agents work → comments/audit
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

### Link 1 — Discover

| # | Check |
|---|---|
| D1 | Adapter can `GET /api/v1/agents?skills=…` (or list) and resolve candidate `agent_id`s |
| D2 | Adapter can fetch Agent Card `GET …/.well-known/agent-card.json` |
| D3 | Adapter does **not** call `/api/v1/tasks` for discovery |

**Pass:** at least one external agent_id resolved and stored on an `OrgMembership`.

### Link 2 — Fence

| # | Check |
|---|---|
| F1 | `POST /api/v1/subnets` creates slug bound as `fencing.subnet_id` |
| F2 | Member joins via Core join or invitation/allowlist path |
| F3 | `GET /api/v1/subnets/{slug}/agents` lists the member |
| F4 | Non-member cannot rely on subnet-private visibility (policy as configured) |

**Pass:** membership reflected both in Pattern DB and ACN subnet agents list.

### Link 3 — Dispatch / heartbeat

| # | Check |
|---|---|
| H1 | `PATCH /subnets/{slug}/harness` returns `harness_registered: true` |
| H2 | Simulated `agent.joined_subnet` (or real join) is accepted with valid HMAC |
| H3 | Pattern creates an issue, wakes assignee via L1 adapter (CLI/HTTP/relay) |
| H4 | Assignee agent remains reachable (`heartbeat` or authenticated call) |

**Pass:** one end-to-end “issue assigned → agent run started” without using Task Pool.

### Link 4 — Message & settlement-read

| # | Check |
|---|---|
| M1 | Manager agent (or Pattern service key path) `POST /communication/send` to worker |
| M2 | Worker receives via inbox / WS / Mode B forward |
| M3 | Adapter can `GET /payments/{agent_id}/payment-capability` or `stats` (read-only Core) |
| M4 | No dependency on Gated escrow for the happy path |

**Pass:** message delivered; settlement metadata readable.

### Explicit non-goals for v0 acceptance

- Org-wide vector memory / SOP search
- Cross-org reputation aggregation
- Automated dispute / jury
- Federation across Pasture instances
- Alipay AI Pay agentic rails (ADR-0009 P2)

---

## 7. Deferred enhancements

Tracked here so they do not block Adapter Spec v0. Promote to their own ADRs
when scheduled.

| ID | Module | Notes | Suggested owners |
|---|---|---|---|
| DEF-MEM | **Org Memory / SOPs** | Cross-member memory, playbooks; Pattern-local store (Mem0/Zep/PG+vector) or Skills packs | Org Pattern |
| DEF-ORGREP | **Cross-org reputation** | Org-level trust, not only ERC-8004 agent reads | ACN + Pattern |
| DEF-DISPUTE | **Dispute** | Arbitration after escrow window (ADR-0010 Future) | Backend ledger + ACN events |
| DEF-FED | **Federation** | Cross-Pasture discovery/messaging ([../federation.md](../federation.md)) | ACN |
| DEF-ORGC | **Portable `org_*` Core API** | Only if multiple Patterns need shared org discovery on-pasture | ACN (v1+) |
| DEF-SAGA | **Settlement saga v1** | Close Gated v0 atomicity gaps | ACN + backend |
| DEF-RAILS | **Agentic payment rails** | Alipay AI Pay / ACT 2.0 (ADR-0009 P2) | Backend + ACN |

---

## 8. Technology service map (quick reference)

| Module | Providers |
|---|---|
| Pasture Core | ACN |
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

1. Depend only on Core (+ optional Convention) per [api-surface-tiers.md](./api-surface-tiers.md).
2. Persist Org / Membership shaped as [org-model-v0.md](./org-model-v0.md).
3. Bind each Org to one ACN subnet and register harness webhook.
4. Pass the four-link acceptance checks above.
5. Document which deferred items (if any) it partially implements.

---

## See also

- [README.md](./README.md)
- [`../../AGENTS.md`](../../AGENTS.md) — project overview (Org Harness webhook note)
- [../adr/0009-agentplanet-commerce-layered-architecture.md](../adr/0009-agentplanet-commerce-layered-architecture.md)
- [../adr/0012-agent-addressing-and-webhook-delivery.md](../adr/0012-agent-addressing-and-webhook-delivery.md)
