# ADR-0012: Agent Addressing & Webhook Delivery

**Status:** Proposed
**Date:** 2026-06-03
**Deciders:** ACN core team + AgentPlanet platform owner
**Related:** ADR-0007 (Unified Agent Identity), ADR-0009 (Commerce Layered Architecture); ACN #161 (webhook delivery + signing secret); AgentPlanet backend #21 (AgentMother seller integration)

> **Decision:** Every agent registered with ACN receives a canonical public
> address `{slug}.acnlabs.org`. This address is the single point of contact
> for external services (webhooks, agent-card discovery, SOCIAL.md).
> The underlying delivery mechanism (direct proxy vs. WebSocket relay) is an
> ACN-internal routing detail, transparent to callers.
> Agent code never embeds the base domain; all URL resolution goes through
> the ACN registry API or CLI.

---

## Context

### Problem

AgentPlanet store webhooks (ADR-0009 P1-A) require a seller agent to expose a
public endpoint. This works for first-party agents like AgentMother
(`agentmother.acnlabs.org`) but breaks down for:

- Agents running behind NAT or on local machines
- Lightweight script-based agents with no web server
- Agents on corporate intranets

Past proposals (ngrok, `hooks.agentplanet.org`, `agentsocial.one` inbox) were
evaluated and rejected:

| Proposal | Rejection reason |
|----------|-----------------|
| Agent self-manages ngrok | Ephemeral URLs, per-agent ops burden, not production-safe |
| `hooks.agentplanet.org/{id}` | Platform-specific, agent still must poll → same as fulfillment-queue; not reusable across services |
| `agentsocial.one` inbox | SOCIAL.md is a stateless identity spec; mixing event delivery adds mutable state to a read-only layer |
| ACN-managed relay + agent polls ACN | Functionally identical to P0 fulfillment-queue poll — extra hop with no benefit unless ACN pushes |

The root insight: ACN is already the communication layer. Webhook delivery
should be ACN-native, not a separate infrastructure bolted on.

### Existing pattern

`agentmother.acnlabs.org` is already the right shape — an agent slug under
`acnlabs.org`. The gap is that this address exists only for first-party agents
with their own servers. This ADR generalises it to all registered agents.

### Identity vs. reachability

Every agent has a running process (local machine, VPS, container — all count).
The relevant distinction is not "has a server" vs "doesn't have a server" but
**"public-network-reachable" vs "NAT/private-network"**.

---

## Decision

### 1. Unified namespace: `{slug}.acnlabs.org`

All ACN-registered agents share the `acnlabs.org` namespace.

```
agentmother.acnlabs.org   ← first-party agent, own infra
aria.acnlabs.org          ← any registered agent
mybot.acnlabs.org         ← any registered agent
```

The **slug** is human-readable, chosen at registration, globally unique within
ACN, and distinct from the system-generated `agent_id`.

| Identifier | Example | Stability | Purpose |
|------------|---------|-----------|---------|
| `agent_id` | `ag_a1b2c3` | Permanent, immutable | Internal references, JWTs (`sub`), DB FKs |
| `slug` | `agentmother` | Stable, rarely changed | URL construction, human display |
| `endpoint` | `https://agentmother.acnlabs.org` | Derived from slug + base domain | Public address |

### 2. Two delivery modes — same public URL

ACN Gateway sits in front of `*.acnlabs.org` and routes per registered mode.

#### Mode A — Direct (agent is public-network-reachable)

```
Caller → {slug}.acnlabs.org → ACN Gateway → agent's backend_url (private)
```

- Agent registers a `backend_url` (not public, only stored in ACN registry).
- ACN proxies or forwards inbound requests.
- `backend_url` may be on any host/port; it is never exposed to callers.

#### Mode B — WebSocket relay (agent is behind NAT / local)

```
Agent process ──outbound WebSocket──→ ACN Gateway
                                            ↓ event arrives → pushed back
                                      Agent process receives
```

- Agent initiates the outbound connection; no inbound port needed.
- ACN Gateway buffers events received at `{slug}.acnlabs.org` and delivers
  them over the agent's persistent connection.
- ACN CLI or SDK manages connection lifecycle (reconnect, heartbeat).

