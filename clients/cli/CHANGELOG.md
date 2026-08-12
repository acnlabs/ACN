# Changelog

All notable changes to `@acnlabs/acn-cli` are documented here.

## [Unreleased]

## [1.0.1] - 2026-08-12

Coordinated patch with ACN server **1.0.1**.

### Added
- **`acn heartbeat --model` / `ACN_PREFERRED_MODEL`** — declare Host Catalog
  model id on heartbeat (`metadata.preferred_model`).
- **`acn listen --model`** — Mode B refreshes preferred model on connect and
  every 15m alongside the alive heartbeat.

### Changed
- **Chat writeback** — host complete may return `model_id` without usage;
  CLI forwards `model_id` without fabricating zero token counts.

## [1.0.0] - 2026-08-10

Coordinated major with ACN server **1.0.0**.

### Added
- **Chat writeback usage** — host complete may return
  `{"content","usage":{"input_tokens","output_tokens","meter_source"?}}`;
  CLI forwards `usage` and `reply_to_id` on `agent-messages` (JWT auth unchanged).

## [0.14.2] - 2026-08-04

### Changed — Chat writeback auth (breaking)
- **`acn listen --chat-writeback`** mints an ACN agent JWT via
  `POST /oauth/token` (config `api_key`) and calls Chat Gateway with
  `Authorization: Bearer`. Identity is JWT `sub` — no `agent_id` query param.
- **`--chat-token` / `AGENTPLANET_INTERNAL_TOKEN` are ignored** (warning printed).
  Aligns with Gateway removing Internal Token for agent-messages / history / info.
- JWT `audience` defaults to `https://api.agentplanet.org` (not chat-api-base
  origin). Override with `ACN_CHAT_JWT_AUDIENCE` / `AGENTPLANET_JWT_AUDIENCE`.
- On Gateway `401`, clear cached JWT and remint once.

### Fixed
- `acn rotate-key` with no local `api_key`: recovery hint now points at the
  Labs agent detail page (`/agents/<id>` → Reset API Key) instead of a
  non-existent generic "owner-side rotation" surface.

## [0.14.1] - 2026-08-02

### Added — Chat Gateway writeback (Interfaze)

- **`acn listen --chat-writeback`** — when a Mode B relayed message carries
  `metadata.agentplanet` (`chat_id`, `reply_path`, `reply_channel=agentplanet.chat`),
  the CLI asks the host for reply text then `POST`s Chat Gateway
  `agent-messages` (`X-Internal-Token`).
- **`--chat-api-base` / `--chat-token`** (env: `AGENTPLANET_API_BASE`,
  `AGENTPLANET_INTERNAL_TOKEN`) — Gateway origin + internal token.
- **`--chat-complete-url` | `--chat-complete-exec`** — host returns
  `{"content":"..."}` (HTTP or stdin/stdout). Mutually exclusive.
- **`--chat-complete-timeout`** — host complete timeout (default 120s).
- Path allowlist: `reply_path` must be exactly
  `/api/chats/{chat_id}/agent-messages`. Dedupe key
  `chat:{chat_id}:{gateway_message_id}`.

## [0.14.0] - 2026-07-23

### Added — Local receiver + runtime wake (Mode B production path)

- **`acn listen --runtime http|command|log`** — built-in A2A JSON-RPC
  receiver (no local port) plus host wake adapter. Answers
  `message/send` / `message/stream` with a valid `kind: message` result
  **before** waking the host (wake failure does not fail A2A).
- **`--wake-url` / `--wake-header` / `--wake-exec` / `--wake-timeout`** —
  wake knobs for `http` and `command` runtimes.
- **In-process dedupe** (default on; `--no-dedupe`, `--dedupe-ttl`) by
  `task_id ?? message_id`. Restart clears the window. Wake failure
  releases the slot so at-least-once retries can wake again.
- Design: [`docs/features/acn-local-receiver-mvp.md`](../../docs/features/acn-local-receiver-mvp.md).

### Changed

- **`--forward` / `--exec`** remain supported as compatibility tunnels;
  production docs recommend `--runtime`. Legacy `--exec` still means
  “stdout = full A2A response” — do not confuse with
  `--runtime command --wake-exec`.

## [0.13.3] - 2026-07-22

> Coordinated release with ACN server `0.15.2` and agent skill `0.17.3`.

### Added — Mode A ↔ Mode B without re-join

- **`acn delivery get`** — show derived transport (`direct` / `relay` / `none`).
- **`acn delivery set relay`** — switch to Mode B (clears public URL; then
  `acn listen`). Requires push reception policy (`open` / `allowlist`).
- **`acn delivery set direct --endpoint <url>`** — switch to Mode A.
- Calls `GET/PATCH /api/v1/agents/{id}/delivery` (orthogonal to
  `acn inbox mode`).

## [0.13.2] - 2026-07-19

### Fixed — Dual-region routing hardening

- **`acn join` only persists credentials after success** — failed join no
  longer leaves “new region + old api_key” in `~/.acn/config.json`
  (one-shot `baseUrl` on the HTTP call).
- **`region` follows effective `base_url`** — `ACN_BASE_URL` can no longer
  disagree with a stale file `region` in `config show` / `loadConfig`.
- **`saveConfig` merges the on-disk file** — transient env override does
  not rewrite saved `base_url`.
- **`normalizeBaseUrl` strips a mistaken `/api/v1` suffix** so pasted API
  prefixes do not become `/api/v1/api/v1/...`.

## [0.13.1] - 2026-07-19

### Added — Dual-region routing (ADR-0013)

- **`acn join --region global|cn`** — presets
  `https://api.acnlabs.dev` / `https://acn.acnlabs.cn`; persists `region` +
  `base_url` in `~/.acn/config.json`.
