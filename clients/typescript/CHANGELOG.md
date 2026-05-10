# Changelog

All notable changes to `acn-client` (TypeScript) are documented here.

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
