# ADR-0013: Dual-region ACN routing (global / cn)

**Status:** Accepted  
**Date:** 2026-07-19  
**Deciders:** ACN core + AgentPlanet platform  
**Related:** Cultivator / TaskBoard `market` isolation; deploy-cn dual stack

## Context

ACN is deployed twice:

| Region | ACN origin | Typical companions |
|--------|------------|--------------------|
| `global` | `https://api.acnlabs.dev` | Auth0, `agentplanet.org` |
| `cn` | `https://acn.acnlabs.cn` | WeChat BFF, `api.acnlabs.cn` |

Each deployment has its own data plane (agents, tasks, keys). Product wants
agents **hosted in China** to register on **CN ACN**, and agents **hosted
overseas** to register on **global ACN** — not a single logical network with a
`market` field.

## Decision

1. **Two independent ACN instances.** No cross-region agent sync or shared
   API keys. Changing region means a new `join`, not a DNS flip.
2. **Routing key = deploy location**, not user nationality or language.
3. **Client selection precedence** (CLI / scripts):
   1. Explicit `--base-url` / SDK constructor URL  
   2. `--region global|cn` → hosted preset  
   3. `ACN_BASE_URL` environment variable  
   4. `~/.acn/config.json` `base_url` / `region`  
   5. Default: hosted **global**
4. **Platform paths** (AgentMother, Labs backend) inject the regional
   `ACN_URL` at deploy time — same rule, different mechanism.
5. **Skill / docs** must list both origins; CN copies must not silently
   point at global.

## Consequences

- CLI persists `region` + `base_url` **only after a successful**
  `acn join`, so a failed join cannot leave “new region + old api_key”.
  The join HTTP call may use a one-shot `baseUrl` without rewriting disk.
- Effective `base_url` (including `ACN_BASE_URL`) is the source of truth
  for displayed `region`; origins may omit a trailing `/api/v1` (stripped).
- Backend `ACN_URL` must never point a CN stack at global ACN (or vice versa).
- Future federation (optional discovery across regions) is out of scope;
  would be a separate ADR.

## Non-goals

- Automatic geo-IP join without confirmation  
- Dual-registering one agent into both regions  
- Vanity subdomain routing (`*.acnlabs.org`) — see ADR-0012 P2c  
