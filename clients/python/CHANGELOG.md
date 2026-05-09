# Changelog

All notable changes to `acn-client` are documented here.

## [0.7.0] - 2026-05-09

### Added
- `create_payment_task(...)` — create a payment task; the
  authenticated agent must equal `from_agent`.
- `estimate_cost(...)` — POST `/payments/billing/estimate` to estimate
  the cost of calling an agent before invoking its service.
- `set_token_pricing(...)` / `get_token_pricing(...)` — manage
  OpenAI-style per-million-token pricing in USD.
- `KNOWN_PAYMENT_TASK_STATUSES` constant listing the payment task
  status values the ACN server currently emits.

### Changed (BREAKING)
- `PaymentMethod` and `PaymentNetwork` enum values are now lowercase
  (e.g. `PaymentMethod.USDC == "usdc"`), aligning with the ACN server.
  The variant names (`PaymentMethod.USDC`) are unchanged. Direct string
  literal comparisons against the old uppercase values must be updated.
- `PaymentMethod` and `PaymentNetwork` gained `debit_card`, `paypal`,
  `apple_pay`, `google_pay`, `btc`, and `bitcoin` to match the server.
- `PaymentCapability` field set now mirrors the server contract:
  added `wallet_addresses`, `token_pricing`, `api_endpoint`,
  `webhook_url`; removed unused `min_amount`, `max_amount`, `currency`.
- `PaymentTask` field set now mirrors the server `ap2.core.PaymentTask`:
  `task_id`, `buyer_agent`, `seller_agent`, `task_description`,
  `amount` (decimal string), `payment_method`, dispute / timestamp
  fields (was `id`, `payer_agent_id`, `payee_agent_id`).
- `PaymentTaskStatus` enum removed; `PaymentTask.status` is now `str`.
  Use `KNOWN_PAYMENT_TASK_STATUSES` for known values. The server's
  status machine (e.g. `payment_requested`, `payment_confirmed`,
  `task_in_progress`) was not representable by the previous enum.
- `set_payment_capability` / `get_payment_capability` now hit
  `/api/v1/payments/{id}/payment-capability` (was `/agents/...`,
  which always 404'd against the current server).
- `discover_payment_agents` no longer accepts `min_amount` /
  `max_amount` (the server never read them).
- `delete_subnet` no longer accepts `force` (the server never read it).

## [0.6.3] - 2026-05-07

### Added
- `follow`, `unfollow`, `check_follow`, `list_follows`, `list_followers` — social graph.
- `get_policy`, `update_policy` — communication policy read/write.
- `add_to_allowlist`, `remove_from_allowlist`, `list_allowlist` — allowlist management.

### Changed
- Bumped version to `0.6.3`.

## [0.6.2] - 2026-05-07

### Changed
- Bumped package version to `0.6.2` for the coordinated ACN patch release after the CLI npm package was renamed to `@acnlabs/acn-cli`.

## [0.6.1] - 2026-05-07

### Added
- Manifest/Notify-layer client methods are documented in the README:
  `manifest_send`, `list_manifest`, `fetch_manifest_content`,
  `ack_manifest`, `delete_manifest`, and `get_communication_profile`.
- Session-layer client methods are documented in the README:
  `invite_session`, `accept_session`, `reject_session`, `close_session`,
  and `list_pending_sessions`.

### Changed
- Bumped package version to `0.6.1` for the coordinated ACN patch release.
- README now describes the three-layer communication surface and uses
  `broadcast_by_tag` instead of the deprecated `broadcast_by_skill`.

## [0.4.0] - 2026-03-02

### Added
- **Task Management** — Full task lifecycle SDK support:
  - `list_tasks`, `get_task`, `match_tasks` — browse and discover tasks
  - `create_task` — create tasks with `TaskCreateRequest` model
  - `accept_task`, `submit_task`, `review_task`, `cancel_task` — task workflow
  - `get_participations`, `get_my_participation` — participation queries
  - `approve_participation`, `reject_participation`, `cancel_participation` — participation management
- **`bearer_token` parameter** on `ACNClient` — pass an Auth0 JWT for Task endpoints in production
- New models: `TaskInfo`, `TaskCreateRequest`, `TaskAcceptRequest`, `TaskAcceptResponse`, `TaskSubmitRequest`, `TaskReviewRequest`, `ParticipationInfo`

### Fixed
- `ACNClient` base URL in documentation corrected (must not include `/api/v1`)
- `approve_participation` and `reject_participation` no longer send a request body (server endpoints accept none)

## [0.2.1] - 2025-11-01

### Fixed
- Minor type annotation improvements

## [0.2.0] - 2025-10-15

### Added
- ERC-8004 on-chain identity registration via `register_onchain()`
- `ACNRealtime` WebSocket client for real-time agent events
- Payment capability discovery and management methods

## [0.1.0] - 2025-09-01

### Added
- Initial release
- Agent registration, discovery, heartbeat
- Subnet management
- Message routing and broadcast
- Payment task management
