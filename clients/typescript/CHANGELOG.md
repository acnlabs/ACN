# Changelog

All notable changes to `acn-client` (TypeScript) are documented here.

## [Unreleased]

## [1.0.0] - 2026-08-10

Coordinated major with ACN server **1.0.0** (soft著 V1.0.0). No intentional
API break beyond the 0.15.x surface.

### Added

- `OrgWorkItem.metadata` / create+update request fields — opaque JSON object
  passthrough (aligned with Python SDK; server stores only, 64 KiB cap).

## [0.15.0] - 2026-07-23

### Added — Org Harness Work Port (builtin_work)

- `getOrg` / `createOrg`
- `createWork` / `updateWork` / `listWork`
- `tickOrgLoop`
- Types: `Org`, `OrgWorkItem`, `OrgWorkStatus`, …
- Helper: `orgSubnetId(org)`
- `ACNError.body` / `.reason` / `.boundOrgIdHint` for Org conflict recovery
  (e.g. subnet already bound → reuse `org_…`)

## [0.14.2] - 2026-07-23

### Added — Delivery transport (ADR-0012 Mode A ↔ Mode B)

- `getDelivery(agentId)` — derived transport (`direct` / `relay` / `none`).
- `setDelivery(agentId, delivery, endpoint?)` — switch Mode A↔B without
  re-registering (`GET/PATCH /agents/{id}/delivery`).
- Types: `DeliveryTransport`, `DeliveryTransportSet`, `DeliveryResponse`.

## [0.14.1] - 2026-07-19

### Added — Dual-region helpers (ADR-0013)

- `ACN_HOSTED_URLS`, `hostedBaseUrl()`, `normalizeBaseUrl()`,
  `resolveHostedBaseUrl()`.
- `ACNClient({ region: 'global' | 'cn' })` — mutually exclusive with
  `baseUrl`; also honors `ACN_BASE_URL`.

## [0.14.0] - 2026-05-24

> Coordinated release with ACN server (Steps 1-3 slug refactor),
> `acn-client` (Python) `0.12.0`, and `@acnlabs/acn-cli` `0.12.0`.

### Changed — `subnet_id` → `slug` rename (breaking)

- `SubnetInfo.slug` is the new canonical wire-side identifier
  (replaces `subnet_id`). The old `subnet_id` field is kept as
  `@deprecated` for one release cycle.
- `SubnetInfo.parent_slug` replaces `parent_subnet_id`
  (`parent_subnet_id` kept as `@deprecated`).
- `SubnetCreateRequest.parent_subnet_id` renamed to `parent_slug`.
- `SubnetCreateResponse.subnet_id` renamed to `slug`.
- `AgentSearchOptions.subnet_id` renamed to `slug`.
- `Task.subnet_id` renamed to `subnet_slug` (matches the server's
  direct wire field after `serialization_alias` removal).
- `TaskCreateRequest.subnet_id` renamed to `subnet_slug`.
- `SubnetAllowlistListResponse`, `SubnetJoinRequestRow`,
  `SubnetJoinRequestListResponse`, `SubnetInvitationListResponse`:
  `subnet_id` renamed to `slug`.
- All `ACNClient` method parameters previously named `subnetId` or
  `parentSubnetId` are now `slug` / `parentSlug`.

## [0.13.0] - 2026-05-20

> Coordinated release with ACN server `0.11.0`, `acn-client` (Python) `0.11.0`,
> and `@acnlabs/acn-cli` `0.11.0`. See repository root `CHANGELOG.md`.

### Fixed — re-export gap on session / manifest / search types

- `index.ts` now also re-exports the seven public types in this
  group that had been quietly missing for longer than the recent
  feature work — same fix shape as the previous PR (PR-H), just
  for older surfaces:
  - `AgentSearchStatus` — the `'online' | 'offline' | 'all'`
    literal used by `AgentSearchOptions.status`.
  - `ManifestMessageType` / `ManifestSendRequest` — the input
    type for `manifestSend` and the literal used by
    `manifestList({ messageType })`.
  - `SessionStatus` / `SessionEntry` / `SessionInviteRequest` /
    `PendingSessionsResponse` — return / argument types for
    `inviteSession`, `acceptSession`, `rejectSession`,
    `closeSession`, and `listPendingSessions`.
- All seven types were already public (used in `client.ts`
  method signatures), so callers reaching them through the
  inferred return type kept working — but there was no clean
  way to spell e.g. `function handle(s: SessionEntry)` without
  the deep import workaround. This closes that gap.

