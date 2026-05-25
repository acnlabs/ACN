# ADR-0005: Subnet ownership transfer

- **Status**: Implemented (2026-05-25)
- **Date**: 2026-05-25
- **Implemented**: 2026-05-25
- **Decision drivers**: prevent orphan subnets when an owner goes dark,
  minimal new surface area, consistent with existing ownership-transfer
  pattern on `Agent` entities, defence-in-depth against reserved
  identity escalation.

## Context

ADR-0004 §Security considerations and §Out of scope explicitly seed this
ADR:

> **Orphan subnets.** If a subnet owner's agent is deleted or its API
> key is revoked, every owner-only endpoint returns 403. Any pending
> join\_request / invitation rows stay `pending` indefinitely…
> Permanent resolution is deferred to the **ownership transfer ADR**.

Without a transfer primitive, subnet operators facing planned turnover
(employee offboarding, service key rotation, agent retirement) have no
path to hand off ownership before going dark. Every ADR-0004 owner-only
capability (approve join requests, send invitations, manage allowlist,
register Org Harness) becomes permanently unreachable the moment the
original owner's key is revoked. The subnet must be deleted and
recreated under a new owner — losing all membership history, pending
requests, and allowlist configuration — or it becomes an untended
orphan.

ACN already exposes a transfer-ownership concept for agents (via service
key rotation and re-registration), so a subnet analogue is a natural
extension. The scope is narrow: one `POST` endpoint, one service method.

## Considered options

### A. `POST /subnets/{id}/transfer` owner-driven endpoint (chosen)

The current owner authenticates via their existing API key and submits
the new owner's `agent_id` in the request body. ACN validates the
transfer, updates the subnet entity, automatically adds `new_owner` to
the member set, and returns the updated `SubnetInfo`. The previous owner
retains membership but loses all owner-only privileges.

**Pros**

- Single endpoint, single service method. Minimal new surface area.
- Consistent with the design principle "owner is the authority on their
  own subnet" — the transfer is owner-initiated and owner-authenticated.
- The new owner becomes a member automatically (idempotent), preserving
  the ADR-0001 invariant that the owner is always a member.
- Works for planned handoffs (offboarding, key rotation, agent
  migration). Does not require admin involvement.

**Cons**

- Cannot rescue a subnet whose owner key is already revoked. An
  operator who loses the key before transferring must still delete and
  recreate. The "emergency recovery" case is explicitly out of scope
  (see below) and deferred to a future admin-override ADR.

### B. Admin-initiated override (rejected)

A privileged platform admin endpoint that can set `subnet.owner` to any
registered agent without the current owner's authentication.

**Rejected** because it requires a separate admin-authentication scheme
(outside the current `AgentApiKeyDep` model) and introduces an asymmetric
power that is hard to audit. The current service design has no
admin-identity concept; introducing one for a narrow recovery case adds
complexity disproportionate to the gain. Operators who lose the owner
key can recover by deleting and recreating the subnet; the data loss is
acceptable at v0.x. An admin override can be added in a follow-up ADR
once an admin-auth primitive exists.

### C. Custody escrow / multi-party approval (rejected)

A transfer that requires both the current owner and the new owner to
sign off, or that uses a time-locked escrow, to prevent hostile
takeover via a stolen key.

**Rejected** as over-engineering for v0.x. ACN's trust model places
the owner's API key as the singular authority for all owner operations;
adding a second-factor specifically for transfer would require a new
confirmation mechanism not yet in scope. The security posture is
consistent with other owner-only destructive operations (e.g. `DELETE
/subnets/{id}`, `PATCH /subnets/{id}/harness`) which also require only
the owner's API key.

## Decision

Adopt option A. Implement `POST /api/v1/subnets/{id}/transfer` backed by
`SubnetService.transfer_owner`.

## Business rules

The following rules are enforced in order (earlier rules short-circuit
later ones):

1. **Owner-only** — `current_owner` (authenticated via `AgentApiKeyDep`)
   must equal `subnet.owner`. `PermissionError` → `403 OWNERSHIP_MISMATCH`.
2. **Reserved subnets** — `subnet_id ∈ {"public", "system"}` cannot be
   transferred. `PermissionError` → `403 OWNERSHIP_MISMATCH`.
3. **Self-transfer rejected** — `new_owner == current_owner` is a no-op
   that offers no operational value and likely indicates a client error.
   `ValueError` → `400 INVALID_REQUEST`.
