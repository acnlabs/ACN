# Changelog

All notable changes to `acn-client` are documented here.

## [Unreleased]

### Added

- `OrgWorkItem.metadata` / create+update request fields — opaque JSON object
  passthrough (`metadata.wave` for Org wave metrics; server stores only,
  64 KiB serialised cap).

## [0.13.0] - 2026-07-23

### Added — Org Harness Work Port (parity with TypeScript 0.15)

- `get_org` / `create_org` / `create_work` / `update_work` / `list_work` /
  `tick_org_loop` on `ACNClient` (`/api/v1/orgs*` · default Work Port
  `builtin_work`).
- Models: `Org`, `OrgCreateRequest`, `OrgWorkItem`, `OrgWorkCreateRequest`,
  `OrgWorkUpdateRequest`, `OrgWorkListResponse`, `OrgLoopTickResponse`,
  `OrgFencing`, `KNOWN_ORG_WORK_STATUSES`, helper `org_subnet_id()`.
- `ACNError.body`, `.reason`, `.bound_org_id_hint` for Org conflict recovery
  (e.g. subnet already bound).

## [0.12.2] - 2026-07-23

### Added — Delivery transport (ADR-0012 Mode A ↔ Mode B)

- `get_delivery(agent_id)` — derived transport (`direct` / `relay` / `none`).
- `set_delivery(agent_id, delivery, *, endpoint=None)` — switch Mode A↔B
  without re-registering (`GET/PATCH /agents/{id}/delivery`).

## [0.12.1] - 2026-07-19

### Added — Dual-region helpers (ADR-0013)

- `ACN_HOSTED_URLS`, `hosted_base_url()`, `normalize_base_url()`,
  `resolve_hosted_base_url()`.
- `ACNClient(..., region="global"|"cn")` — mutually exclusive with
  `base_url`; also honors `ACN_BASE_URL` when neither is set (else
  localhost default unchanged).

## [0.12.0] - 2026-05-24

> Coordinated release with ACN server (Steps 1-3 slug refactor),
> `@acnlabs/acn-cli` `0.12.0`, and `acn-client` (TypeScript) `0.14.0`.

### Changed — `subnet_id` → `slug` rename (breaking)

- `SubnetInfo.parent_subnet_id` renamed to `parent_slug`.
  Old wire key `parent_subnet_id` is still accepted via
  `AliasChoices` for backward-compatible parsing of responses
  from pre-Step-3 servers.
- `SubnetCreateRequest.parent_subnet_id` renamed to `parent_slug`.
  Callers must update keyword argument at construction site.
- `SubnetInfo.id` now accepts both `slug` (new server wire key) and
  `subnet_id` (legacy) via `AliasChoices`.
- `AgentSearchOptions.subnet_id` renamed to `slug`.
- `ACNClient.list_subnets(parent_subnet_id=...)` parameter renamed
  to `parent_slug`.
- `ACNClient.list_children(parent_slug)` — no change (was already
  `parent_slug` internally).
- All other `ACNClient` method parameters that previously used
  `subnet_id` now use `slug`.

## [0.11.0] - 2026-05-20

> Coordinated release with ACN server `0.11.0`, `@acnlabs/acn-cli` `0.11.0`,
> and `acn-client` (TypeScript) `0.13.0`. See repository root `CHANGELOG.md`
> for the full cross-package summary.

### Changed — Pydantic v2 idiomatic config (no behavioural change)

- Migrate the three remaining `class Config: populate_by_name =
  True` blocks (on `AgentInfo`, `AgentRegisterRequest`, and
  `SubnetInfo`) to the v2-idiomatic
  `model_config = ConfigDict(populate_by_name=True)` form.
  Pydantic emits `PydanticDeprecatedSince20: Support for
  class-based config is deprecated, use ConfigDict instead.
  Deprecated in Pydantic V2.0 to be removed in V3.0.` for the
  legacy form, so this also future-proofs the SDK for Pydantic
  v3. `ConfigDict` was already imported and already in use by
  every other model in the file — this is the catch-up batch.
- The behaviour (`populate_by_name=True` lets callers construct
  `AgentInfo` / `SubnetInfo` either by their canonical Python
  name `id` or by the wire-side alias `agent_id` / `subnet_id`)
  is byte-identical. Verified by running the full SDK pytest
  suite with `-W error::DeprecationWarning` — 65 passed, zero
  warnings.

### Added — manifest-mode reachability (mirrors server PR #87)
- `CommunicationProfile.unread_manifest_count: int` — surfaces the
  pending manifest queue length on the typed model returned by
  `get_communication_profile`. Defaults to `0` for back-compat with
  servers older than PR #87 (or test harnesses that ship the legacy
  three-field payload). Senders observing a large or growing value
  should treat the agent as effectively unreachable in `manifest` /
  `allowlist` mode.
- `update_policy` docstring now documents the conditional `warning`
  field that the server emits when the post-update mode is
  `'manifest'` or `'allowlist'`. The SDK keeps the raw-`dict`
  return contract (consistent with `rotate_api_key` and the 13
  admission verbs) so callers can surface the warning verbatim in
  CLIs / dashboards without a SDK-layer schema gate.
- `tests/test_communication_profile.py` (new) — 4 regression tests
  pinning the unread-count round-trip, the legacy-payload default,
  the conditional `warning` passthrough on `manifest` mode, and the
  warning's absence on `open` mode.

### Added — H1 (pre-launch security audit)
- Regression tests for `rotate_api_key` (the SDK method itself shipped
  in 0.10.0 alongside the server-side endpoint, but had no test
  coverage). `tests/test_rotate_api_key.py` pins the wire shape
  (`POST /api/v1/agents/{id}/rotate-key` with no body / params),
  the raw-`dict` return contract (forward-compatible with future
  server fields), and path-encoded `agent_id` handling.

