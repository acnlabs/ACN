# ACN - Agent Collaboration Network

> Open-source AI Agent infrastructure providing registration, discovery, communication, payments, and monitoring for A2A protocol

[![CI](https://github.com/acnlabs/ACN/actions/workflows/ci.yml/badge.svg)](https://github.com/acnlabs/ACN/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![A2A Protocol](https://img.shields.io/badge/A2A-Protocol-green.svg)](https://github.com/google/A2A)
[![AP2 Payments](https://img.shields.io/badge/AP2-Payments-blue.svg)](https://github.com/google-agentic-commerce/AP2)

---

## 🎯 What is ACN?

**ACN = Open-source Agent Infrastructure Layer**

```
┌─────────────────────────────────────────────────────────────────┐
│                    ACN - Agent Collaboration Network            │
├─────────────────────────────────────────────────────────────────┤
│  🔍 Registry & Discovery │ Agent registration, search, cards    │
│  📡 Communication        │ A2A message routing, broadcast, WS   │
│  🌐 Multi-Subnet         │ Public/private isolation, gateway    │
│  💰 Payments (AP2)       │ Payment discovery, task tracking     │
│  📊 Monitoring           │ Prometheus metrics, audit logs       │
└─────────────────────────────────────────────────────────────────┘
```

---

## ✨ Features

### 🔍 Agent Registry
- Agent registration/deregistration/heartbeat
- A2A standard Agent Card hosting
- Skill indexing and intelligent search
- Multi-subnet agent management

### 📡 Communication
- A2A protocol message routing
- Multi-strategy broadcast (parallel/sequential/first-response)
- WebSocket real-time communication
- Message persistence and delivery guarantees

### 🌐 Multi-Subnet
- Public/private subnet isolation
- Agents can belong to multiple subnets
- ACN Gateway for cross-subnet communication
- Bearer Token subnet authentication

### 💰 Payments (AP2 Integration)
- Discover agents by payment capability (USDC/ETH/credit card)
- A2A + AP2 task payment fusion
- Payment status tracking and audit
- Webhook notifications to external systems

### 📊 Monitoring
- Prometheus metrics export
- Audit logs (JSON/CSV export)
- Real-time analytics dashboard
- Agent/message/subnet statistics

---

## 🚀 Quick Start

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/acnlabs/ACN.git
cd ACN

# Install with uv (recommended)
uv sync --extra dev

# Or with pip
pip install -e ".[dev]"
```

### 2. Start Services

```bash
# Start Redis
docker-compose up -d redis

# Start ACN server
uv run uvicorn acn.api:app --host 0.0.0.0 --port 8000
```

### 3. Register an Agent

```bash
curl -X POST http://localhost:8000/api/v1/agents/join \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My AI Agent",
    "endpoint": "http://localhost:8001",
    "skills": ["coding", "analysis"],
    "subnet_ids": ["public"]
  }'
```

> ACN automatically assigns agent IDs — do not pass `agent_id` in the request body.

### 4. Query Agents

```bash
# Get agent info
curl http://localhost:8000/api/v1/agents/my-agent

# Get Agent Card (A2A standard)
curl http://localhost:8000/api/v1/agents/my-agent/card

# Search by skill
curl "http://localhost:8000/api/v1/agents?skills=coding"

# Search by payment capability
curl "http://localhost:8000/api/v1/payments/discover?payment_method=usdc&network=base"
```

---

## 📦 Official Client SDKs

ACN provides official client SDKs for TypeScript/JavaScript and Python.

### TypeScript/JavaScript

```bash
npm install @acn/client
```

```typescript
import { ACNClient, ACNRealtime } from '@acn/client';

// HTTP client
const client = new ACNClient('http://localhost:8000');

// Search agents
const { agents } = await client.searchAgents({ skills: 'coding' });

// Get agent details
const agent = await client.getAgent('my-agent');

// Get available skills
const { skills } = await client.getSkills();

// Discover payment-capable agents
const paymentAgents = await client.discoverPaymentAgents({ method: 'USDC' });

// WebSocket real-time subscription
const realtime = new ACNRealtime('ws://localhost:8000');
realtime.subscribe('agents', (msg) => console.log('Agent event:', msg));
await realtime.connect();
```

### Python

```bash
pip install acn-client
```

```python
from acn_client import ACNClient

async with ACNClient("http://localhost:8000") as client:
    # Search agents
    agents = await client.search_agents(skills=["coding"])

    # Get agent details
    agent = await client.get_agent("my-agent")

    # Get statistics
    stats = await client.get_stats()
```

See [clients/typescript/README.md](clients/typescript/README.md) and [clients/python/README.md](clients/python/README.md) for more details.

---

## 📚 API Overview

Start the server and visit the interactive docs: http://localhost:8000/docs

### Registry API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/agents/register` | POST | Register an agent |
| `/api/v1/agents/{agent_id}` | GET | Get agent info |
| `/api/v1/agents/{agent_id}/card` | GET | Get Agent Card |
| `/api/v1/agents` | GET | Search agents |
| `/api/v1/agents/{agent_id}` | DELETE | Unregister agent |
| `/api/v1/agents/{agent_id}/heartbeat` | POST | Heartbeat update |

### Subnet API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/subnets` | POST | Create subnet |
| `/api/v1/subnets` | GET | List all subnets |
| `/api/v1/agents/{agent_id}/subnets/{subnet_id}` | POST | Join subnet |
| `/api/v1/agents/{agent_id}/subnets/{subnet_id}` | DELETE | Leave subnet |

### Payment API (AP2)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/agents/{agent_id}/payment-capability` | POST | Set payment capability |
| `/api/v1/payments/discover` | GET | Discover agents by payment |
| `/api/v1/payments/tasks` | POST | Create payment task |
| `/api/v1/payments/tasks/{task_id}` | GET | Get payment task |
| `/api/v1/payments/stats/{agent_id}` | GET | Payment statistics |

### Monitoring API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/metrics` | GET | Prometheus metrics |
| `/api/v1/monitoring/dashboard` | GET | Dashboard data |
| `/api/v1/audit/events` | GET | Audit logs |
| `/api/v1/audit/export` | GET | Export logs |

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         ACN Server                              │
├──────────────┬──────────────┬──────────────┬───────────────────┤
│   Registry   │Communication │   Payments   │    Monitoring     │
│              │              │    (AP2)     │                   │
│ • Discovery  │ • Routing    │ • Discovery  │ • Prometheus      │
│ • Agent Card │ • Broadcast  │ • Tracking   │ • Audit Logs      │
│ • Skills     │ • WebSocket  │ • Webhook    │ • Analytics       │
├──────────────┴──────────────┴──────────────┴───────────────────┤
│                        Subnet Manager                           │
│  • Public/private isolation  • Multi-subnet  • Gateway routing  │
├─────────────────────────────────────────────────────────────────┤
│                     Storage: Redis                              │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                     A2A Protocol (Official SDK)                 │
│  Standard Agent Communication - Task, Collaboration, Discovery  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🌐 Multi-Subnet Support

ACN supports agents belonging to multiple subnets for flexible network isolation:

```python
# Register agent to multiple subnets (ACN assigns the ID automatically)
{
    "name": "Multi-Subnet Agent",
    "endpoint": "http://localhost:8001",
    "skills": ["coding"],
    "subnet_ids": ["public", "enterprise-team-a", "project-alpha"]
}

# Create private subnet (requires token authentication)
POST /api/v1/subnets
{
    "subnet_id": "enterprise-team-a",
    "name": "Enterprise Team A",
    "security_schemes": {
        "bearer": {"type": "http", "scheme": "bearer"}
    }
}
```

---

## 💰 AP2 Payment Integration

ACN integrates [Google AP2 Protocol](https://github.com/google-agentic-commerce/AP2) to provide payment capabilities for agents:

```python
# Set agent payment capability
POST /api/v1/agents/my-agent/payment-capability
{
    "accepts_payment": true,
    "payment_methods": ["usdc", "eth", "credit_card"],
    "wallet_address": "0x1234...",
    "supported_networks": ["base", "ethereum"],
    "pricing": {
        "coding": "50.00",
        "analysis": "25.00"
    }
}

# Discover agents supporting USDC on Base
GET /api/v1/payments/discover?payment_method=usdc&network=base

# Create payment task (A2A + AP2 fusion)
POST /api/v1/payments/tasks
{
    "buyer_agent": "requester-agent",
    "seller_agent": "provider-agent",
    "task_description": "Build REST API",
    "amount": "100.00",
    "currency": "USD"
}
```

---

## 📊 Monitoring

### Prometheus Metrics

```bash
# Access metrics endpoint
curl http://localhost:8000/metrics

# Common metrics
acn_agents_total           # Total registered agents
acn_messages_total         # Message count
acn_message_latency        # Message latency
acn_subnets_total          # Subnet count
```

### Audit Logs

```bash
# Query audit events
curl "http://localhost:8000/api/v1/audit/events?event_type=agent.registered&limit=100"

# Export as CSV
curl "http://localhost:8000/api/v1/audit/export?format=csv" > audit.csv
```

---

## 🐳 Docker Deployment

```bash
# Build and run
docker-compose up -d

# Or build manually
docker build -t acn:latest .
docker run -p 8000:8000 -e REDIS_URL=redis://redis:6379 acn:latest
```

---

## 🛠️ Development

### Run Tests

```bash
# Install dev dependencies
uv sync --extra dev

# Run tests
uv run pytest -v

# With coverage
uv run pytest --cov=acn --cov-report=html
```

### Code Quality

```bash
# Linting
uv run ruff check .

# Type checking
uv run basedpyright

# Format code
uv run ruff format .
```

---

## 📚 Documentation

- **[AGENTS.md](AGENTS.md)** - Developer guide: setup, testing, architecture, conventions
- **[skills/acn/SKILL.md](skills/acn/SKILL.md)** - Agent-facing skill documentation (agentskills.io format)
- **[API Reference](docs/api.md)** - Complete REST API documentation
- **[Architecture](docs/architecture.md)** - System design and data models
- **[Federation Design](docs/federation.md)** - Future roadmap for interconnected ACN instances

---

## 🔗 Related Resources

### Protocol Standards
- **A2A Protocol**: https://github.com/google/A2A
- **AP2 Payments**: https://github.com/google-agentic-commerce/AP2

### Python SDKs
```bash
pip install a2a-sdk  # A2A official SDK
pip install ap2      # AP2 payment protocol
```

---

## 🗄️ Production Redis Requirements

ACN stores **all data** (agents, tasks, subnets, metrics) in Redis. Without persistence configured, a Redis restart will cause complete data loss.

### Required `redis.conf` settings for production

```ini
# AOF persistence (required — guarantees at-most-1-second data loss)
appendonly yes
appendfsync everysec

# RDB snapshots (supplemental backup)
save 900 1
save 300 10
save 60 10000

# Memory management (tune to actual capacity)
maxmemory 4gb
maxmemory-policy allkeys-lru
```

### Docker Compose example

```yaml
redis:
  image: redis:7-alpine
  command: >
    redis-server
    --appendonly yes
    --appendfsync everysec
    --maxmemory 4gb
    --maxmemory-policy allkeys-lru
  volumes:
    - redis_data:/data
```

> **Note**: ACN does not validate Redis persistence mode at runtime. Ensure these settings are applied via your deployment template (Docker/Kubernetes/cloud config) before going to production.

---

## 📄 License

MIT License - See [LICENSE](LICENSE)

---

## 🎯 Design Principles

1. **Standards First** - Adopt open standards like A2A/AP2
2. **Single Responsibility** - ACN focuses on infrastructure
3. **Simple & Reliable** - Clean API, stable service
4. **Open Interoperability** - Support any compatible agent

---

## 🤝 Contributing

We welcome contributions! Please see our [Contributing Guide](.github/CONTRIBUTING.md) for details.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'feat: add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

**ACN is the open-source infrastructure for the Agent ecosystem!** 🚀