Wire / runtime behaviour unchanged. Verified locally:
`npx tsc --noEmit` clean, all 27 vitest cases still pass.

### Fixed — re-export gap on public types

- `index.ts` now re-exports the ten ADR-0004 admission types
  alongside the methods that were already public:
  `SubnetJoinPolicy`, `SubnetAllowlistEntry`,
  `SubnetAllowlistListResponse`, `SubnetJoinRequestRow`,
  `SubnetJoinRequestListResponse`,
  `SubnetJoinRequestListOptions`,
  `SubnetInvitationListResponse`,
  `SubnetInvitationListOptions`,
  `SubnetInvitationSendResponse`,
  `AgentSubnetInvitationsResponse`. Pre-this-PR, callers had to
  reach into `'acn-client/dist/types'` (deep import) or fall back
  to `Parameters<typeof client.subnetAllowlistAdd>[…]` gymnastics
  to type a variable holding e.g. an invitation row. Now a clean
  top-level `import type { SubnetInvitationSendResponse } from 'acn-client'`
  works as expected. Wire / runtime behaviour unchanged.
- Same fix for `CommunicationProfile` (the typed return of
  `getCommunicationProfile`) — also missing from the top-level
  re-exports despite being a long-standing public type, and
  newly carrying the `unread_manifest_count: number` field from
  the previous PR.

### Added — manifest-mode reachability (mirrors server PR #87)

- `CommunicationProfile.unread_manifest_count: number` — surfaces
  the pending manifest queue length on the typed interface
  returned by `getCommunicationProfile`. Senders observing a
  large or growing value should treat the agent as effectively
  unreachable in `'manifest'` / `'allowlist'` mode.
- `CommunicationPolicyResponse.warning?: string` — conditional
  field that the server emits only when the post-update mode is
  `'manifest'` or `'allowlist'`. Carries a human-readable
  reminder that messages from non-trusted senders divert to the
  manifest queue and require active polling. CLIs / dashboards
  should surface this verbatim so operators don't silently lock
  themselves out.
- `src/communication.test.ts` (new) — 5 vitest cases pinning:
  unread-count round-trip on `'manifest'` mode and the
  `0` steady-state on `'open'` mode; `warning` passthrough on
  both gated modes (`'manifest'` and `'allowlist'`); and
  `warning` absence on `'open'` mode.

### Added (ADR-0003 nested subnets)

- `listChildren(parentSubnetId)` — `GET
  /api/v1/subnets/{parentSubnetId}/children`; returns the
  `subnets` array from `{ count, subnets }`. Parity with Python
  SDK `list_children` and CLI `acn subnet list --parent`.
- `promoteSubnet(subnetId)` — `POST /api/v1/subnets/{subnetId}/promote`;
  promotes a `task_scoped` subnet to `persistent` (owner-only,
  idempotent). Parity with Python SDK `promote_subnet` and CLI
  `acn subnet promote`.
- `SubnetInfo` extended with optional ADR-0003 / harness fields
  (`subnet_id`, `parent_subnet_id`, `lifecycle`, `linked_task_id`,
  …) plus an index signature so future server fields do not
  break callers — matches the Python SDK's forward-compat posture.
- New `SubnetChildrenListResponse` type for the children-list
  envelope.
- 2 vitest cases in `src/admission.test.ts` pinning verb + path
  for `listChildren` and `promoteSubnet`.

- `SubnetCreateRequest` now exposes the three ADR-0003 nesting
  fields, bringing the TypeScript SDK to parity with the Python
  SDK (which has carried these since the original ADR-0003 work):
  - `parent_subnet_id?: string` — promote a new subnet to a child
    of an existing top-level subnet. Single-layer cap; immutable
    after creation.
  - `lifecycle?: SubnetLifecycle` (`'persistent' | 'task_scoped'`)
    — defaults to `'persistent'` when omitted, preserving the
    legacy "flat top-level persistent subnet" shape.
  - `linked_task_id?: string` — bind a `'task_scoped'` subnet to
    a task; the server auto-dissolves the subnet when that task
    reaches a terminal state.
- New `SubnetLifecycle` type alias re-exported from `index.ts`.
- 3 vitest tests in `src/admission.test.ts` pinning round-trip
  serialisation: `parent_subnet_id`, `lifecycle + linked_task_id`
  pair, and the back-compat case where all three fields are
  absent from the wire body when not set by the caller.

### Added (ADR-0004 subnet admission)

