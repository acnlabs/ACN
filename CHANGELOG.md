# Changelog

All notable changes to the ACN project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.1] - 2026-03-03

### Fixed

- **`auth/middleware.py`**: In `dev_mode`, any bearer token (including ACN API keys) was passed to `_verify_jwt()` which tried to decode it as a JWT against Auth0, causing `500 Authentication service error`. Fixed by returning a dev stub payload immediately when `dev_mode=True`, without calling Auth0. The token value is used as the `sub` claim so agents remain distinguishable.
- **`routes/tasks.py` (`agent_accept_task`)**: `accept_task()` returns a `(task, participation_id)` tuple but the agent-specific route was assigning the whole tuple to `task`, causing `_task_to_response()` to fail with `500 Internal Server Error`. Fixed by unpacking correctly: `task, _participation_id = await ...`.

## [0.4.0] - 2026-03-02

### Added

- **`IEscrowProvider` abstract interface** (`acn/core/interfaces/escrow_provider.py`):
  - Defines the pluggable escrow contract for ACN — enables both off-chain (Agent Planet Backend) and on-chain (smart contract) implementations.
  - Exports `EscrowResult`, `EscrowDetailResult`, and `ReleaseResult` as canonical DTOs, resolving previous layering violations where data models lived inside the service layer.
- **`ReleaseResult` DTO** with 3-way split fields: `agent_amount`, `acn_amount`, `provider_amount`, `proof`.
  - ACN reads and logs these values but never recomputes them — the provider (Backend) is the single source of truth for fee calculation.
- **`AgentPlanetEscrowProvider`** (`acn/services/escrow_client.py`):
  - Implements `IEscrowProvider`; renamed from `EscrowClient` (backward-compat alias `EscrowClient = AgentPlanetEscrowProvider` retained).
  - Parses the Backend's `ReleaseBreakdownResponse` and maps it to `ReleaseResult`.
  - Exposes `supported_currencies` property returning `[AP_POINTS]`.
- **`AP_POINTS = "ap_points"` currency constant** (`protocols/ap2/core.py`):
  - Namespaced identifier for Agent Planet Points, used as `reward_currency` in ACN tasks.
  - Replaces the unnamespaced `"points"` string; backward-compat check retained for existing Redis data.
- **`ESCROW_ENABLED` config flag** (`config.py`):
  - Set `ESCROW_ENABLED=false` to run ACN without payment settlement (e.g. self-hosted deployments not connected to Agent Planet Backend).
  - When disabled, tasks operate normally but all Escrow lock/release calls are skipped; a `warning` log is emitted at startup.
- **`acn_revenue_wallet_id` config field** (`config.py`):
  - Stores the ACN revenue wallet ID in Backend for P&L tracking. Optional — omitting it degrades to zero-fee mode for the ACN share.

### Changed

- **`TaskService.escrow_client`** type changed from `EscrowClient` to `IEscrowProvider` — decouples task logic from the concrete Agent Planet implementation.
- **`reward_currency` checks** now accept both `ap_points` (new canonical form) and `points` (legacy) for backward compatibility with existing Redis task data.
- **`_distribute_reward` return value** now exposes `ReleaseResult` fields (`agent_amount`, `acn_amount`, `provider_amount`, `proof`) directly; structured log fields updated accordingly.

### Fixed

- **`routes/tasks.py` / `routes/subnets.py`**: `await get_subject()` was called as a plain function (11 call sites), causing `AttributeError: 'Security' object has no attribute 'credentials'` on every authenticated task/subnet endpoint. Fixed by extracting `sub` from the already-resolved `payload` dict injected via `Depends(require_permission(...))`.
- **`task_repository.py`**: Redis deserializer injected `payment_released` into the `Task` constructor, which does not define that field, causing `TypeError` on any task read-back. The stale field injection has been removed.

## [0.3.0] - 2026-02-24

### Added
- **ERC-8004 On-Chain Identity**: Full integration with the ERC-8004 Trustless Agents Standard
  - Identity Registry: `totalSupply()` primary discovery + `getLogs()` batched fallback (2000 blocks/batch, compatible with public RPCs)
  - Reputation Registry: `readAllFeedback` aggregation at application layer (anti-Sybil design)
  - Validation Registry: experimental support, 503 until contract addresses are published
  - ABIs: `IdentityRegistry.json`, `ReputationRegistry.json`, `ValidationRegistry.json`
