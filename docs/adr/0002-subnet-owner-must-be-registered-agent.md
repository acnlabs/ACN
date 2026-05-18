# ADR-0002: `subnets.owner` must reference a registered ACN agent

- **Status**: Accepted — new code path enforced; existing `backend@internal`
  rows to be migrated by `agentplanet/backend` (tracked in
  acnlabs/ACN#48)
- **Date**: 2026-05-18
- **Decision drivers**: implicit FK integrity, ops tooling simplicity,
  protocol-surface consistency

## Context

ACN's `subnets` table has an `owner` column that is intended to be a
logical foreign key to the `agents` table.  This is implicit — the DB
schema has no `FOREIGN KEY` constraint — but every ownership-derived
query (ACL checks, dual-store cleanup, audit tooling) relies on
`owner` resolving to a live agent record.

### The `backend@internal` leak

`acn/acn/auth/middleware.py` synthesises a special JWT payload for
requests authenticated with `INTERNAL_API_TOKEN`:

```python
return {
    "sub": "backend@internal",
    "permissions": ["acn:read", "acn:write", "acn:admin"],
}
```

`backend@internal` is a **service-principal placeholder**, not a
registered agent.  At some point `agentplanet/backend` created subnets
via this path, writing `owner = "backend@internal"` into 7 `ws-*`
workspace-mirror subnets.  Consequences:

- `owner → agents` is no longer a reliable implicit FK. Every ops
  query that joins subnets with agents must special-case
  `backend@internal`.
- Per-subnet creator attribution is irrecoverable from inside ACN
  alone; cross-referencing `agentplanet/backend.workspaces.acn_subnet_id`
  is required.
- `SubnetInfo.owner` leaks the placeholder directly to API consumers
  and SDK users.

The current route `POST /api/v1/subnets` already requires
`AgentApiKeyDep` (a verified agent API key), so no **new**
`backend@internal` subnets can be created through the normal HTTP
path.  The 7 existing rows pre-date that guard.  This ADR closes the
gap at the service layer and documents the migration recipe.

## Decision

**Option A — owner = agent_id only (chosen).**

Every subnet must be owned by a row in the `agents` table.
`backend@internal` is rejected as an owner value at the service layer
(`SubnetService.create_subnet`).  Service callers (e.g.
`agentplanet/backend`) that previously created subnets via the
internal-token path **must** register a dedicated service-account
agent and create subnets through that agent's API key.

### Rejected alternatives

**Option B — owner ∈ {agent_id, service_principal, user_id}** (IAM
model).  Pros: matches what the auth middleware already half-implements.
Cons: every ownership-derived query grows a dispatch branch on identity
class; diverges from the single `AgentApiKeyDep` shape on the
user-facing path; does not actually solve the attribution problem.

**Option C — owner = agent_id; `metadata.creator_user_id` for
service calls.**  Still requires a service-account agent for the
`owner` column; adds extra complexity for metadata consumers.
Inconsistent with Option A's cleaner invariant without enough
counterbalancing benefit.

### Why Option A

- The rest of the protocol already works this way: every dual-store
  write goes through an agent identity; ADR-0001's fix models only
  the agent path.
- Closing the exception costs less than codifying it.
- `AgentApiKeyDep` is already the enforcing mechanism at the route
  layer — the service-layer guard is defence-in-depth.

## Implementation

### Service-layer guard (ADR-0002 §A.1)

`SubnetService.create_subnet` raises `ValueError` when
`owner == "backend@internal"`:

```python
if owner == "backend@internal":
    raise ValueError(
        "ADR-0002: 'backend@internal' is not a valid subnet owner; "
        "register a service-account agent and create subnets through "
        "that agent's api key."
    )
```

This is defence-in-depth: the route already requires `AgentApiKeyDep`
so the guard should never trigger in production.  Its value is
catching internal-test callers and future code changes that bypass
the route layer.

### Service-account agent conventions

Service callers that need to own subnets must:

1. **Register** a dedicated agent via `POST /api/v1/agents/register`.
   Recommended naming convention:
   ```
   name:        "svc-<service>-<env>"   (e.g. "svc-backend-prod")
   agent_type:  "service"
   description: "Service-account agent for <service> (<env>)"
   ```
2. **Store** the returned `api_key` as a secret in the service's
   secret manager (rotation: `PATCH /api/v1/agents/{id}/rotate-key`).
3. **Create subnets** by passing the agent's API key as the
   `Authorization: Bearer <api_key>` header — same as any other
   agent call.

### Migration recipe for existing `backend@internal` rows

The 7 `ws-*` subnets must be migrated by `agentplanet/backend` once a
service-account agent is registered.  Steps:

```sql
-- 1. Verify the rows to migrate (run in read-only first)
SELECT subnet_id, name, owner, created_at
FROM subnets
WHERE owner = 'backend@internal'
ORDER BY created_at;

-- 2. Pick the registered service-account agent ID
--    (replace <svc-agent-id> with the real UUID from agents table)

-- 3. Migrate owners
UPDATE subnets
SET owner = '<svc-agent-id>'
WHERE owner = 'backend@internal';

-- 4. Dual-store backfill — add subnet_ids to the service-account agent
--    (Redis SADD, or use the ACN internal migration endpoint if available)
--    This mirrors the pattern in ADR-0001 §Migration.

-- 5. Verify no orphans remain
SELECT COUNT(*) FROM subnets WHERE owner = 'backend@internal';
-- Expected: 0
```

The dual-store backfill (step 4) follows the same recipe as ADR-0001's
cleanup: `SADD agent:{svc-agent-id}:subnets <subnet-id>` for each
migrated row.

## Invariant coverage

| Layer | Enforcement |
|-------|------------|
| DB schema | No hard FK (unchanged — adding FK would block migration) |
| Route layer | `AgentApiKeyDep` requires a valid `acn_*` API key; `backend@internal` cannot appear |
| Service layer | `SubnetService.create_subnet` rejects `backend@internal` explicitly (ADR-0002 §A.1) |
| Ops tooling | `SELECT … WHERE owner = 'backend@internal'` is the migration health check |

The existing `do_join_subnet` dual-store invariant (ADR-0001) covers
service-account agents automatically — they go through `AgentApiKeyDep`
like any other agent, so no special branching is needed.

## Consequences

### Positive

- `owner → agents` is a reliable implicit FK once migration is
  complete.  Ops queries need no `backend@internal` special-case.
- `SubnetInfo.owner` exposes a meaningful agent UUID to API consumers.
- Protocol surface stays one-shape: agent API key for everything.

### Negative / risks

- `agentplanet/backend` must register a service-account agent and
  rotate its secret through the standard key-management process —
  one-time operational overhead.
- The 7 `ws-*` rows carry `backend@internal` until migration is done.
  The service-layer guard does not reject reads; existing rows are safe
  to query and serve, just not to clone.

## Related

- ADR-0001: subnet creator must be a member (dual-store consistency)
- Issue #48: tracks this ADR and the `agentplanet/backend` migration
- `acn/acn/auth/middleware.py:397-438`: internal-token payload synthesis
- `acn/acn/routes/subnets.py`: `AgentApiKeyDep` enforcement point
