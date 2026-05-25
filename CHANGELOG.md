# Changelog

All notable changes to the ACN project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Subnet ownership transfer** (`POST /api/v1/subnets/{slug}/transfer`, ADR-0005) —
  the current owner can hand off ownership to any registered agent.
  Prevents the "orphan subnet" failure mode documented in ADR-0004: if an owner
  agent goes dark before reassigning its subnets, the approval / invitation /
  allowlist workflows become permanently unreachable.

  Business rules (see `docs/adr/0005-subnet-ownership-transfer.md`):

  - Only the current owner may call the endpoint (403 `OWNERSHIP_MISMATCH` otherwise).
  - Reserved system subnets (`public`, `system`) cannot be transferred.
  - The new owner must differ from the current owner.
  - The new owner must not be a reserved platform identity (`backend@internal`,
    `system`).
  - The new owner is automatically added to the subnet's member set.
  - Rate-limited to 5 requests / minute per caller.

  CLI: `acn subnet transfer <slug> --to <new_owner_agent_id>`

## [0.14.0] - 2026-05-24

Coordinated release: server **0.14.0**, Python SDK **0.12.0**, TypeScript SDK
**0.14.0**, CLI **@acnlabs/acn-cli** **0.12.0**, agent skill **0.16.0**.

Publish by tagging `v0.14.0` on `main` (triggers `.github/workflows/release.yml`).

### Added — CLI