### Added (ADR-0004 subnet admission)
- `SubnetCreateRequest.join_policy: str | None` — opt subnets into
  the approval state machine at creation time. `None` (default) ⇒
  server-side `"open"` (legacy unrestricted self-join);
  `"approval"` ⇒ admission flow gates membership through
  allowlist / join_request / invitation.
- 13 admission methods on `ACNClient`, returning raw
  `dict[str, Any]` (un-modeled to mirror the server's un-typed
  JSON responses):
  - `subnet_allowlist_add(subnet_id, agent_id)`
  - `subnet_allowlist_remove(subnet_id, agent_id)` — idempotent
    (204 even when entry absent).
  - `subnet_allowlist_list(subnet_id, *, limit, offset)` —
    owner-only.
  - `subnet_join_request_approve(subnet_id, request_id, *, note)`
  - `subnet_join_request_reject(subnet_id, request_id, *, note)`
  - `subnet_join_request_withdraw(subnet_id, request_id, *, note)`
    — applicant-only.
  - `subnet_join_request_list(subnet_id, *, kind, status, limit,
    offset)` — defaults `kind="join_request"`; `kind="invitation"`
    is rejected (use `subnet_invitation_list` instead).
  - `subnet_invitation_send(subnet_id, agent_id, *, note)` —
    returns either the normal-path `{invitation_id, status}` or
    the merge-path `{auto_resolved, resolved_kind, request_id}`
    payload verbatim; branch dispatch is the caller's
    responsibility.
  - `subnet_invitation_accept(subnet_id, request_id, *, note)`
  - `subnet_invitation_reject(subnet_id, request_id, *, note)`
  - `subnet_invitation_cancel(subnet_id, request_id, *, note)` —
    owner-side cancel; row goes to `withdrawn` (distinct from
    invitee `rejected`).
  - `subnet_invitation_list(subnet_id, *, status, limit, offset)`
    — owner-only.
  - `agent_subnet_invitations(agent_id)` — invitee's cross-subnet
    pending-invitation view; self-only, `status=pending` only.
- 20 regression tests in `tests/test_subnet_admission.py` pinning
  the wire shape (verb + path + body/params) for every method,
  plus the `SubnetCreateRequest.join_policy` round-trip.

### Removed (alive-as-SSOT follow-up)

- **`AgentStatus.BUSY` removed** (was deprecated since the previous
  unreleased cycle). The ACN server stopped emitting `"busy"` during
  the 2026-05 alive-as-SSOT refactor — liveness is a binary Redis
  TTL key (`ONLINE` / `OFFLINE`). The deprecation warning shim and
  the `_AgentStatusMeta` metaclass are gone.

  **Migration:** replace `AgentStatus.BUSY` with `AgentStatus.OFFLINE`.

### Changed
- `__version__` is now read at import time from installed package metadata
  (`importlib.metadata.version("acn-client")`) instead of a hard-coded
  string. This makes `pyproject.toml` the single source of truth and
  eliminates the kind of drift that previously had `__version__` reporting
  `0.7.1` while the published wheel was `0.10.0`.

## [0.10.0] - 2026-05-13

> **Backfilled retroactively on 2026-05-19** — this entry was missing from
> CHANGELOG when 0.10.0 was published. Content reconstructed from commit
> `72ba729` ("feat: grader loop, agent task history, and v0.10.0 sync").

### Added
- `get_agent_task_history(agent_id, ...)` — `GET /api/v1/tasks/agent/{agent_id}/history`
  for agent self-reflection.
- `Task.max_resubmit_attempts` — caps automated grader-loop retries per
  participant after rejection (`None` = unlimited).
- `Participation.resubmit_count` — formalized as a dedicated field
  (was previously stored in `metadata`).

### Server-side counterparts (not SDK API surface but worth noting)
- New webhook event `participation.rejected` for Org Harness grader loops.
- `submit_task` service enforces `max_resubmit_attempts`.

## [0.9.0] - 2026-05-12

> **Backfilled retroactively on 2026-05-19** — content reconstructed from
> commit `d083ced` ("feat(org-harness): pluggable Org Harness interface v0.9.0").

### Added
- `set_subnet_harness(subnet_id, harness_url, harness_secret)` —
  registers (or clears) the per-subnet Org Harness webhook for external
  orchestrators (Paperclip, OpenHarness, etc.).
- `SubnetInfo.harness_url` / `SubnetInfo.harness_registered` — prospective
  joiners can now discover the governing orchestration system before joining.
  `harness_secret` is write-only and is never returned by the API.

## [0.8.0] - 2026-05-12

> **Backfilled retroactively on 2026-05-19** — content reconstructed from
> commit `c5d17c0` ("chore(release): bump to v0.8.0, sync docs and skill").

### Changed
- Version bump to align with server / CLI release train; no SDK API
  changes in this release. Companion docs / skill updates landed in the
  server package (POST confirm endpoint replacing stale PATCH status;
  Saga settlement & credits currency in `acn pay` subcommands).

## [0.7.1] - 2026-05-10

### Changed (BREAKING)
- `PaymentStats` field set now mirrors the server's
  ``PaymentTaskManager.get_payment_stats`` response — the previous
  ``total_received / total_sent / transaction_count / avg_amount``
  fields were never emitted by the server, so reads silently
  returned all-zero stats.
  - New shape: `total_tasks`, `as_buyer` (`PaymentRoleStats`),
    `as_seller` (`PaymentRoleStats`), `by_status: dict[str, int]`,
    `completed_transactions`.
  - New helper model `PaymentRoleStats` (`count`, `total_amount` as
    decimal string) is exported from `acn_client.models`.

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
