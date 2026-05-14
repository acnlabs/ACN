# Changelog

All notable changes to `acn-client` (TypeScript) are documented here.

## [0.11.2] - 2026-05-14

### Changed
- **Subnet membership paths now canonical**: `joinSubnet`, `leaveSubnet`,
  and `getAgentSubnets` switch to `/api/v1/agents/{agent_id}/subnets/…`,
  matching every other agent-side endpoint (heartbeat, claim, transfer,
  …). The legacy `/api/v1/subnets/{agent_id}/subnets/…` paths the SDK
  used in 0.11.1 still work but are now `deprecated=True` in the
  backend's OpenAPI schema; this release moves callers onto the
  canonical surface so the legacy paths can eventually be removed.
- **Backend requirement**: ACN backend must carry the canonical-routes
  patch (released alongside this SDK). Earlier backend builds only
  served the legacy paths and will 404 on the canonical ones. If you
  pin to an older backend, stay on 0.11.1.

No caller-code changes required — method signatures are unchanged.

## [0.11.1] - 2026-05-14

### Fixed
- **Subnet membership paths**: `joinSubnet`, `leaveSubnet`, and
  `getAgentSubnets` now send requests to the ACN backend's actual path —
  `/api/v1/subnets/{agent_id}/subnets/{subnet_id}` and
  `/api/v1/subnets/{agent_id}/subnets`. Earlier builds (≤ 0.11.0) called
  `/api/v1/agents/{agent_id}/…`, which 404'd against any live ACN server.
  No caller-code changes required — the SDK method signatures are
  unchanged.

## [0.11.0] - 2026-05-14

This release adds first-class support for ACN's **Org-Harness Task Pool**
(create / accept / submit / review / cancel tasks, subnet harness webhook
registration). It also fixes a critical authentication bug that prevented
every authenticated route from working in earlier 0.10.x builds.

### Fixed
- **Auth header**: requests now send `Authorization: Bearer <apiKey>` instead
  of the unsupported `X-API-Key`. The ACN server has only ever accepted
  Bearer, so every authenticated route from earlier 0.10.x silently 401'd.
  No caller-code changes required; if you had a reverse proxy stripping the
  `Authorization` header to work around the old behaviour, remove that rule.
- `reviewTask(approved, notes)` now sends the third argument as the `notes`
  field expected by `TaskReviewRequest`. Earlier 0.10.x sent it as
  `feedback`, which the server silently dropped — review notes never
  reached the audit trail. No caller-code changes required; the SDK
  signature's third parameter was renamed `feedback` → `notes` for clarity.

### Changed (BREAKING)
- `TaskStatus` union now mirrors the server enum exactly:
  - **Removed**: `'in_review'` (never emitted by the server)
  - **Added**: `'submitted'`, `'rejected'`
  - Final values: `'open' | 'in_progress' | 'submitted' | 'completed' | 'rejected' | 'cancelled'`
  - **Action required**: code that compared `task.status === 'in_review'`
    must now compare against `'submitted'`.
- `Task.reward` is a decimal string (e.g. `"10.00"`) matching the server's
  `TaskResponse.reward`. The convenience numeric alias `reward_amount` is
  available as an optional read-only field.
- `TaskCreateRequest` now reflects the server contract:
  - **Required**: `title`, `description` (10–10 000 chars), `reward` (string),
    `deadline_hours` (1–2 160).
  - **Removed**: `reward_amount: number` (was incorrect — server requires
    string `reward`).
  - **Added (optional)**: `max_participants`, `task_type`, `required_tags`,
    `auto_approve`, `use_escrow`, `reward_currency`.

### Added
- Task Pool methods:
  - `createTask(req)` — POST `/api/v1/tasks`
  - `getTask(taskId)` — GET `/api/v1/tasks/{id}`
  - `listTasks(opts?)` — GET `/api/v1/tasks` with `status / creator_id /
    assignee_id / limit / offset` filters
  - `acceptTask(taskId, message?)` — POST `/api/v1/tasks/{id}/accept`,
    returns `TaskAcceptResponse` (`{ task, participation_id }`)
  - `submitTask(taskId, submission, opts?)` — POST `/api/v1/tasks/{id}/submit`
    with optional `artifacts` and `participationId`
  - `reviewTask(taskId, approved, notes?)` — POST `/api/v1/tasks/{id}/review`
  - `cancelTask(taskId)` — POST `/api/v1/tasks/{id}/cancel`
  - `getTaskParticipations(taskId)` — GET `/api/v1/tasks/{id}/participations`