4. **ADR-0002 guard (unconditional)** — `new_owner == "backend@internal"`
   is always rejected. The `backend@internal` identity is a routing
   placeholder for internal service calls (ADR-0002); it cannot own any
   subnet. This guard runs regardless of whether `agent_repository` is
   wired, providing defence-in-depth against callers that bypass the
   route layer.
5. **`"system"` identity guard (unconditional)** — `new_owner == "system"`
   is always rejected. `"system"` is the platform identity that owns the
   two reserved subnets; no other subnet may be transferred to it. Same
   unconditional semantics as the ADR-0002 guard.
6. **Registered-agent check (conditional)** — when `agent_repository` is
   wired, `new_owner` must correspond to a registered agent.
   `ValueError` → `400 INVALID_REQUEST`. When the repo is absent (legacy
   Redis-only fixtures), this check is skipped; rules 4 and 5 still
   apply.
7. **Member set update** — `new_owner` is added to `subnet.member_agent_ids`
   via an explicit set copy (not via `Subnet.add_member`; see
   §Child-subnet note). `dataclasses.replace` is used to avoid mutating
   the original entity.

**Child-subnet note (ADR-0003)**: when transferring a child subnet, the
`new_owner` is added to the child's member set by direct `set.add`,
intentionally bypassing `add_member`'s parent-membership check. This is
the same deliberate bypass documented in `create_subnet` (delegated-admin
pattern): the owner may be outside the parent subnet; the membership-subset
invariant limits what they can do after the transfer (they cannot add
members who are not in the parent). See `SubnetService.create_subnet` for
the full rationale.

**Previous owner membership**: the previous owner retains membership in
the subnet after the transfer. Ownership transfer does not remove any
memberships. An owner who wishes to leave after transferring must call
`DELETE /agents/{id}/subnets/{subnet_id}` separately.

## Rate limiting

`POST /{slug}/transfer` is limited to `10/minute` per client, the
same limit applied to `POST /{slug}/promote` and
`DELETE /{slug}`. Transfer is a rare, high-stakes operation; the
limit prevents accidental rapid-fire retries without impeding normal use.

## API

```http
POST /api/v1/subnets/{slug}/transfer
Authorization: Bearer <owner_api_key>
Content-Type: application/json

{ "new_owner": "<agent_id>" }
```

**Request body**

| field | type | constraints | description |
|---|---|---|---|
| `new_owner` | `string` | `min_length=1`, `max_length=128` | Agent ID of the new subnet owner |

**Responses**

| status | `error_code` | condition |
|---|---|---|
| 200 | — | Transfer succeeded; returns updated `SubnetInfo` with `owner == new_owner` |
| 400 | `invalid_request` | Self-transfer; `backend@internal`; `system` identity; unregistered agent |
| 403 | `ownership_mismatch` | Caller is not `subnet.owner`; or subnet is a reserved system subnet |
| 404 | `subnet_not_found` | `subnet_id` does not exist |
| 422 | `validation_failed` | Missing or invalid `Authorization` header, or empty `new_owner` string |
| 500 | — | Unexpected error (details not leaked to client) |

## CLI

```bash
acn subnet transfer <subnet_id> --to <new_owner_agent_id>
```

## Error codes

No new `ErrorCode` enum members are added. The existing codes
`OWNERSHIP_MISMATCH`, `INVALID_REQUEST`, `SUBNET_NOT_FOUND`, and
`VALIDATION_FAILED` cover all transfer-specific failure modes with
`details.reason` providing discrimination where needed.

## Security considerations

**Key-already-revoked case.** If the owner's API key is revoked before
transfer, the owner can no longer authenticate and the `POST /transfer`
endpoint returns 401/422. This ADR's scope is **planned handoffs**, not
emergency recovery. A compromised or lost key requires out-of-band
operator intervention (database repair, admin override — neither is
implemented here). The failure mode is identical to losing the key for
any other owner-only operation and is considered acceptable at v0.x.

**Hostile transfer via stolen key.** If an attacker obtains the owner's
API key, they can transfer the subnet to an agent they control, gaining
full owner capabilities. The risk is equal to the existing attack surface
of any owner-only operation (delete subnet, register harness) with a
stolen key. Mitigations are out of scope for this ADR (2FA, key-signing,
audit alerts on transfers) and are noted as follow-up concerns.

**ADR-0002 defence-in-depth.** The `backend@internal` guard runs in the
service layer unconditionally, even when the transfer arrives via an
internal caller that bypasses route-layer authentication. This prevents
the routing placeholder identity from ever appearing in `subnet.owner`
regardless of call path.

