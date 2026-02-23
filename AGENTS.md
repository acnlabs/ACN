# AGENTS.md

## Project Overview

ACN (Agent Collaboration Network) is open-source infrastructure for AI agent coordination.

Key capabilities:
- **Registry & Discovery** — Agent registration, A2A Agent Card hosting, skill search
- **Communication** — A2A message routing, broadcast, WebSocket real-time
- **Multi-Subnet** — Public/private isolation, gateway routing
- **Payments (AP2)** — Payment discovery, task payment tracking
- **Task Pool** — Human and agent task creation, assignment, and submission

**Data layer:** Redis only. No SQL database. All agents, tasks, subnets, and metrics live in Redis.

---

## Prerequisites

- Python 3.11+
- Redis (local or Docker)
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

---

## Dev Setup

```bash
# Clone
git clone https://github.com/acnlabs/ACN.git
cd ACN

# Install with uv (recommended)
uv sync --extra dev

# Or with pip
pip install -e ".[dev]"

# Start Redis
docker-compose up -d redis

# Configure environment
cp env.example .env
# Edit .env: set REDIS_URL=redis://localhost:6379

# Start server (with hot reload)
uv run uvicorn acn.api:app --host 0.0.0.0 --port 8000 --reload
```

API docs available at: http://localhost:8000/docs

---

## Run Commands

```bash
# Development
uv run uvicorn acn.api:app --host 0.0.0.0 --port 8000 --reload

# Production
uvicorn acn.api:app --host 0.0.0.0 --port ${PORT:-8000}

# Docker
docker-compose up -d
```

---

## Testing

Tests require a running Redis instance.

```bash
# Start Redis first
docker-compose up -d redis

# Run all tests
uv run pytest -v

# With coverage
uv run pytest --cov=acn --cov-report=term-missing

# Single file
uv run pytest tests/test_registry.py -v
```

---

## Code Style

```bash
# Lint
uv run ruff check .

# Format
uv run ruff format .

# Type check
uv run basedpyright
```

Rules:
- Line length: 100
- Python 3.11+ syntax (`X | Y` unions, `match`, etc.)
- Type hints on all public functions and methods
- `structlog` for all logging (not `print` or stdlib `logging`)
- Route handlers are thin — business logic belongs in `services/`

---

## Architecture

```
acn/                               # Python package
├── api.py                         # FastAPI app, lifespan, root endpoints (/health, /ready, /skill.md)
├── config.py                      # Pydantic settings, env-driven
├── models.py                      # Shared Pydantic models
├── auth/                          # Auth0 JWT verification middleware
├── routes/                        # HTTP route handlers (thin layer)
│   ├── registry.py                # Agent registration & discovery
│   ├── tasks.py                   # Task Pool API
│   ├── communication.py           # Message routing & broadcast
│   ├── subnets.py                 # Subnet management
│   ├── payments.py                # AP2 payment endpoints
│   ├── monitoring.py              # Prometheus metrics & audit
│   ├── analytics.py               # Analytics dashboard
│   └── websocket.py               # WebSocket connections
├── services/                      # Business logic layer
├── infrastructure/
│   ├── messaging/                 # MessageRouter, SubnetManager, WebSocketManager
│   ├── persistence/redis/         # Redis repositories (sole persistence layer)
│   └── task_pool.py               # Task pool management
├── core/                          # Domain entities & repository interfaces
├── protocols/
│   ├── a2a/                       # A2A protocol server (mounted at /a2a)
│   └── ap2/                       # AP2 payment protocol
└── monitoring/                    # Metrics, audit logger, analytics

skills/acn/SKILL.md                # Agent-facing skill documentation (served at /skill.md)
AGENTS.md                          # This file
```

---

## Key Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://localhost:6379` | Redis connection string |
| `GATEWAY_BASE_URL` | `http://localhost:8000` | Public-facing URL of this service |
| `BACKEND_URL` | `http://localhost:8000` | Internal backend service URL |
| `INTERNAL_API_TOKEN` | *(dev default)* | Service-to-service auth token |
| `AUTH0_DOMAIN` | None | Auth0 tenant (e.g. `tenant.auth0.com`) |
| `AUTH0_AUDIENCE` | None | Auth0 audience URL |
| `DEV_MODE` | `true` | Set `false` in production to enforce auth |
| `ENABLE_DOCS` | `true` | Expose `/docs` Swagger UI |
| `CORS_ORIGINS` | `["*"]` | Restrict in production |

---

## Health Endpoints

| Endpoint | Purpose | Expected |
|----------|---------|---------|
| `GET /health` | Liveness — is the process running? | Always `200 {"status":"ok"}` |
| `GET /ready` | Readiness — are dependencies up? | `200` when Redis reachable, `503` otherwise |

Railway healthcheck uses `/health`. Use `/ready` for monitoring/alerting.

---

## Deployment (Railway)

- Builder: Dockerfile
- Health check path: `/health` (liveness, no Redis dependency)
- Required plugin: Railway Redis → set `REDIS_URL=${{Redis.REDIS_URL}}`
- No `startCommand` in `railway.json` — Dockerfile CMD handles `$PORT` expansion via `sh -c`

---

## Conventions

- **Agent IDs are ACN-managed** — never accept externally supplied IDs; always generate via `uuid4()`
- **Redis key prefixes** — `agent:`, `task:`, `subnet:`, `metric:` (check existing patterns before adding new ones)
- **Idempotent registration** — same owner + endpoint combination always returns the same agent ID
- **`dev_mode=true`** bypasses Auth0 — never deploy to production with this enabled
- **Failing fast** — `config.py` validates production settings at startup via `model_validator`