- `registerSubnetHarness(subnetId, harnessUrl, harnessSecret?)` — PATCH
  `/api/v1/subnets/{id}/harness` to register (or clear, via `null`) a
  webhook URL with optional HMAC secret. Used by the Paperclip ACN plugin
  and any other Org-Harness consumer.
- Exported types: `Task`, `TaskStatus`, `TaskAcceptResponse`,
  `TaskCreateRequest`, `TaskListOptions`, `TaskListResponse`,
  `Participation`, `ParticipationListResponse`, `SubnetHarnessRequest`.

### Notes
- CHANGELOG entries for `0.8.0`, `0.9.0`, and `0.10.0` were never published.
  This release consolidates everything that has landed on the 0.10 line.

## [0.7.1] - 2026-05-10

### Changed (BREAKING)
- `PaymentStats` field set now mirrors the server's
  `PaymentTaskManager.get_payment_stats` response — the previous
  `total_received / total_sent / transaction_count / avg_amount`
  fields were never emitted by the server, so reads silently returned
  all-zero stats.
  - New shape: `total_tasks`, `as_buyer` (`PaymentRoleStats`),
    `as_seller` (`PaymentRoleStats`), `by_status: Record<string, number>`,
    `completed_transactions`.
  - New helper interface `PaymentRoleStats` (`count`, `total_amount` as
    decimal string) is exported from the package root.

## [0.7.0] - 2026-05-09

### Added
- `createPaymentTask(...)` — create a payment task; the authenticated
  agent must equal `from_agent`.
- `estimateCost(...)` — POST `/payments/billing/estimate` to estimate
  the cost of calling an agent before invoking its service.
- `setTokenPricing(...)` / `getTokenPricing(...)` — manage OpenAI-style
  per-million-token pricing in USD.
- `KNOWN_PAYMENT_TASK_STATUSES` constant listing the payment task
  status values the ACN server currently emits, plus a derived
  `PaymentTaskStatus` union type.

### Changed (BREAKING)
- `PaymentMethod` and `PaymentNetwork` are now lowercase string-literal
  unions (e.g. `"usdc"`, `"base"`), aligning with the ACN server.
  Direct string-literal comparisons against the old uppercase values
  must be updated.
- `PaymentMethod` and `PaymentNetwork` gained `debit_card`, `paypal`,
  `apple_pay`, `google_pay`, `btc`, and `bitcoin` to match the server.
- `PaymentCapability` field set now mirrors the server contract: added
  `wallet_addresses`, `token_pricing`, `api_endpoint`, `webhook_url`;
  removed unused `min_amount`, `max_amount`, `currency`.
- `PaymentTask` field set now mirrors the server `ap2.core.PaymentTask`:
  `task_id`, `buyer_agent`, `seller_agent`, `task_description`,
  `amount` (decimal string), `payment_method`, dispute / timestamp
  fields (was `id`, `payer_agent_id`, `payee_agent_id`).
- `PaymentTaskStatus` is no longer a hardcoded enum; it is a derived
  union over `KNOWN_PAYMENT_TASK_STATUSES` so additions on the server
  are forward-compatible without an SDK release.
- `setPaymentCapability` / `getPaymentCapability` now hit
  `/api/v1/payments/{id}/payment-capability` (was `/agents/...`,
  which always 404'd against the current server).
- `discoverPaymentAgents` no longer accepts `min_amount` / `max_amount`
  (the server never read them).
- `deleteSubnet` no longer accepts `force` (the server never read it).

### Compatibility
- `getPaymentCapability` normalizes the server's `payment_methods`
  response into `supported_methods` so application code can read either
  field. With ACN backend ≥ commit `ccc0a1d` (May 10 2026) the server
  itself emits both names; this normalization remains in place as
  back-compat for older ACN deployments.

## [0.6.3] - 2026-05-07

### Changed
- Bumped to `0.6.3` for coordinated ACN patch release.
