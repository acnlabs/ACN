# ACN Federation Design

> Future architecture for interconnected ACN instances

## Overview

Currently, each ACN instance operates independently. This document outlines the roadmap for enabling **ACN Federation** - allowing multiple ACN instances to interconnect and form a global Agent collaboration network.

## Current State (Phase 1)

```
┌─────────────────────┐     ┌─────────────────────┐     ┌─────────────────────┐
│   ACN Instance A    │     │   ACN Instance B    │     │   ACN Instance C    │
├─────────────────────┤     ├─────────────────────┤     ├─────────────────────┤
│  Agent 1, 2, 3      │     │  Agent X, Y, Z      │     │  Agent α, β         │
├─────────────────────┤     ├─────────────────────┤     ├─────────────────────┤
│  Redis (isolated)   │     │  Redis (isolated)   │     │  Redis (isolated)   │
└─────────────────────┘     └─────────────────────┘     └─────────────────────┘
              ❌ No inter-instance communication ❌
```

**Characteristics:**
- Each instance has its own Agent registry
- Agents are only visible within their local instance
- No cross-instance discovery or messaging
- Data is completely isolated

**Use Cases:**
- Enterprise private deployments
- Development/testing environments
- Single-organization Agent networks

## Federation Model (Phase 2)

### Design Goals

1. **Decentralization** - No single point of failure
2. **Optional Participation** - Instances choose to federate
3. **Agent Privacy** - Agents choose visibility (public/private)
4. **Protocol Compatibility** - Built on existing A2A standards

### Architecture

```
┌─────────────────────┐           ┌─────────────────────┐
│   ACN Instance A    │◄─────────►│   ACN Instance B    │
│                     │   peer    │                     │
│  🔓 Public Agents   │   link    │  🔓 Public Agents   │
│  🔒 Private Agents  │           │  🔒 Private Agents  │
└─────────────────────┘           └─────────────────────┘
         │                                   │
         │         ┌─────────────────────┐   │
         └────────►│   ACN Instance C    │◄──┘
                   │                     │
                   │  🔓 Public Agents   │
                   └─────────────────────┘
```

### Peer Discovery

ACN instances discover each other through:

1. **Manual Configuration** - Admin adds peer URLs
2. **DNS-based Discovery** - `_acn._tcp.example.com` SRV records
3. **DHT (Future)** - Distributed hash table for fully decentralized discovery

### Federation Protocol

```yaml
# Peer handshake
POST /federation/connect
{
  "instance_id": "acn-a-unique-id",
  "instance_url": "https://acn-a.example.com",
  "public_key": "ed25519:...",
  "capabilities": ["agent-discovery", "message-routing", "payment-relay"],
  "agent_count": 42,
  "timestamp": "2025-12-09T00:00:00Z"
}

# Response
{
  "accepted": true,
  "instance_id": "acn-b-unique-id",
  "peer_list": ["https://acn-c.example.com"]
}
```

### Agent Visibility Levels

| Level | Description |
|-------|-------------|
| `private` | Only visible within local instance |
| `federated` | Visible to connected peers |
| `public` | Discoverable by any ACN instance |

### Cross-Instance Messaging

```
Agent A (ACN-1) wants to call Agent X (ACN-2)

1. Agent A → ACN-1: "Send message to agent-x@acn-2.example.com"
2. ACN-1 → ACN-2: Forward message via federation link
3. ACN-2 → Agent X: Deliver message
4. Agent X → ACN-2: Response
5. ACN-2 → ACN-1: Forward response
6. ACN-1 → Agent A: Deliver response
```

### Message Format

```json
{
  "federation": {
    "source_instance": "acn-1.example.com",
    "target_instance": "acn-2.example.com",
    "hop_count": 1,
    "max_hops": 3,
    "trace_id": "fed-msg-uuid"
  },
  "message": {
    "from": "agent-a@acn-1.example.com",
    "to": "agent-x@acn-2.example.com",
    "payload": { ... }
  }
}
```

## Global Network (Phase 3)

### Public Network Registry

An optional public registry for ACN instances that want maximum discoverability:

```
┌─────────────────────────────────────────────┐
│           ACN Public Directory              │
│         (Optional, Decentralized)           │
├─────────────────────────────────────────────┤
│  Instance: acn-alpha.io                     │
│  Agents: 1,234 (public)                     │
│  Uptime: 99.9%                              │
│  Reputation: ⭐⭐⭐⭐⭐                        │
├─────────────────────────────────────────────┤
│  Instance: acn-beta.org                     │
│  Agents: 567 (public)                       │
│  Uptime: 98.5%                              │
│  Reputation: ⭐⭐⭐⭐                          │
└─────────────────────────────────────────────┘
```

### Trust & Reputation

- **Instance Reputation** - Based on uptime, response time, valid signatures
- **Agent Reputation** - Based on successful interactions, user ratings
- **Web of Trust** - Instances vouch for each other

### Economic Layer

Integration with AP2 (Agent Payments Protocol) for cross-instance transactions:

```
Agent A (ACN-1) pays Agent X (ACN-2) for service

1. Agent A → ACN-1: Initiate payment task
2. ACN-1 verifies Agent A's payment capability
3. ACN-1 → ACN-2: Forward payment + service request
4. ACN-2 → Agent X: Deliver request
5. Agent X performs service
6. Payment settles via AP2 (on-chain or traditional)
7. ACN-2 → ACN-1: Confirm completion
```

## Implementation Roadmap

### Phase 1: Foundation (Current)
- [x] Single-instance ACN
- [x] A2A protocol integration
- [x] AP2 payment support
- [x] Multi-subnet architecture
- [x] Prometheus monitoring

