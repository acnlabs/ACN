# Changelog

All notable changes to `@acnlabs/acn-cli` are documented here.

## [Unreleased]

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
