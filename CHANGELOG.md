# Changelog

All notable changes to the ACN project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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



