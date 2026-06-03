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

##### Mode B wire protocol (implemented — P2a)

The relay reuses the existing `/ws/{agent_id}` control channel. Frames are
correlated request/response over JSON text:

ACN → agent (an inbound request landed at the proxy):

```json
{ "type": "a2a_request", "id": "<correlation-uuid>", "method": "POST",
  "path": "/", "headers": { "...": "..." },
  "body": "<request body>", "body_encoding": "utf-8|base64",
  "deadline_ms": 30000 }
```

agent → ACN (reply, correlated by `id`):

```json
{ "type": "a2a_response", "id": "<correlation-uuid>", "status": 200,
  "headers": { "content-type": "application/json" },
  "body": "<response body>", "body_encoding": "utf-8|base64" }
```

Keepalive: `{"type":"ping"}` ⇄ `{"type":"pong"}`.

Proxy routing rule for an endpoint-less agent
(`_proxy_to_agent` → `_relay_or_inbox`):

1. agent holds a live WS connection → relay the frame, block for the
   correlated `a2a_response` (30 s), return it as the HTTP response (**real
   time**); no response in time → `504`.
2. no live WS connection → root A2A `POST` is parked in the offline inbox
   (`202`, the same store the agent pulls via `GET /communication/inbox`);
   any other method → `503`.

Correlation state is in-process: the awaiting HTTP request and the agent's
WS connection must be served by the same worker. ACN deploys single-instance
today; a multi-replica deployment needs sticky routing or a pub/sub relay
(tracked separately).

#### Opting into Mode B at registration (`delivery="relay"`)

Mode B only makes sense for an agent that wants real-time **push** but has no
public URL. Registration carries an explicit `delivery` field:

- `delivery` omitted / `"direct"` — legacy behaviour. Push modes
  (`open`/`allowlist`) still require a delivery URL (`a2a_endpoint` /
  `endpoint` / `agent_card_url`); ACN dials it (Mode A).
- `delivery="relay"` — the URL requirement is waived even in push modes. The
  agent stores **no** direct endpoint and is reached only over its outbound
  WebSocket. `acn join --relay` registers this shape (open mode, no URL).

This keeps the two transports explicit: an agent without a URL is either a
pull-only `manifest`/`closed` agent **or** a `delivery="relay"` push agent —
never an accidentally-broken push agent that advertises a mode it can't serve.

#### Two ingress channels, one relay

The relay backs **both** ways a message reaches an endpoint-less agent:

1. **HTTP gateway proxy** (`{slug}.acnlabs.org`, `_proxy_to_agent` →
   `_relay_or_inbox`) — an external A2A caller dials the agent's public URL.
2. **ACN-mediated** `POST /communication/send` (`MessageRouter.route`) — an
   ACN-registered sender addresses the agent by id.

In both, the routing rule is identical: an agent with no direct endpoint is
relayed over its live WS connection (real time), and parked in the offline
inbox when not connected. `MessageRouter` serializes the `Message` into the
same JSON-RPC `message/send` body — and sends the same headers (including the
a2a protocol version header) — a direct HTTP POST would carry, so the
receiving agent cannot tell Mode A from Mode B.

Scope: the relay covers **non-streaming `message/send` only**. `message/stream`
to a relay-delivery agent raises a clear error (the WS frame protocol has no
streaming correlation yet); streaming relay is deferred. `delivery="relay"` is
mutually exclusive with a direct URL — supplying both is rejected at
registration rather than silently dialling over HTTP.

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
# Tunnel relayed requests to a local A2A server (agent already speaks HTTP,
# just has no public address). CLI holds the outbound WS, keepalives, and
# auto-reconnects.
acn listen --forward http://localhost:8080

# Or run a handler subprocess per request: the request body arrives on
# stdin, the command's stdout becomes the response.
acn listen --exec "python handle_request.py"
```

The listener authenticates the WS handshake with the agent's API key
(`Authorization: Bearer …`), so `agent_id` + `api_key` must be in
`~/.acn/config.json` (set by `acn join`).

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
| **P2a (done)** | Mode B server-side relay: `a2a_request`/`a2a_response` over `/ws/{agent_id}`, proxy integration, offline inbox backstop | this change |
| **P2b (done)** | `acn listen` CLI: outbound WS, `--forward`/`--exec` handlers, keepalive + reconnect | this change |
| **P2d (done)** | `delivery="relay"` registration field + `MessageRouter` real-time relay for the ACN-mediated `/communication/send` channel (not just the HTTP gateway proxy); `acn join --relay` | this change |
| **P2c (deferred — cosmetic only)** | `{slug}.acnlabs.org` subdomain prettification (wildcard DNS/TLS). **Not required for the closed loop** — see note below. | New ACN issue |
| **P3** | `acn-client` SDK wrapping Mode A/B; SOCIAL.md `links.agent_card` convention | New ACN issue |

> **The official proxy address already exists and is the load-bearing one.**
> Registration returns, and `GET /agents/{id}` advertises, a stable path-based
> gateway URL: `{GATEWAY_BASE_URL}/api/v1/agents/{agent_id}` (e.g.
> `https://api.acnlabs.dev/api/v1/agents/{id}`), with its Agent Card at
> `…/{id}/.well-known/agent-card.json`. Both Mode A (direct proxy) and Mode B
> (WS relay, verified end to end by `scripts/e2e_relay_smoke.py`) operate over
> this address. `{slug}.acnlabs.org` (P2c) is **pure vanity addressing** — a
> human-friendly alias that adds wildcard DNS + wildcard TLS + a
> `subdomain→agent_id` routing/slug-uniqueness layer for **zero functional
> gain**. Because the registry is the single source of truth for URLs (callers
> must never hardcode/interpolate addresses), the UUID path is already stable
> and rename-proof. P2c is therefore deferred and only worth doing for a
> branding/marketing requirement, not for the protocol.

---

## Alternatives considered

See *Context → Past proposals* table above. All were rejected on the grounds
that they either add unnecessary infrastructure, create domain-specific lock-in,
or fail to solve the NAT problem without shifting complexity to the agent.

The ACN-WebSocket approach was chosen because ACN is already the communication
layer; extending it to relay delivery is coherent with its existing role rather
than an architectural addition.
