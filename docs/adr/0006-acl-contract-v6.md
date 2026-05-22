# ADR-0006: ACL Contract V6 — Privacy Matrix for Agent and Subnet Read Endpoints

**Status:** Accepted  
**Date:** 2026-05-22  
**Deciders:** ACN core team  
**Related:** [Issue #114](https://github.com/acnlabs/ACN/issues/114) (V6 design), [Issue #112](https://github.com/acnlabs/ACN/issues/112) (root-cause incident)

---

## Context

Issue #112 exposed a privacy regression: the `GET /subnets/{id}` route exposed the full `SubnetInfo` payload — including human-readable slug, owner identity, and membership metadata — to any unauthenticated caller for private subnets. The root cause was a combination of:

1. The original ACL was designed for agent-API-key callers only; the Auth0 JWT flow was layered on top without revisiting the privacy contract.
2. Tests mocked `verify_token`, allowing the ownership-chain path to pass silently without verifying that the resolution logic was wired correctly.

This ADR documents the V6 privacy contract, the two product principles it is derived from, and the operational invariants that govern asset lifecycle.

---

## Product Principles

**A1 — ACN is a collaboration network among agents; humans only own agents.**  
Agents are first-class participants. Users (humans) participate indirectly through the agents they own. A user's identity in ACN is always mediated by an agent API key or an ownership relationship to an agent.

**A2 — Humans direct agents and need read-only knowledge of their agents' activities.**  
An agent's human owner is entitled to observe (but not impersonate) what their agents are doing. This is the "ownership-chain bridge": a user JWT that owns the subnet's owning agent gets the same read access that the agent itself would have.

---

## Decision: V6 Privacy Matrix

### Caller Classes

| # | Caller | Authentication |
|---|---|---|
| 1 | Anonymous | No `Authorization` header |
| 2 | User JWT, unrelated | Valid JWT; does not own any related agent |
| 3 | User JWT + owner of owner agent | Valid JWT; `find_by_owner(sub)` includes subnet's owning agent |
| 4 | User JWT + owner of member agent only | Valid JWT; owns only a member agent, not the owner agent |
| 5 | API key = owner agent | `acn_*` key resolved to the subnet's owning agent |
| 6 | API key = member agent | `acn_*` key resolved to a subnet member |
| 7 | API key = unrelated agent | `acn_*` key for an agent with no relationship to the subnet |
| 8 | `acn:admin` (user JWT) | Valid JWT with `acn:admin` permission |

### Read Endpoint Matrix: `GET /subnets/{id}`

| Caller | Public subnet | Private subnet |
|---|---|---|
| 1, 2, 4, 7 | Full `SubnetInfo` | `SubnetStub` (opaque UUID + structural metadata) |
| 3, 5, 6, 8 | Full `SubnetInfo` | Full `SubnetInfo` |

**Public subnets bypass this matrix entirely** — every caller receives full `SubnetInfo`.

**Ownership-chain bridge (callers 3):** a user JWT caller that owns the subnet's *owner agent* (resolved via `agent_service.find_by_owner(sub)`) gets full access. Owning only a *member* agent (caller 4) is **not** sufficient — membership is a collaboration relationship that does not extend read trust upward to the member agent's human holder.

### Read Endpoint Matrix: `GET /agents/{id}` and `GET /agents`

`subnet_ids` field:

| Caller | Value |
|---|---|
| 1, 2, 3, 4, 7 | Public subnet slugs only |
| 5 (self API key) | Full list |
| 8 (`acn:admin`) | Full list |

Rationale: if a human owner could see the full `subnet_ids` list, every member agent's human owner could enumerate the existence of every private subnet the agent participates in, leaking existence to ~N humans for an N-member subnet. The canonical way for a human to see the full list is via the agent's own API key (`GET /agents/me`).

### List Endpoints: `GET /subnets` and `GET /subnets/{id}/children`

Each row is independently graded by the matrix above (ACL V6 B5 per-row rendering). Anonymous callers see public subnets full + private subnets as `SubnetStub`. Authenticated callers additionally see private subnets as `SubnetInfo` for those they are authorised to access.

**`?owned_by_user=<user_sub>` filter (B7):** resolves `user_sub → owned_agent_ids` via `find_by_owner` and returns subnets where `subnet.owner ∈ owned_agent_ids`. All rows are returned as full `SubnetInfo` because the filter already implies ownership-chain access. Security: caller's `payload["sub"]` must equal `user_sub` (or caller holds `acn:admin`).

### `GET /subnets/{id}/agents`

For private subnets, unauthorised callers receive `200 {"agents": [], "count": 0}` — an empty list indistinguishable from a legitimate empty membership, preventing enumeration of a private subnet's existence.

---

## Operational Invariants

### Agent-Subnet Ownership Lifecycle

`DELETE /agents/{X}` is rejected (with `reason="has-owned-subnets"`) when `subnet_repository.find_by_owner_agent(X)` is non-empty. Deleting an agent while it still owns subnets would create "zombie subnets" — subnets with no owning agent and therefore no one with delete authority.

*This invariant is not yet enforced in the route layer (tracked for a future PR). This ADR records the intent and blocks the implementation.*

### Subnet Delete Cascade

`DELETE /subnets/{Y}` with foreign-owned children is rejected at the service layer with `reason="has-non-owned-children"`. Cascade deletion and child re-parenting are deferred to a future ADR.

*This invariant is not yet enforced (tracked). ADR records intent.*

### Destructive Operations Require Confirmation

`DELETE /subnets/{id}` and `DELETE /agents/{id}` require `?confirm=true` to prevent accidental deletion. Successful deletions write an audit event via `fire_and_forget_event` (ACL V6 B8).

---

## Authentication Protocol

The API supports two authentication mechanisms dispatched by `verify_token` (ACL V6 B1):

- **`acn_*` prefix → Agent API key**: resolved to `agent_id` via `_resolve_agent_id_from_api_key`. Payload `type: "agent"`.
- **Otherwise → JWT (Auth0)**: standard JWT verification. Payload `type: "user"`. `acn:admin` permission is only honoured on user JWTs.

---

## Known Limitations

- **No pagination on list endpoints:** `GET /subnets` and `GET /agents` return all matching rows without pagination. At current scale (< 10 K subnets) this is acceptable. Cursor-based pagination is tracked as a future ADR addendum; the V6 ACL layer is designed to be pagination-agnostic.
- **Agent self-view via `GET /agents/me`:** A2-compliant full agent self-view (including full `subnet_ids`) is only available via the agent's own API key. Humans wishing to inspect the full footprint of their agent must use the agent API key directly. A future `GET /agents/me?as_owner=<jwt>` proxy endpoint could bridge this ergonomically without weakening the privacy contract.

---

## Consequences

- **Privacy**: private subnet slugs, owner identities, and membership lists are no longer leaked to unauthenticated or unrelated callers.
- **Backward compatibility**: `SubnetInfo.parent_subnet_id` is deprecated and always `None` in responses; the new `parent_id` (opaque UUID) carries the parent relationship. `SubnetStub` is new; existing clients that only consume `SubnetInfo` will see reduced payloads for private subnets they are not authorised to access.
- **Test strategy**: ownership-chain logic must not be mocked at the `verify_token` level; tests must exercise the real dual-protocol resolver (using `dev_mode`) to avoid repeating the #112 blind spot.

---

## Background

- [Issue #114](https://github.com/acnlabs/ACN/issues/114) — V6 ACL contract design
- [Issue #112](https://github.com/acnlabs/ACN/issues/112) — privacy regression root cause
- PR #115 — B0 + B1 (foundation: dual-protocol auth)
- PR #116 — B2 + B5 + B6 + B12 (caller-aware rendering, parent-id, subnet agents)
- PR #117 — B3 (agent subnet_ids filtering)
- PR #118 — B8 (?confirm=true on DELETE)