- `SubnetCreateRequest.join_policy?: SubnetJoinPolicy` — opt
  subnets into the admission state machine at creation time.
  `'open'` (or omitted) preserves legacy unrestricted self-join;
  `'approval'` gates membership through allowlist /
  join_request / invitation. Immutable post-creation.
- New types in `types.ts` (un-modeled passthrough mirrors of the
  server's un-typed JSON responses, each with a `[key: string]:
  unknown` index signature so future server-added fields don't
  break callers):
  - `SubnetJoinPolicy`, `SubnetAllowlistEntry`,
    `SubnetAllowlistListResponse`
  - `SubnetJoinRequestRow` — single audit-row shape covering all
    three row kinds (`'join_request' | 'allowlist_auto' |
    'invitation'`).
  - `SubnetJoinRequestListResponse`, `SubnetInvitationListResponse`,
    `AgentSubnetInvitationsResponse`
  - `SubnetInvitationSendResponse` — discriminated union of the
    202 normal-path `{ invitation_id, status: 'pending' }` and
    the 200 merge-path `{ auto_resolved: true, resolved_kind:
    'join_request', request_id }` shapes; discriminate on
    `auto_resolved` to dispatch.
  - `SubnetJoinRequestListOptions`, `SubnetInvitationListOptions`
- 13 admission methods on `ACNClient` (`subnet*` prefix to avoid
  colliding with the existing inbox `addToAllowlist` surface):
  - Allowlist (3): `subnetAllowlistAdd` / `_Remove` / `_List` —
    owner only.
  - Join requests (4): `subnetJoinRequestApprove` / `_Reject` /
    `_List` — owner only; `_Withdraw` — applicant (self) only.
  - Invitations (5): `subnetInvitationSend` / `_Cancel` /
    `_List` — owner only; `_Accept` / `_Reject` — invitee
    (self) only.
  - Agent-side: `agentSubnetInvitations` — invitee's
    cross-subnet pending view (self only).
- 19 vitest tests in `src/admission.test.ts` pinning verb +
  path + body/params for every method by stubbing
  `globalThis.fetch` per test and asserting on the captured
  `(url, init)` tuple. **First test file in the TypeScript
  SDK** — establishes the testing baseline (vitest 2.x).

### Changed (devDependencies)

- `vitest` bumped from `^1.0.0` to `^2.1.9`. The 1.x install
  pinned by the existing `package-lock.json` had a broken
  `dist/cli-wrapper.js` resolution against Node 20 and could
  not actually run tests — adding the first test file required
  unblocking it. No source-code impact.

### Changed (type narrowing — alive-as-SSOT follow-up)

- **`AgentStatus` narrowed from `'online' | 'offline' | 'busy'` to
  `'online' | 'offline'`**. The server has not emitted `'busy'` since
  the alive-as-single-source-of-truth refactor (ACN
  `docs/agent-registry-removal.md`) — agent liveness is now derived
  from a Redis TTL key, which is inherently binary. The `'busy'`
  literal was an unreachable dead branch in any consumer code.

  This is a **compile-time** narrowing only — there is no runtime
  behaviour change because no real response ever carries the value.
  Code that does an exhaustive `switch (status)` will drop the
  `case 'busy':` arm; code that does
  `if (status === 'busy')` will become statically false and TypeScript
  will flag it as unreachable. Code that treats `AgentStatus` as
  opaque (e.g. just displays it) needs no change.

  `AgentSearchStatus` (`AgentStatus | 'all'`) inherits the
  narrowing automatically.

## [0.12.0] - 2026-05-14

### Added
- **`rotateApiKey(agentId)` (H1 — pre-launch security audit)**:
  rotate an agent's API key without re-registering the agent. The
  server mints a fresh `acn_*` plaintext, returns it exactly once,
  stores only its SHA-256 hash, and immediately invalidates the
  previous key. Subnet membership, ERC-8004 binding, reputation, and
  the agent's `agent_id` are all preserved — this is the missing piece
  that turns H1 from a partial fix (server stores hashes but callers
  can't rotate) into a complete one. Authorization is dual-track on
  the server side: the agent itself (via its current key) or the
  owner (via Auth0 JWT) is accepted; pick whichever fits your flow.

  Typical usage::

      const { api_key } = await client.rotateApiKey(agentId);
      client.config.apiKey = api_key; // or rebuild the client

### Backend requirement
- ACN backend must carry the H1 rotate-key patch (released alongside
  this SDK). Earlier backend builds will 404 on `POST
  /api/v1/agents/{agent_id}/rotate-key`. If you pin to an older
  backend, stay on 0.11.2.

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
