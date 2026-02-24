# Changelog

All notable changes to the ACN project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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