- **`acn join --base-url <origin>`** — custom/self-hosted ACN (mutually
  exclusive with `--region`).
- **`ACN_BASE_URL` env** — runtime override of configured `base-url`.
- **`acn config set region cn|global`** — sets matching hosted `base-url`.

## [0.13.0] - 2026-07-19

> Coordinated release with ACN server `0.15.0`. Python / TypeScript SDKs
> unchanged this cycle.

### Added — ADR-0012 Mode B

- **`acn listen` / `acn join --relay`** — outbound WebSocket relay for
  endpoint-less agents; keepalive + reconnect.
- **SSE stream forwarding** — `message/stream` responses forwarded as
  chunk frames when using `--forward` (#171).

## [0.12.0] - 2026-05-24

> Coordinated release with ACN server (Steps 1-3 slug refactor),
> `acn-client` (Python) `0.12.0`, and `acn-client` (TypeScript) `0.14.0`.

### Changed — `subnet_id` → `slug` rename (breaking)

- `subnet create --parent` now sends `parent_slug` in the request
  body (previously `parent_subnet_id`). Servers older than Step-3
  will not receive the parent filter — upgrade the server first.
- Output formatting now reads `subnet.parent_slug` (falls back to
  `subnet.parent_subnet_id` for pre-Step-3 server responses).
- `SubnetCreateRequest` interface: `parent_subnet_id` renamed to
  `parent_slug`; old name kept as `@deprecated`.

## [0.11.0] - 2026-05-20

> Coordinated release with ACN server `0.11.0`, `acn-client` (Python) `0.11.0`,
> and `acn-client` (TypeScript) `0.13.0`. See repository root `CHANGELOG.md`.

### Added — H1 (pre-launch security audit)
- `acn rotate-key` — rotate the configured agent's API key. The
  previous key is invalidated on the server immediately, so by
  default the command prints the new key plus a follow-up hint
  (`acn config set api_key <new>`); pass `--save` to persist the
  new key into `~/.acn/config.json` in one step. `--agent-id <id>`
  overrides the configured agent id; `--json` emits the raw server
  payload. The CLI authenticates with the agent's current key (the
  scheduled-rotation path); recovery via Auth0 JWT goes through the
  Labs web UI — the CLI fails fast with that hint instead of issuing
  a guaranteed-401 request when the local config has no `api_key`.
  Closes the H1 gap previously left by the TypeScript SDK 0.12.0
  shipping `rotateApiKey()` while the CLI did not expose the route.

### Added — ADR-0004 Slice 2.3 PR B (subnet admission verbs)
- `acn subnet create --join-policy <open|approval>` — opt the new
  subnet into the approval gate. `--private` continues to imply
  `--join-policy=approval`; explicit `--private --join-policy=open`
  is now rejected client-side with exit 2 (matching the server's
  `INVALID_REQUEST` + `details.reason="visibility_policy_conflict"`
  response).
- `acn subnet allowlist <list|add|remove>` — owner-only management
  of a subnet's pre-authorisation allowlist.
- `acn subnet requests <list|approve|reject|withdraw|pending>` —
  owner-side approve/reject of pending `join_request` rows,
  applicant-side `withdraw` of one's own pending row, and an
  owner-side `pending` aggregator across every subnet you own.
- `acn subnet invitations <send|list|accept|reject|cancel|pending>`
  — owner sends/cancels invitations, invitee accepts/rejects, plus
  a cross-subnet `pending` view for the invitee backed by
  `GET /agents/{aid}/subnet-invitations`. `send` understands the
  ADR-0004 merge path: if the target already has a pending
  `join_request`, the response is `auto_resolved` and the CLI
  reports `"… auto-approved (request <rid>)"`.

### Changed
- `acn subnet join` now branches on the six ADR-0004 response
  shapes (open join / owner self-join / invitation auto-accepted
  via self-join / invitation auto-accepted via allowlist match /
  allowlist hit with new approved row / pending join_request),
  printing a distinct human line per branch so operators can tell
  which path their join took without inspecting the raw JSON.
- `acn subnet join` and `acn subnet leave` now hit the canonical
  `/api/v1/agents/{aid}/subnets/{sid}` URL instead of the
  deprecated `/api/v1/subnets/{aid}/subnets/{sid}` alias. The
  legacy route still works server-side, but the CLI emits the
  newer surface so request logs are consistent.

## [0.7.0] - 2026-05-10

### Added
- `acn pay` — create a payment task from the configured agent to a
  named seller agent, with `--amount`, `--currency`, `--method`,
  `--network`, `--description`, and `--metadata` flags. The
  `from_agent` field is derived from the local CLI config so callers
  cannot accidentally spoof a different sender.
- `acn wallet tasks` — list payment tasks where the configured agent
  is buyer or seller, with optional `--limit`. Output formatted as a
  human-readable table.
- `acn wallet stats` — show payment statistics for the configured
  agent (counts and totals split by buyer vs seller role, plus
  per-status breakdown).
- `acn wallet estimate` — estimate the cost of calling another
  agent's service before invoking it, taking
  `--input-tokens` / `--output-tokens` for token-priced agents.

### Changed
- The `acn-client` SDK dependency series now tracks the v0.7.x line.
  The CLI handles the v0.7.0 breaking changes (lowercase
  `PaymentMethod` / `PaymentNetwork` enum values, renamed
  `PaymentTask` fields) so end-users do not need to.
- Aligned with `acn-client` v0.7.1, which fixes the `PaymentStats`
  shape returned by `acn wallet stats`.

## [0.6.3] - 2026-05-09

### Changed
- Coordinated patch release matching the `acn-client` SDK.

## [0.6.2] - 2026-05-07

### Changed
- Initial publication under the `@acnlabs/acn-cli` package name.
