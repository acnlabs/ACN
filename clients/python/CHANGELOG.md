# Changelog

All notable changes to `acn-client` are documented here.

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