## Out of scope

- **Emergency admin override** — recovering a subnet whose owner key is
  already revoked or whose owner agent is deleted.
- **Transfer confirmation by new owner** — requiring the new owner to
  accept the transfer before it takes effect (custody escrow / two-party
  sign-off).
- **Ownership history / audit trail** — a queryable log of past transfers.
  The structured log event `subnet_owner_transferred` (emitted by the
  service layer at every transfer) serves as the audit record; a
  queryable API is deferred.
- **Webhook on ownership transfer** — no new webhook event is emitted.
  Operators monitoring subnet lifecycle via webhooks should poll
  `GET /subnets/{id}` after a transfer to detect owner changes.
- **Automatic member eviction of old owner** — the previous owner is
  intentionally not removed from the member set.

## Relationship to other ADRs

- **ADR-0001** — "owner is always a member" invariant is preserved: the
  new owner is added to `member_agent_ids` as part of the transfer
  operation.
- **ADR-0002** — `backend@internal` guard is applied unconditionally at
  the service layer, matching the same guard in `create_subnet`.
- **ADR-0003** — child-subnet transfer bypasses `add_member`'s
  parent-membership check, matching the same deliberate bypass in
  `create_subnet` (delegated-admin pattern).
- **ADR-0004** — this ADR implements the "ownership transfer" item that
  was explicitly deferred in ADR-0004 §Out of scope and seeded in
  §Security considerations (`"ADR-00XX: Subnet ownership transfer"`).

## Implementation evidence

Implemented in `feat/subnet-owner-transfer` on the `acn-ts-admission`
worktree (commits `d7adb09`, `17718df`).

| file | change |
|---|---|
| `acn/services/subnet_service.py` | `transfer_owner` method (lines 1680–1815) |
| `acn/routes/subnets.py` | `TransferOwnerRequest` model + `transfer_subnet_owner` endpoint |
| `tests/routes/test_subnets_transfer.py` | 9 route-layer test cases |
| `tests/services/test_subnet_service_transfer.py` | 17 service-layer white-box test cases |
| `skills/acn/references/API.md` | `POST /{id}/transfer` endpoint added to Subnets table |
| `.claude/skills/acn/SKILL.md` | `acn subnet transfer` CLI command added |
| `CHANGELOG.md` | `[Unreleased]` entry |

### Test coverage

**Route layer** (`tests/routes/test_subnets_transfer.py`, 9 cases)

- Happy path: `200` with updated `owner`
- Non-owner: `403 ownership_mismatch`
- Missing auth: `422 validation_failed`
- Subnet not found: `404 subnet_not_found`
- Self-transfer: `400 invalid_request`
- Unregistered agent: `400 invalid_request`
- Empty `new_owner`: `422` Pydantic validation
- `backend@internal`: `400 invalid_request`
- `"system"` identity: `400 invalid_request`

**Service layer** (`tests/services/test_subnet_service_transfer.py`, 17 cases)

- Owner field updated in returned entity
- `new_owner` added to member set
- Previous owner retains membership
- `repository.save` called with updated entity
- Idempotent when `new_owner` already a member
- Agent repo wired + registered owner accepted
- Original `member_agent_ids` set not mutated (shallow-copy safety)
- Non-owner `PermissionError`
- Reserved subnet `"public"` `PermissionError`
- Reserved subnet `"system"` `PermissionError`
- Non-owner check fires before reserved-subnet check
- Self-transfer `ValueError`
- `backend@internal` `ValueError` (unconditional, no agent repo needed)
- `"system"` identity `ValueError` (unconditional)
- Unregistered agent `ValueError` (with agent repo wired)
- Existence check skipped when agent repo is `None`
- `SubnetNotFoundException` propagates on missing subnet

## References

- [ADR-0001](./0001-subnet-creator-must-be-member.md) — owner-is-member
  invariant preserved by the transfer.
- [ADR-0002](./0002-subnet-owner-must-be-registered-agent.md) —
  `backend@internal` guard applied in the transfer path.
- [ADR-0003](./0003-subnet-nesting-single-layer.md) — child-subnet
  `add_member` bypass rationale.
- [ADR-0004](./0004-subnet-join-policy.md) §Security considerations and
  §Out of scope — origin of this ADR.
- `acn/services/subnet_service.py::transfer_owner` — implementation.
- `acn/routes/subnets.py::transfer_subnet_owner` — HTTP surface.