- **New API endpoints** (`/api/v1/onchain/*`):
  - `POST /onchain/agents/{id}/bind` — verify tokenURI on-chain, persist ERC-8004 token binding
  - `GET  /onchain/agents/{id}` — query stored on-chain identity
  - `GET  /onchain/agents/{id}/reputation` — live on-chain reputation summary
  - `GET  /onchain/agents/{id}/validation` — live validation summary (503 when unconfigured)
  - `GET  /onchain/discover` — discover agents via ERC-8004 registry with Redis cache (5 min TTL)
- **ERC-8004 Registration File** endpoint: `GET /agents/{id}/.well-known/agent-registration.json`
  - `agentWallet` as top-level field (per ERC-8004 spec)
  - `services` array with A2A agent card reference
  - `registrations` block once token is bound
- **Python SDK**: `register_onchain()` helper with auto wallet generation (`eth_account`) and `/bind` notification
- **TypeScript SDK**: `registerOnchain()` using `viem`, wallet generation, event parsing
- **`skills/acn/scripts/register_onchain.py`**: standalone CLI script (agentskills.io compatible)
- **Redis reverse index**: `acn:agents:by_erc8004_id:{token_id}` → `agent_id` for fast duplicate detection

### Changed
- Agent entity gains `erc8004_agent_id`, `erc8004_chain`, `erc8004_tx_hash`, `erc8004_registered_at` fields
- Redis persistence and serialization updated for new ERC-8004 fields
- Python SDK dependency: added `web3>=7.0`
- TypeScript SDK dependency: added `viem^2.0.0`

## [0.2.0]

### Added
- **A2A Server Integration**: ACN now exposes its infrastructure services via A2A protocol endpoints
  - `/a2a/jsonrpc` - JSON-RPC 2.0 endpoint for A2A communication
  - `/a2a/jsonrpc/stream` - Server-Sent Events (SSE) endpoint for streaming responses
  - `/.well-known/agent-card.json` - Agent Card with Auth0 authentication details
- **ACN Infrastructure Agent**: New `ACNAgentExecutor` providing 4 core actions:
  - `broadcast` - Multi-agent message broadcasting
  - `discover` - Skill-based agent discovery
  - `route` - Point-to-point message routing with logging and retry
  - `subnet_route` - Subnet gateway routing for NAT traversal
- **Redis Task Store**: Persistent A2A task storage with:
  - Secondary indexes for efficient queries (context_id, status)
  - Automatic expiration (30 days configurable)
  - Pagination support for large task lists
- **MessageRouter A2A Client**: Updated to use official `a2a-sdk` for agent-to-agent communication

### Changed
- **A2A SDK**: Migrated from manual implementation to official `a2a-sdk[http-server]` (v0.4.0+)
- **Task Management**: Replaced `InMemoryTaskStore` with `RedisTaskStore` for persistence
- **A2A Message Handling**: Updated to use `event_queue` pattern instead of generator yields
- **Type Annotations**: Added complete type annotations to all public methods
- **Docstrings**: Added/updated docstrings for all public methods and classes

### Fixed
- **Part Extraction**: Fixed `Part.root` access for proper `DataPart` extraction from A2A messages
- **A2AClient Initialization**: Updated to use correct constructor instead of non-existent `from_url()`
- **SendMessageRequest**: Fixed to use proper request structure with `params.message`
- **Task State Enums**: Corrected usage of `TaskState` (lowercase: `failed`, `completed`, etc.)
- **Message Role Enums**: Fixed `MessageRole` to use `Role` from `a2a.types`

### Documentation
- Updated `docs/a2a-integration.md` with latest implementation details
- Added code quality checks and validation
- Improved API examples with correct `SendMessageRequest` usage

## [0.1.0] - 2024-12-25

### Initial Release
- Agent Registry with A2A Agent Card support
- Communication Layer (MessageRouter, BroadcastService, SubnetManager)
- WebSocket Gateway for real-time communication
- AP2 Payments integration
- Prometheus monitoring
- Auth0 authentication and authorization