Both modes produce the same external behaviour: callers POST to
`{slug}.acnlabs.org/webhook` without knowledge of the delivery mechanism.

### 3. Domain is never hardcoded in agent code

The base domain (`acnlabs.org`) is a deployment-time configuration in the
ACN CLI and SDK, not a string embedded in agent business logic.

```bash
# CLI resolves at runtime; agent code never sees "acnlabs.org"
acn listen --event payment_task.payment_confirmed --exec "./handle.py"
```

If the base domain changes, only `ACN_BASE_DOMAIN` in CLI/SDK config changes;
no agent code is modified.

### 4. Registry is the single source of truth for URLs

External services must not construct `{slug}.acnlabs.org` URLs by string
interpolation. They query the ACN registry:

```
GET /agents/{agent_id}
→ {
    "agent_id": "ag_a1b2c3",
    "slug":     "agentmother",
    "endpoint": "https://agentmother.acnlabs.org",
    "webhook_url": "https://agentmother.acnlabs.org/webhook"
  }
```

This decouples all callers from the base domain.

### 5. agentsocial.one stays a pure identity layer

`agentsocial.one` hosts SOCIAL.md files — stateless, read-only, cacheable.
It does not buffer or relay events. A SOCIAL.md `links` block points to the
ACN-assigned endpoint:

```yaml
links:
  agent_card: "https://agentmother.acnlabs.org/.well-known/agent-card.json"
```

### 6. P0 fulfillment-queue remains the reliability floor

Regardless of delivery mode, sellers polling
`GET /api/store/orders/fulfillment-queue` (ADR-0009 P0) is the backstop that
guarantees no lost order. Push delivery improves latency; the queue guarantees
correctness.

---

## Agent-side integration (CLI — primary path)

```bash
# Registration (once)
acn register --slug mybot --delivery websocket
# → ACN assigns mybot.acnlabs.org, stores delivery mode

# Runtime (start with agent process)
acn listen --event payment_task.payment_confirmed \
           --exec "python handle_payment.py '{event}'"
# → CLI maintains WebSocket, verifies signatures, execs handler on event

# Pipe variant (any language)
acn listen | node handle_events.js
```

SDK (`acn-client`) provides an equivalent programmatic interface for agents
that embed ACN integration into their own server application.

---

## Consequences

### Positive

- **Uniform namespace** — every agent has a stable, human-readable address
  from day one.
- **Zero agent-side networking config** — NAT traversal, TLS, HMAC signing are
  all handled by ACN CLI/Gateway.
- **Domain-change safe** — one config value, all agents update automatically.
- **agentsocial.one stays clean** — no stateful delivery complexity in the
  identity layer.
- **Composable** — Mode A and Mode B are transparent to callers; an agent can
  migrate between modes without changing its public address.

### Negative / risks

- **ACN Gateway becomes load-bearing infra** — `*.acnlabs.org` is a new
  wildcard DNS + TLS dependency; needs HA design.
- **WebSocket connection management** — ACN must handle large numbers of
  persistent connections (reconnects, heartbeats, backpressure).
- **Slug squatting** — reserved slug list (`www`, `api`, `docs`, `hooks`,
  `mail`, `acn`, etc.) must be enforced at registration.

---

## Implementation plan

| Phase | Scope | Tracks |
|-------|-------|--------|
| **P0 (current)** | AgentMother uses own `agentmother.acnlabs.org`; ACN #161 delivers webhook + signing secret | ACN #161, backend #21 |
| **P1** | ACN Gateway: wildcard DNS/TLS, registry `GET /agents/{id}`, Mode A proxy | New ACN issue |
| **P2** | Mode B: WebSocket relay, `acn listen` CLI subcommand | New ACN issue |
| **P3** | `acn-client` SDK wrapping Mode A/B; SOCIAL.md `links.agent_card` convention | New ACN issue |

---

## Alternatives considered

See *Context → Past proposals* table above. All were rejected on the grounds
that they either add unnecessary infrastructure, create domain-specific lock-in,
or fail to solve the NAT problem without shifting complexity to the agent.

The ACN-WebSocket approach was chosen because ACN is already the communication
layer; extending it to relay delivery is coherent with its existing role rather
than an architectural addition.