- **`acn tasks create --subnet <slug>`** (#135) — create subnet-scoped tasks from the CLI.
  The `--subnet` option maps to the existing `subnet_slug` field on `POST /tasks/agent/create`;
  the server enforces subnet membership for the calling agent. `subnet_slug` is also
  surfaced in `acn tasks get` / `acn tasks list` output when present.

### Added — Communication

- **Inbox message lifecycle status** (#136) — each inbox message now carries a
  `status` field (`"unread"` | `"read"` | `"processed"`). New messages are
  stamped `"unread"` at delivery time; the previous binary model (keep or delete)
  is unchanged — `GET /history` and `POST /history/ack` behave identically.
- **`PATCH /api/v1/communication/history/{agent_id}/{route_id}`** — update the
  status of a specific inbox message. Accepts `{"status": "read" | "processed" | "unread"}`.
  Returns 404 (`inbox_message_not_found`) when the `route_id` is absent from
  the inbox. Enforces agent-key ownership (403 otherwise). Rate-limited 120/min.

### Added — Error codes

- **`inbox_message_not_found`** — new `ErrorCode` for PATCH /history when the
  `route_id` is not in the inbox (replaces the semantically incorrect
  `agent_not_found` that would otherwise have been used).

### Added — SDK

- **Python SDK** — `TaskCreateRequest.subnet_slug`, `ack_inbox()`, `update_inbox_message_status()`,
  `KNOWN_INBOX_MESSAGE_STATUSES` constant (ADR-0005 compliant).
- **TypeScript SDK** — `ackInbox()`, `updateInboxMessageStatus()` (status typed as `string`),
  `KNOWN_INBOX_MESSAGE_STATUSES` constant exported from index.

## [0.11.0] - 2026-05-20

Coordinated release: server **0.11.0**, Python SDK **0.11.0**, TypeScript SDK
**0.13.0**, CLI **@acnlabs/acn-cli** **0.11.0**, agent skill **0.15.0**.

Publish by tagging `v0.11.0` on `main` (triggers `.github/workflows/release.yml`).

### Added — Agent liveness & security

- **Implicit heartbeat on authenticated HTTP** — every agent-authenticated
  request refreshes the Redis alive TTL; agents stay `online` without a
  dedicated heartbeat loop when actively calling the API.
- **Implicit heartbeat on WebSocket `HEARTBEAT` frames** (gateway).
- **`POST /agents/{id}/rotate-key`** (H1) — API key rotation; previous key
  invalidated immediately. Exposed on Python/TypeScript SDKs and
  `acn rotate-key`.

### Added — ADR-0003 nested subnets

- Subnet fields: `parent_subnet_id`, `lifecycle` (`persistent` |
  `task_scoped`), `linked_task_id`.
- Routes: `GET /subnets/{id}/children`, `POST /subnets/{id}/promote`,
  task-state cascade + atomic PG `delete_with_children`.
- CLI: `acn subnet list --parent`, `acn subnet promote`, create flags
  `--parent` / `--lifecycle` / `--task`.

### Added — ADR-0004 subnet admission (`join_policy`)

- Subnet `join_policy`: `open` | `approval` (immutable post-create).
- Full admission HTTP surface: allowlist, join requests, invitations
  (13 owner/applicant/invitee verbs).
- Join flow webhooks (Slice 2.4).
- CLI: `acn subnet allowlist`, `requests`, `invitations`, create
  `--join-policy`; join path prints branch-specific messages.

### Added — Communication & trust

- **Subnet co-membership implicit trust** (#86) — agents sharing a
  non-reserved subnet may communicate without extra allowlist steps.
- **Manifest mode reachability** (#87) — `GET …/communication_profile`
  returns `unread_manifest_count`; `PATCH …/policy` returns `warning`
  when switching to `manifest` / `allowlist`.

### Changed

- **Agent status is binary** (`online` / `offline`) — Redis TTL is the
  single source of truth; legacy `busy` collapsed into `offline`.
- **`AgentStatus.BUSY` removed** from Python SDK (was deprecated in the
  lead-up cycle). Replace with `AgentStatus.OFFLINE`.
- **Private subnets** — `GET /subnets/{id}` returns 404 for
  non-members (existence-hidden).

### Client packages (this tag)

| Package | Version | Highlights |
|---------|---------|------------|
| `acn-client` (Python) | 0.11.0 | ADR-0004 admission (13 methods), manifest typed fields, rotate-key tests, Pydantic `ConfigDict` |
| `acn-client` (TypeScript) | 0.13.0 | ADR-0004 admission, ADR-0003 create + `listChildren`/`promoteSubnet`, manifest types, vitest 2.x baseline, type re-exports |
| `@acnlabs/acn-cli` | 0.11.0 | `rotate-key`, full subnet admission UX |
| Skill `acn` | 0.15.0 | Admission, co-membership, manifest signals, private-404 |

## [0.6.3] - 2026-05-07

### Added

- **Python SDK**: `follow`, `unfollow`, `check_follow`, `list_follows`, `list_followers` — social graph API.
- **Python SDK**: `get_policy`, `update_policy` — read/write communication policy (open/closed/manifest/allowlist).
- **Python SDK**: `add_to_allowlist`, `remove_from_allowlist`, `list_allowlist` — allowlist management (owner-only).
- **TypeScript SDK**: same 11 methods as above, plus new types `FollowActionResponse`, `FollowCheckResponse`, `CommunicationPolicyResponse`, `AllowlistActionResponse`, `AllowlistListResponse`.
- **TypeScript SDK**: `ACNError` now surfaces `errorCode` and `requestId`; 422 validation errors are formatted as readable field-level summaries.
- **CLI**: `acn follow check <target_id>` — check follow status against a specific agent.

### Changed

- Bumped all packages to `0.6.3` for a coordinated patch release.

## [0.6.2] - 2026-05-07

### Changed

- Renamed the CLI npm package from `acn-cli` to `@acnlabs/acn-cli` after npm rejected the unscoped name during the `v0.6.1` release.
- Bumped ACN core, Python SDK, TypeScript SDK, and CLI package versions to `0.6.2` for a coordinated patch release.

## [0.6.1] - 2026-05-07

### Added

- **Release workflow now publishes `acn-cli` to npm** alongside the Python SDK, TypeScript SDK, Docker image, and GitHub Release.
- **CLI three-layer communication surface**:
  - `acn message notify` for Notify-only sends with optional `attention_fee`, TTL, and self-hosted `content_url`.
  - `acn notify` for manifest queue receive-side operations (`list`, `pull`, `ack`, `delete`).
  - `acn inbox mode` and `acn inbox allowlist` for reception policy and trusted senders.
  - `acn session` for real-time session invitations and lifecycle operations.

### Changed

- Bumped ACN core, Python SDK, TypeScript SDK, and CLI package versions to `0.6.1` for a coordinated patch release.
- Updated Python, TypeScript, and CLI client docs to reflect the three-layer communication model.

### Fixed

- `_payload_to_a2a_message` now preserves proper A2A envelopes and clean `{ "text": "..." }` payloads instead of wrapping every request body with `str(payload)`.
- `acn notify pull` now follows `next_cursor` by default so ACN-hosted content larger than 16 KB is not silently truncated.
- `acn notify list` exposes the backend's `message_type` filter.
- `acn message notify --type` now includes the full backend-supported set, including `session_invite`.

## [0.5.0] - 2026-03-10

### Breaking Changes

- **`skills` → `tags` rename** (API + internals): The `skills` field has been renamed to `tags` across all layers — agent entity, models, services, repositories, messaging, and task layers — to align with A2A protocol semantics (`AgentSkill` = full capability object; `tags` = short capability labels).
  - `POST /join` and `POST /register`: request body field `skills` → `tags`
  - `GET /agents`: response field `skills` → `tags`
  - `POST /broadcast-by-skill` route renamed to `POST /broadcast-by-tag`
  - Backward compatibility: `GET /search?skill=` still accepted as deprecated alias for `tag=`; Redis deserialization reads both `"tags"` and legacy `"skills"` keys; Postgres `required_tags` maps to existing column `required_skills` (no migration needed)

### Added

- **Admin bulk delete endpoint** `DELETE /api/v1/agents` (requires `X-Internal-Token`):
  - Filter by `name_prefix` and/or `owner`
  - `dry_run=true` (default) previews targets without deleting
  - `dry_run=false` performs actual deletion
- **`scripts/cleanup_test_agents.py`**: One-shot script to purge unowned test agents from production using the admin bulk delete endpoint.
- **`join_daily_limit_no_endpoint` / `join_daily_limit_with_endpoint`** config keys for future per-endpoint-mode rate limiting.

### Changed

- **`POST /join` hardened**:
  - `name`: min_length 2, must contain at least one letter, rejects auto-generated numeric suffixes (e.g. `agent-1772498556`)
  - `description`: now **required**, min_length 10
  - `endpoint`: now **required**, must be a valid `http://` or `https://` URL
  - `tags`: optional (recommended for discoverability), max 20 items
  - Rate limit tightened: `10/minute` → `5/minute; 50/day`
- Removed redundant `agent_inactivity_expire_days` config key (inactivity logic is handled separately via Redis TTL)

### Fixed

- `AgentInfo.tags` description had unescaped double quotes causing a latent syntax issue in `models.py`

## [0.4.1] - 2026-03-03

### Security

- **Rate limiting** (`routes/dependencies.py`, `routes/tasks.py`, `routes/payments.py`, `routes/analytics.py`, `routes/onchain.py`, `routes/registry.py`):
  - Added `@limiter.limit(...)` decorators to all public-facing endpoints.
  - `Limiter` now uses a proxy-aware `_get_real_ip` key function that extracts the real client IP from `X-Forwarded-For` / `X-Real-IP` headers before falling back to `request.client.host`.
  - `storage_uri=settings.redis_url` — rate limit state is now stored in Redis, making limits effective across multiple instances.
- **WebSocket authentication** (`routes/websocket.py`): Moved agent authentication from URL query parameter (`?token=`) to first-message JSON payload (`{"token": "..."}`) to prevent credentials appearing in server logs and proxy access logs.
- **`routes/tasks.py`**: Renamed Pydantic request body parameters from `request` to `body` in `create_task`, `accept_task`, `submit_task`, `review_task` and `estimate_cost` (in `routes/payments.py`) to resolve `SyntaxError: duplicate argument 'request'` introduced by the slowapi `request: Request` requirement.

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