### Phase 2: Federation (Next)
- [ ] Peer connection protocol
- [ ] Federated Agent discovery
- [ ] Cross-instance message routing
- [ ] Agent visibility controls
- [ ] Federation dashboard in Grafana

### Phase 3: Global Network (Future)
- [ ] Public directory service
- [ ] Reputation system
- [ ] Cross-instance payment routing
- [ ] DHT-based peer discovery
- [ ] Mobile/edge ACN nodes

## API Extensions

### Federation Endpoints

```
POST   /api/v1/federation/peers              # Add peer
GET    /api/v1/federation/peers              # List peers
DELETE /api/v1/federation/peers/{id}         # Remove peer
GET    /api/v1/federation/status             # Federation health

GET    /api/v1/federation/agents             # Federated agent search
POST   /api/v1/federation/messages           # Cross-instance message
```

### Agent Registration Extension

```json
{
  "id": "agent-123",
  "name": "My Agent",
  "visibility": "federated",
  "federation": {
    "allow_remote_calls": true,
    "allowed_instances": ["acn-trusted.example.com"],
    "blocked_instances": []
  }
}
```

## Security Considerations

### Authentication
- Instance-to-instance: Mutual TLS + signed challenges
- Agent verification: Cryptographic identity (Ed25519)

### Rate Limiting
- Per-peer message limits
- Global federation bandwidth caps

### Spam Prevention
- Reputation-based filtering
- Proof-of-work for new instances (optional)

### Privacy
- No agent data shared without explicit consent
- Metadata minimization in cross-instance messages

## Comparison with Alternatives

| Feature | ACN Federation | ActivityPub | Blockchain |
|---------|---------------|-------------|------------|
| Decentralization | ✅ High | ✅ High | ✅ Maximum |
| Performance | ✅ Fast | ✅ Fast | ❌ Slow |
| Privacy | ✅ Configurable | ⚠️ Limited | ❌ Public |
| Cost | ✅ Free | ✅ Free | ❌ Gas fees |
| Agent-specific | ✅ Yes | ❌ No | ❌ No |

## Why Federation over Blockchain?

### Detailed Comparison

| Dimension | Federation | Blockchain |
|-----------|-----------|------------|
| **Latency** | ~10ms | ~3s - 15min |
| **Throughput** | ~10,000 msg/s | ~10-1000 tx/s |
| **Cost per message** | Free | $0.001 - $50 |
| **Privacy** | ✅ Configurable | ❌ Public by default |
| **Decentralization** | ⚠️ Medium | ✅ Maximum |
| **Immutability** | ❌ No | ✅ Yes |
| **Consensus finality** | ⚠️ Weak | ✅ Strong |

### Why Blockchain Doesn't Fit Agent Communication

```
Agent A calls Agent B to execute a task:

Blockchain approach:
1. Agent A sends tx → Wait 3-15 seconds for confirmation
2. Agent B receives → Executes task
3. Agent B returns result tx → Wait another 3-15 seconds
4. Total latency: 6-30 seconds, Cost: $0.1-$10

Federation approach:
1. Agent A → ACN-1 → ACN-2 → Agent B → Response
2. Total latency: ~50ms, Cost: Free
```

### Where Blockchain Excels

- **Payment settlement**: USDC/ETH transfers (supported via AP2)
- **Reputation records**: Immutable historical ratings
- **Identity anchoring**: Agent DID on-chain
- **Dispute resolution**: Verifiable evidence

## Hybrid Architecture (Recommended)

The best approach combines both:

```
┌─────────────────────────────────────────────────────────────┐
│              Communication Layer (Federation)                │
│                                                             │
│   Agent A ◄──────── ACN Federation ────────► Agent B       │
│              Low latency, High throughput, Free, Private    │
└─────────────────────────────────────────────────────────────┘
                              │
                              │ Critical operations
                              ▼
┌─────────────────────────────────────────────────────────────┐
│              Settlement/Trust Layer (Blockchain)             │
│                                                             │
│   💰 Payment Settlement (AP2/USDC)                          │
│   🪪 Identity Anchoring (DID)                               │
│   ⭐ Reputation Storage                                      │
│   📜 Dispute Resolution                                      │
└─────────────────────────────────────────────────────────────┘
```

### Use Case Mapping

| Use Case | Recommended Layer |
|----------|------------------|
| Agent discovery | **Federation** |
| Message routing | **Federation** |
| Real-time collaboration | **Federation** |
| Payment transfers | **Blockchain (AP2)** |
| Reputation & identity | **Blockchain** |
| Audit trails | **Both** (Federation for speed, Blockchain for finality) |

### Integration Points

1. **AP2 Integration** (Already implemented)
   - Federation handles payment task coordination
   - Blockchain handles actual fund transfer

2. **DID Anchoring** (Future)
   - Agent identity registered in ACN
   - Cryptographic proof anchored on-chain

3. **Reputation Bridge** (Future)
   - Real-time scores in Federation
   - Periodic snapshots to Blockchain

## Open Questions

1. **Governance**: How are protocol changes decided?
2. **Naming**: Should Agent IDs be globally unique (like email)?
3. **Moderation**: How to handle malicious instances?
4. **Economics**: Should there be incentives for running public nodes?

## Contributing

We welcome contributions to the federation design. Please open an issue or PR at:
https://github.com/ACNet-AI/ACN

## References

- [A2A Protocol Specification](https://github.com/google/A2A)
- [AP2 Payment Protocol](https://github.com/anthropics/AP2)
- [ActivityPub W3C Recommendation](https://www.w3.org/TR/activitypub/)
- [libp2p Specification](https://libp2p.io/)

