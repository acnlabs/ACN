# ACN Error Schema

**Status**: ✅ Pilot landed — Phase 2 review v2 P1 #11 (May 2026)
**Pilot scope**: communication routes (`/api/v1/communication/`*)
**Min SDK consumer**: ACN python client `0.5.0+` (synchronised with P1 #10 `X-ACN-SDK-Min-Version`)

This document is the canonical specification of the ACN error response schema. SDK authors and dashboard maintainers should treat it as the contract; route authors and reviewers should treat it as the migration guide.

The implementation lives in `[acn/core/errors.py](../../acn/core/errors.py)`; the central exception handler is registered in `[acn/api.py](../../acn/api.py)`.

---

## 1. Response schema

Every ACN-emitted error response — both 4xx (raised via `ACNHTTPError`) and 5xx (sanitised by the existing handler chain) — has the same flat top-level shape:

```json
{
  "error_code": "communication_rejected",
  "message": "Recipient's communication_policy denied this sender",
  "details": { "reason": "policy_closed", "reject_reason": "vacation" },
  "request_id": "01J5R2X..."
}
```


| Field        | Type                        | Stability                                                                                             |
| ------------ | --------------------------- | ----------------------------------------------------------------------------------------------------- |
| `error_code` | `string` (snake_case ASCII) | **Stable contract** — the only field SDK clients should branch on                                     |
| `message`    | `string`                    | Human-readable prose. **Not stable** — SDK clients MUST NOT string-match on it                        |
| `details`    | `object` (code-specific)    | Per-code structured context. Field semantics depend on `error_code`; undocumented fields are unstable |
| `request_id` | `string` (UUID v4)          | Per-request correlation id. Echoed in the `X-Request-ID` response header                              |


The same value is present in both the body's `request_id` and the `X-Request-ID` response header, so a client can quote either when reporting an issue.

### 5xx deprecation double-emit

During a 30-day deprecation window the sanitised 5xx body additionally carries the legacy `error` field, equal in value to `error_code`:

```json
{
  "error": "internal_server_error",
  "error_code": "internal_server_error",
  "message": "An internal error occurred. Please try again later.",
  "details": {},
  "request_id": "..."
}
```

Tracker: see "Phase 2 review v2 P1 #11 — Error schema migration sprint" in `[docs/BACKLOG.md](../BACKLOG.md)` for the field name, double-emit start date, removal target date, and owner.

---

## 2. ErrorCode catalog

The `ErrorCode` enum in `[acn/core/errors.py](../../acn/core/errors.py)` is a **forward catalog** — codes that aren't yet raised by any pilot route are included so future migrations can adopt them without reopening this PR. Every declared code has a `_DEFAULT_MESSAGES` entry; a CI test (`tests/core/test_error_schema.py::TestCatalogStructure`) enforces the structural invariant.

### Pilot use (communication routes)


| `error_code`             | HTTP status | Used by                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | `details` schema                                   |
| ------------------------ | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| `agent_not_found`        | 404         | communication routes (`/send` `/broadcast` `/broadcast-by-tag` `/history` `/history/{agent_id}/ack` `/internal/send`); allowlist routes (POST); registry routes (`GET /agents/{id}` `POST /heartbeat` `GET /me` `GET /agent-card.json` `GET /agent-registration.json` `GET /endpoint` `GET /policy` `PATCH /policy` `DELETE /agents/{id}` `POST /claim` `POST /transfer` `POST /release` `GET /wallets`, plus the catch-all proxy); subnets routes (`POST /subnets/{agent_id}/subnets/{subnet_id}` `DELETE /subnets/{agent_id}/subnets/{subnet_id}` `GET /subnets/{agent_id}/subnets`); payments routes (`POST /{agent_id}/payment-capability` `POST /{agent_id}/token-pricing`); follows routes (`POST /agents/{agent_id}/follows/{target_id}` — followee lookup miss) | `{ agent_id: string }`                             |
| `api_key_agent_mismatch` | 403         | communication routes (`/history` `/history/{agent_id}/ack`); allowlist routes (POST/DELETE/GET); registry routes (`POST /heartbeat`); subnets routes (`POST /subnets/{agent_id}/subnets/{subnet_id}` `DELETE /subnets/{agent_id}/subnets/{subnet_id}` `GET /subnets/{agent_id}/subnets`); payments routes (`POST /{agent_id}/payment-capability` `GET /tasks/agent/{agent_id}` `GET /stats/{agent_id}` `POST /{agent_id}/token-pricing`); follows routes (`POST /agents/{agent_id}/follows/{target_id}` `DELETE /agents/{agent_id}/follows/{target_id}` — path-mismatch gates)                                                                                                                                                                                                                                                                                               | `{ path_agent: string, key_agent: string }`        |
| `from_agent_mismatch`    | 403         | `/send` `/broadcast` `/broadcast-by-tag`; payments routes (`POST /tasks`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | `{ authenticated_as: string, from_agent: string }` |
| `communication_rejected` | 403         | `/send` `/internal/send` (defensive); registry catch-all proxy (`POST/PUT/PATCH /{agent_id}{/rest_path}`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | `{ reason: string, reject_reason: string | null }` |
| `unknown_strategy`       | 422         | `/broadcast`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | `{ strategy: string, expected: string[] }`         |
| `internal_server_error`  | 5xx         | All routes (sanitised by 5xx handler)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | `{}`                                               |


### Allowlist routes (sprint row #1)


| `error_code`                  | HTTP status | Used by                                     | `details` schema                      |
| ----------------------------- | ----------- | ------------------------------------------- | ------------------------------------- |
| `self_allowlist_forbidden`    | 400         | `/agents/{id}/allowlist/{target_id}` (POST) | `{ owner_id: string }`                |
| `allowlist_capacity_exceeded` | 429         | `/agents/{id}/allowlist/{target_id}` (POST) | `{ owner_id: string, max_size: int }` |


`agent_not_found` and `api_key_agent_mismatch` rows above are also raised here — see the *Used by* column.

### Registry routes (sprint rows #2a + #2b)


| `error_code`       | HTTP status | Used by                                               | `details` schema        |
| ------------------ | ----------- | ----------------------------------------------------- | ----------------------- |
| `subnet_not_found` | 400         | `POST /register` (DEV) and `POST /register-protected` | `{ subnet_id: string }` |


Pilot codes `agent_not_found` / `api_key_agent_mismatch` / `communication_rejected` are also raised by registry — see the *Used by* column on the pilot table. Sprint #2a's coverage of `agent_not_found` was extended in #2b to include `PATCH /agents/{id}/social-card-url` (a missed migration site — see footnote `[^2]`).

The remaining 11 4xx sites in `acn/routes/registry.py` migrated to the cross-module catalog in sprint #2b — see "Cross-module catalog (sprint row #2b)" below for the per-`ErrorCode` site enumeration.

### Tasks routes (sprint rows #4 + #4-followup)

| `error_code`     | HTTP status | Used by                                                                                                                                                                                                       | `details` schema       |
| ---------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------- |
| `task_not_found` | 404         | `GET /tasks/{id}`, `POST /tasks/{id}/accept`, `POST /tasks/{id}/invite`, `POST /tasks/{id}/submit`, `POST /tasks/{id}/review`, `POST /tasks/{id}/cancel`, `GET /tasks/{id}/participations`, `POST /tasks/{id}/participations/{pid}/cancel`, `POST /tasks/{id}/participations/{pid}/approve`, `POST /tasks/{id}/participations/{pid}/reject`, `GET /tasks/{id}/internal`, `POST /tasks/agent/{id}/accept`, `POST /tasks/agent/{id}/submit` | `{ task_id: string }`  |

Sprint #4-followup completed the migration of the 26 deferred 4xx sites by adopting the cross-module catalog from sprint #2b — see "Cross-module catalog (sprint row #2b)" below for the per-`ErrorCode` enumeration of tasks sites. The 1 remaining 5xx site (`create_task` catch-all) stays on raw `HTTPException` per the sanitisation contract.

### Subnets routes (sprint rows #3 + #3-followup)

`subnet_not_found` is raised at 7 sites in `acn/routes/subnets.py` — the public lookup endpoints (`GET /subnets/{id}`, `GET /subnets/{id}/agents`), the agent join/leave flows (`POST /subnets/{agent_id}/subnets/{subnet_id}`, `DELETE /subnets/{agent_id}/subnets/{subnet_id}`), the owner-protected delete's `except SubnetNotFoundException` branch (`DELETE /subnets/{id}`), and the two internal-token admin endpoints (`POST /subnets/{id}/members/{agent_id}`, `DELETE /subnets/{id}/members/{agent_id}`). Pilot codes `agent_not_found` and `api_key_agent_mismatch` are also raised here — see the *Used by* columns on the pilot table.

Sprint #3-followup completed the migration of the 6 deferred 4xx sites (auth / permission / validation) by adopting the cross-module catalog from sprint #2b — see "Cross-module catalog (sprint row #2b)" below for the per-`ErrorCode` enumeration of subnets sites.

**1 site remains deferred — pre-existing latent bug.** The `else: raise HTTPException(404)` short-circuit inside `delete_subnet`'s `try` body is silently rewritten to 500 by the surrounding catch-all `except Exception`. The migration intentionally does **not** convert this site because `ACNHTTPError` would have the same fate (it inherits from `Exception`, not `HTTPException`, by design). The fix requires the `except ACNHTTPError: raise` cross-module defence P3 ticket in [`docs/BACKLOG.md`](../BACKLOG.md) and is tracked there alongside the registry catch-all defence work.

> **Local catch-all defence**: sprint #3-followup added an `except ACNHTTPError: raise` line to the top of `list_subnets`'s catch-all block as a *local* application of the P3 defence ticket — without it, the new `AUTHENTICATION_REQUIRED` / `OWNERSHIP_MISMATCH` raises in the `try` body would have been swallowed by the trailing `except Exception` and rewritten to 500. The ticket still applies to the other 7 catch-all blocks in `subnets.py` and the 3 in `registry.py` that have no `ACNHTTPError`-emitting code in their `try` bodies (yet).

### Follows routes (sprint row #6)


| `error_code`             | HTTP status | Used by                                                | `details` schema                              |
| ------------------------ | ----------- | ------------------------------------------------------ | --------------------------------------------- |
| `self_follow_forbidden`  | 400         | `POST /agents/{agent_id}/follows/{target_id}`          | `{ follower_id: string }`                     |
| `follow_limit_exceeded`  | 429         | `POST /agents/{agent_id}/follows/{target_id}`          | `{ follower_id: string, max_follows: int }`   |


Pilot codes `agent_not_found` (×1 — followee lookup miss) and `api_key_agent_mismatch` (×2 — POST + DELETE path-mismatch gates) are also raised by follows — see the *Used by* column on the pilot table. follows has **no** 5xx catch-all sites — `follow_agent` / `unfollow_agent` only catch the three domain-specific exceptions (`SelfFollowError`, `AgentNotFoundException`, `FollowLimitExceededError`); there is no trailing `except Exception` to defend.

**Field-name choice — `follower_id` vs `owner_id`.** sprint #1 (allowlist) uses `owner_id` for the corresponding `self_allowlist_forbidden` and `allowlist_capacity_exceeded` codes because the allowlist is *owned* by the agent. follow has no ownership semantics — the operating entity is a *follower*, and both the service-layer exception names (`FollowLimitExceededError`, `SelfFollowError`) and the `acn-follow-proposal.md` response bodies use `follower`. The two sprints are semantically parallel (per-agent capacity ceiling + self-reference forbidden) but field-name divergent on purpose. SDK clients should not aliias the two — a `follow_limit_exceeded` retry handler reads `details.max_follows`, an `allowlist_capacity_exceeded` retry handler reads `details.max_size`. The `MAX_FOLLOWS` constant value (currently 10000) is published in `details.max_follows` so clients can pre-flight on retry without hardcoding it; it is a public contract knob and any change must coordinate with SDK release notes.

### Payments routes (sprint row #5)


| `error_code`                    | HTTP status | Used by                                                                                                                            | `details` schema             |
| ------------------------------- | ----------- | ---------------------------------------------------------------------------------------------------------------------------------- | ---------------------------- |
| `payment_capability_not_found`  | 404         | `GET /payments/{agent_id}/payment-capability`                                                                                      | `{ agent_id: string }`       |
| `payment_task_not_found`        | 404         | `GET /payments/tasks/{task_id}` (internal-token)                                                                                   | `{ task_id: string }`        |
| `token_pricing_not_configured`  | 404         | `GET /payments/{agent_id}/token-pricing`, `POST /payments/billing/estimate`, `POST /payments/billing/charge` (internal-token)      | `{ agent_id: string }`       |
| `billing_transaction_not_found` | 404         | `GET /payments/billing/transactions/{transaction_id}` (internal-token)                                                             | `{ transaction_id: string }` |


Pilot codes `agent_not_found`, `api_key_agent_mismatch`, and `from_agent_mismatch` are also raised by payments — see the *Used by* column on the pilot table. The 3 remaining 5xx sites (`set_payment_capability`, `create_payment_task`, `set_token_pricing` catch-alls) stay on raw `HTTPException(500)` per the sanitisation contract; all three carry the `except ACNHTTPError: raise` + `except HTTPException: raise` defence layers (P3 cross-module catch-all defence).

`INSUFFICIENT_BALANCE` stays in the reserved group of the `ErrorCode` catalog: `payments.py` only surfaces *resource-existence* failures (the four codes above), not balance failures. Balance failures live one layer deeper (wallet / billing subsystem) and may surface at a different boundary in a future sprint.

### Cross-module catalog (sprint row #2b)

Six `ErrorCode` members designed to be **shared by `registry`, `subnets`, and `tasks`** so an SDK consumer can write one set of fallback handlers regardless of which module emitted the error. The cross-module set is the deliverable that unblocked rows #3-followup and #4-followup; see [`docs/BACKLOG.md`](../BACKLOG.md) for the per-row status.

| `error_code`              | HTTP status | Raised by                                                                                                                                                                                                                                                                                                                                                                                                                            | `details` schema                                                       |
| ------------------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------- |
| `authentication_required` | 401         | **registry** — `GET /me` (×2: invalid Authorization header format / unrecognised API key); **subnets** — `GET /subnets?owner=…` (owner-filter requires auth), `GET /subnets/{id}/agents` (private subnet requires auth); **tasks** — `require_task_write_auth` agent path (invalid `acn_xxx` API key)                                                                                                                                 | `{ reason: enum, subnet_id?: string }`                                 |
| `internal_token_invalid`  | 401         | **registry** — `POST /agents/join/internal` (X-Internal-Token missing or mismatched)                                                                                                                                                                                                                                                                                                                                                 | `{}`                                                                   |
| `missing_permission`      | 403         | **registry** — `POST /agents/dev/register` (dev-mode disabled in this environment); **tasks** — `require_task_write_auth` JWT path (caller lacks `acn:write` scope)                                                                                                                                                                                                                                                                  | `{ reason: enum, required_permission?: string }`                       |
| `ownership_mismatch`      | 403         | **registry** — `POST /register-protected` (owner-token mismatch); `DELETE /agents/{id}` `POST /claim` `POST /transfer` `POST /release` (`PermissionError` re-raises); **subnets** — `GET /subnets?owner=…` (cross-tenant non-admin), `DELETE /subnets/{id}` (`PermissionError` re-raise); **tasks** — every write endpoint that goes through `task_service` raising `PermissionError` (×10, `replace_all=true` cohort)                | `{ agent_id?: string, subnet_id?: string, task_id?: string, reason?: string } \| { requested_owner: string, token_owner: string }` |
| `not_subnet_member`       | 403         | **subnets** — `GET /subnets/{id}/agents` against a private subnet by a non-owner non-admin caller; **tasks** — `GET /tasks/{id}` against a task whose `subnet_id` is set, by an anonymous (no-auth) or non-member caller                                                                                                                                                                                                              | `{ subnet_id: string, agent_id?: string, task_id?: string, reason?: enum }` |
| `invalid_request`         | 400         | **registry** — `DELETE /api/v1/agents` (bulk-delete filter required), `POST /agents/{id}/claim` (`ValueError` from claim flow); **subnets** — `POST /subnets` (`ValueError` from create flow); **tasks** — `list_tasks` invalid status enum, `match_tasks_for_agent` empty tag list, every write endpoint raising `ValueError` (×10, `replace_all=true` cohort)                                                                       | `{ reason: string, field?: string, value?: any, allowed?: list, task_id?: string, agent_id?: string }` |

`details.reason` is a stable per-code enum where the value is a fixed identifier, OR a free-form `str(...)` of the underlying domain exception when the code wraps `ValueError` / `PermissionError`. Reason values currently emitted, by `error_code`:

* `authentication_required` — `"invalid_authorization_header_format"` (registry), `"invalid_api_key"` (registry), `"owner_filter_requires_auth"` (subnets), `"private_subnet"` (subnets), `"invalid_agent_api_key"` (tasks)
* `missing_permission` — `"dev_mode_disabled"` (registry); tasks emits `details.required_permission` (`"acn:write"`) instead of a reason
* `not_subnet_member` — `"anonymous_caller"` (tasks `get_task` no-auth branch), `"not_member"` (tasks `get_task` non-member branch); subnets uses no reason (the field set already disambiguates)
* `invalid_request` — `"bulk_delete_filter_required"` (registry), `"tag_list_empty"` (tasks `match_tasks_for_agent`), `field="status"` enum-value rejection (tasks `list_tasks`); free-form `str(ValueError)` (registry claim path, subnets create path, tasks 10× `replace_all=true` cohort)
* `ownership_mismatch` — free-form `str(PermissionError)` from the underlying service (registry / subnets / tasks); the registry owner-token mismatch path uses no reason (the field set itself — `requested_owner` + `token_owner` — already disambiguates)

Default `_DEFAULT_MESSAGES` for these codes are short, generic, and SDK-friendly (e.g. `"The authenticated caller does not own the requested resource."` for `OWNERSHIP_MISMATCH`). Routes pass per-call diagnostic prose via the explicit `message=` kwarg when the default isn't precise enough — the bulk-delete safety guard is the canonical example.

### Reserved (declared, not yet raised)

`wallet_rate_limit_exceeded` / `insufficient_balance` / `resource_conflict`

Reserved codes will be picked up by the migration sprint as each route is converted (see Section 7).

### `details` field semantics

The catalog tables above show field *names and types*; this section pins the precise *semantics* the SDK contract guarantees, since several details fields use the same name across codes.


| Field path                                     | Semantics                                                                                                                                                                                                                                                                              |
| ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `agent_not_found.details.agent_id`             | The ID of the **missing** agent — i.e. the agent the request was trying to reference. Differs by route: it is `target_agent` for `/send`, `path agent_id` for `/history`, `target_id` for allowlist POST/DELETE.                                                                       |
| `from_agent_mismatch.details.authenticated_as` | The agent ID that the API key authenticated as (server-trusted).                                                                                                                                                                                                                       |
| `from_agent_mismatch.details.from_agent`       | The agent ID that the request body claimed (caller-supplied, untrusted).                                                                                                                                                                                                               |
| `api_key_agent_mismatch.details.path_agent`    | The agent ID extracted from the URL path.                                                                                                                                                                                                                                              |
| `api_key_agent_mismatch.details.key_agent`     | The agent ID associated with the API key the caller presented.                                                                                                                                                                                                                         |
| `self_allowlist_forbidden.details.owner_id`    | The owner / target agent ID — they are equal here by definition (this is what *makes* the call self-referential).                                                                                                                                                                      |
| `allowlist_capacity_exceeded.details.owner_id` | The owner whose allowlist hit the cap. Always equal to the path agent_id.                                                                                                                                                                                                              |
| `allowlist_capacity_exceeded.details.max_size` | The configured maximum entries per allowlist (currently the system-wide constant `MAX_ALLOWLIST_SIZE = 500`). If ACN later moves to per-owner caps, the response will carry the *effective* cap for that owner — SDK clients should always read this field rather than hardcoding 500. |
| `unknown_strategy.details.strategy`            | The unrecognised value the caller supplied (case-preserved for diagnostic clarity, even though strategy parsing is case-insensitive at the boundary).                                                                                                                                  |
| `unknown_strategy.details.expected`            | The list of accepted strategy names. Stable order, lowercase.                                                                                                                                                                                                                          |
| `communication_rejected.details.reason`        | A short server-classified reason code (e.g. `policy_closed`, `not_in_allowlist`).                                                                                                                                                                                                      |
| `communication_rejected.details.reject_reason` | Recipient-supplied free-form prose, may be `null`. **Treat as untrusted user content** — do not log verbatim or render as HTML.                                                                                                                                                        |


---

## 3. Out of scope

### `RequestValidationError` (FastAPI automatic 422)

FastAPI emits its own structured 422 for pydantic body / query / path validation failures:

```json
{ "detail": [{"loc": ["body", "from_agent"], "msg": "...", "type": "..."}] }
```

This shape is **not covered by this schema**. Overriding `RequestValidationError` would replace FastAPI's pydantic-aware error reporting with a less informative single message — the trade-off doesn't pay off inside Phase 2 #11's scope.

Consumers in pilot routes that pose a body schema (e.g. `AckInboxRequest`) will continue to see FastAPI's default 422 shape on validation failures. SDK clients should keep their existing `RequestValidationError` parser.

A P3 BACKLOG ticket is open for "align RequestValidationError with ACN flat schema" — estimated 1 PR, not blocking #11.

### Slowapi rate-limit 429

`@limiter.limit("...")` decorators on routes (e.g. `60/minute` on the allowlist routes) emit a different shape via `slowapi._rate_limit_exceeded_handler`:

```json
{ "error": "Rate limit exceeded: 60 per 1 minute" }
```

This means HTTP 429 has **two distinct shapes** depending on which handler emitted it:


| Trigger                                            | Shape                                                 | Handler                        |
| -------------------------------------------------- | ----------------------------------------------------- | ------------------------------ |
| `@limiter.limit(...)` quota exhausted              | `{ "error": "Rate limit exceeded: ..." }` (slowapi)   | `_rate_limit_exceeded_handler` |
| Migrated route raising `ACNHTTPError(..., 429, …)` | `{ error_code, message, details, request_id }` (flat) | `_acn_http_error_handler`      |


`allowlist_capacity_exceeded` (sprint row #1) is the first migrated 429 so the contrast bites here first; future sprint rows that emit 429 (e.g. follow limits) will be on the same flat side.

SDK clients **must not** branch on `status_code == 429` alone. The parsing template in section 4 (`if "error_code" in body`) already routes both shapes correctly; no SDK code change is needed beyond keeping that check in place.

Realigning slowapi to the flat schema is reserved as `WALLET_RATE_LIMIT_EXCEEDED` in the catalog — see the BACKLOG roadmap; it is intentionally not bundled with the per-module migration sprint because slowapi handler replacement affects every limited route at once.

### Non-pilot routes

The route modules `onchain`, `dependencies`, `manifest`, `analytics`, and `websocket` still raise vanilla `HTTPException` and are caught by the existing `_http_exception_handler` 4xx pass-through, which emits the legacy `{"detail": "..."}` / `{"detail": {...}}` shape. The `registry`, `subnets`, `tasks`, `payments`, and `follows` modules are fully migrated as of sprints #2b, #3-followup, #4-followup, #5, and #6 respectively. See the coexistence matrix below. These routes are NOT broken, they just speak the old contract until the migration sprint reaches them.

---

## 4. Transitional coexistence matrix

During the migration sprint, ACN-emitted 4xx responses carry **two distinct shapes** depending on which route emitted them. This is the single most important fact for SDK upgrade:


| Route group                                                                                                                    | Exception class                          | 4xx body shape                                                                         | Status           |
| ------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------- | -------------------------------------------------------------------------------------- | ---------------- |
| Pilot — `/communication/send`, `/broadcast`, `/broadcast-by-tag`, `/history`, `/history/{agent_id}/ack`, `/internal/send` [^1] | `ACNHTTPError`                           | `{ error_code, message, details, request_id }` (flat)                                  | ✅ Migrated       |
| `/api/v1/agents/{id}/allowlist/...` (POST/DELETE/GET) [^1]                                                                     | `ACNHTTPError`                           | `{ error_code, message, details, request_id }` (flat)                                  | ✅ Migrated       |
| `/api/v1/agents/`* (registry) [^1] [^2]                                                                                        | `ACNHTTPError` (4xx) + `HTTPException` (5xx) | flat                                                                              | ✅ Aligned (#2a + #2b) |
| `/api/v1/subnets/*` [^1] [^3]                                                                                                  | `ACNHTTPError` (4xx) + `HTTPException` (5xx, 1× latent bug)             | flat (except 1 latent-bug site silently rewritten to 500)                              | ✅ Aligned (#3 + #3-followup, modulo latent bug) |
| `/api/v1/tasks/*` [^1] [^4]                                                                                                    | `ACNHTTPError` (4xx) + `HTTPException` (5xx)             | flat                                                                                   | ✅ Aligned (#4 + #4-followup) |
| `/api/v1/agents/{id}/follows/*` (POST/DELETE) [^1] [^6]                                                                        | `ACNHTTPError`                           | flat                                                                                   | ✅ Aligned (#6)   |
| `/api/v1/payments/*` [^1] [^5]                                                                                                 | `ACNHTTPError` (4xx) + `HTTPException` (5xx) | flat                                                                                   | ✅ Aligned (#5)   |
| `/api/v1/onchain/*`                                                                                                            | `HTTPException`                          | `{ "detail": "..." }`                                                                  | ⏳ Pending        |
| `/api/v1/communication/manifest/*`                                                                                             | `HTTPException`                          | `{ "detail": "..." }`                                                                  | ⏳ Pending        |
| `/api/v1/analytics/*`                                                                                                          | `HTTPException`                          | `{ "detail": "..." }`                                                                  | ⏳ Pending        |
| `/ws/*` (websocket)                                                                                                            | `HTTPException`                          | `{ "detail": "..." }`                                                                  | ⏳ Pending        |
| Auth dependency rejects (any route)                                                                                            | `HTTPException`                          | `{ "detail": "..." }`                                                                  | ⏳ Pending        |
| All routes — 5xx                                                                                                               | `HTTPException` (sanitised)              | `{ error, error_code, message, details, request_id }` (flat, with deprecation `error`) | ✅ Aligned        |


Each migration PR in the sprint flips a row from ⏳ → ✅. SDK consumers can depend on this matrix as the source of truth for which response shape a given endpoint emits.

[^2]: **Registry full migration (sprint #2a + #2b).** All 30 4xx raise sites in `acn/routes/registry.py` are migrated:

    *Sprint #2a (19 sites)* — direct mapping to existing catalog codes: 15× `AGENT_NOT_FOUND` + 1× `API_KEY_AGENT_MISMATCH` + 2× `SUBNET_NOT_FOUND` + 1× `COMMUNICATION_REJECTED` (which also flattened the legacy nested `{"detail": {"detail": "..."}}` proxy shape).

    *Sprint #2b (11 sites)* — picked up by the new cross-module catalog: 1× `MISSING_PERMISSION` (dev-mode disabled), 1× `OWNERSHIP_MISMATCH` for the `register-protected` owner-token mismatch + 3× more for the `unregister_agent` / `transfer_agent` / `release_agent` `PermissionError` re-raises, 2× `AUTHENTICATION_REQUIRED` for `GET /me`'s malformed-header / unrecognised-API-key paths, 1× `INTERNAL_TOKEN_INVALID` for the internal join endpoint, 2× `INVALID_REQUEST` (bulk-delete safety guard + `claim_agent`'s `ValueError`), and 1× `AGENT_NOT_FOUND` to fix a missed migration site at `update_agent_social_card_url` (a `PATCH` endpoint added to registry *after* sprint #2a's 19-site enumeration was frozen — the sprint-#2a footnote's "29 4xx total" count was a snapshot, not a stable invariant).

    Sprint #2c was originally scoped to cover the `ValueError` claim path with a dedicated `INVALID_AGENT_CLAIM` code; the cross-module RFC consolidated that into `INVALID_REQUEST` with `details.reason="invalid_agent_claim"` so #2c is no longer needed as a separate sprint row.

    The 7 5xx sites in registry (502 / 503 / catch-all 500) stay on `HTTPException` by design — `ACNHTTPError` rejects 5xx at construction time so the existing sanitised-5xx handler chain stays in charge. SDK clients hitting registry endpoints now see the flat schema for *all* 4xx; the §4 SDK parsing template's `if "error_code" in body` branch is no longer load-bearing for registry but stays correct (shared across modules at different migration stages).

[^3]: **Subnets full migration modulo 1 latent-bug site (sprint #3 + #3-followup).** All 19 of the 20 4xx raise sites in `acn/routes/subnets.py` are migrated:

    *Sprint #3 (13 sites)* — direct mapping to existing catalog codes: 7× `SUBNET_NOT_FOUND` + 3× `AGENT_NOT_FOUND` + 3× `API_KEY_AGENT_MISMATCH`.

    *Sprint #3-followup (6 sites)* — picked up by the cross-module catalog: 1× `INVALID_REQUEST` (`create_subnet` `ValueError`, `details.reason=str(e)`), 2× `AUTHENTICATION_REQUIRED` (owner-filter on `GET /subnets` + private-subnet view on `GET /subnets/{id}/agents`), 1× `OWNERSHIP_MISMATCH` (cross-tenant `GET /subnets?owner=…`, `details.requested_owner` / `token_owner`), 1× `OWNERSHIP_MISMATCH` (`delete_subnet` `except PermissionError`, `details.subnet_id` + `reason=str(e)`), 1× `NOT_SUBNET_MEMBER` (private-subnet member list, `details.subnet_id` + `agent_id`).

    *Local catch-all defence layer added in #3-followup* — `list_subnets` got an `except ACNHTTPError: raise` line at the top of its catch-all block. Without it, the new `AUTHENTICATION_REQUIRED` / `OWNERSHIP_MISMATCH` raises from inside its `try` body would have been swallowed by the trailing `except Exception` and silently rewritten to 500 (because `ACNHTTPError` is `Exception`-typed by design — see `acn.core.errors` docstring on why it does NOT subclass `HTTPException`). The local fix is a precursor to the cross-module catch-all defence P3 ticket in `[docs/BACKLOG.md](../BACKLOG.md)`.

    **1 site remains deferred — pre-existing latent bug.** The `else: raise HTTPException(404)` short-circuit inside `delete_subnet`'s `try` body is silently rewritten to 500 by the surrounding catch-all `except Exception` (today and after the migration would have been, since `ACNHTTPError` is also `Exception`-typed by design). Fixing it requires the cross-module `except ACNHTTPError: raise` defence ticket and is tracked there alongside the registry catch-all defence work.

    The 8 5xx sites in subnets stay on `HTTPException` by design (sanitised-5xx handler chain). Sprint row #3 flips fully ✅ when the catch-all defence ticket lands and the latent-bug site can finally be migrated.

[^4]: **Tasks full migration (sprint #4 + #4-followup).** All 39 4xx raise sites in `acn/routes/tasks.py` are migrated:

    *Sprint #4 (13 sites)* — every `except TaskNotFoundException: raise … from None` site (`GET /tasks/{id}`, `POST /tasks/{id}/{accept,invite,submit,review,cancel}`, `GET /tasks/{id}/participations`, the three `POST /tasks/{id}/participations/{pid}/{cancel,approve,reject}`, `GET /tasks/{id}/internal`, `POST /tasks/agent/{id}/{accept,submit}`). All 13 use the uniform shape `ACNHTTPError(TASK_NOT_FOUND, 404, {"task_id": …}) from None`.

    *Sprint #4-followup (26 sites)* — picked up by the cross-module catalog from #2b:

      * **10× `OWNERSHIP_MISMATCH`** (403) — every `except PermissionError` site, `replace_all=true` migration to byte-identical `ACNHTTPError(OWNERSHIP_MISMATCH, 403, {"task_id": task_id, "reason": str(e)})`. Sites: every write endpoint that goes through `task_service` (`accept_task`, `invite_task`, `submit_task`, `review_task`, `cancel_task`, the 3× participation moderation endpoints `cancel_participation`/`approve_applicant`/`reject_applicant`, plus the 2 dedicated agent-API-key endpoints `agent_accept_task`/`agent_submit_task`).
      * **10× `INVALID_REQUEST`** (400) — paired 1:1 with the PermissionError sites (same handlers, different `except` branch); same `replace_all=true` migration to `ACNHTTPError(INVALID_REQUEST, 400, {"task_id": task_id, "reason": str(e)})`.
      * **6 endpoint-specific 4xx**:
        * 1× `AUTHENTICATION_REQUIRED` (401) — `require_task_write_auth` agent path when `acn_xxx` resolution returns `None` (`details.reason="invalid_agent_api_key"`).
        * 1× `MISSING_PERMISSION` (403) — `require_task_write_auth` JWT path when caller lacks `acn:write` scope (`details.required_permission="acn:write"`).
        * 1× `INVALID_REQUEST` (400) — `list_tasks` invalid status enum (`details.field="status"`, `value`, `allowed=[…]`).
        * 1× `INVALID_REQUEST` (400) — `match_tasks_for_agent` empty tag list (`details.field="tags"`, `reason="tag_list_empty"`).
        * 2× `NOT_SUBNET_MEMBER` (403) — `get_task`'s private-subnet gate; `details.reason="anonymous_caller"` for the no-auth branch and `"not_member"` for the resolved-but-non-member branch. Both keep status 403 (rather than 401 for the anonymous case) so an attacker cannot probe for private tasks via auth-gate behaviour — `details.reason` gives the SDK enough context to disambiguate without leaking task existence to anonymous callers.

    The 1 5xx site (`create_task` catch-all) stays on `HTTPException` by design (sanitised-5xx handler chain).

[^5]: **Payments full migration (sprint #5).** All 13 4xx raise sites in `acn/routes/payments.py` are migrated:

    * **4× `API_KEY_AGENT_MISMATCH`** (403) — every site that gates on path `agent_id` vs auth-key `agent_id` (`set_payment_capability`, `get_agent_payment_tasks`, `get_agent_payment_stats`, `set_token_pricing`). Uniform `details={"path_agent": …, "key_agent": …}` matching the §2 schema established by sprint #1-#3.
    * **2× `AGENT_NOT_FOUND`** (404) — distinct code paths: `set_payment_capability` catches `AgentNotFoundException` from `agent_service.get_agent`, `set_token_pricing` checks `registry.get_agent` returning `None`. Both emit identical canonical 404 with `details={"agent_id": …}`.
    * **1× `FROM_AGENT_MISMATCH`** (403) — `create_payment_task` body-field mismatch (auth-key vs `request.from_agent`). Distinct error code from `API_KEY_AGENT_MISMATCH` so SDK clients can branch on the source of the mismatch. `details={"authenticated_as": …, "from_agent": …}` matching §2.
    * **1× `PAYMENT_CAPABILITY_NOT_FOUND`** (404) — `get_payment_capability` lookup miss; `details={"agent_id": …}`.
    * **1× `PAYMENT_TASK_NOT_FOUND`** (404) — `get_payment_task` internal lookup miss; `details={"task_id": …}`.
    * **3× `TOKEN_PRICING_NOT_CONFIGURED`** (404) — `get_token_pricing` (path agent_id), `estimate_cost` (body field, public limited endpoint), `bill_usage` (body field, internal-token billing endpoint). All emit `details={"agent_id": …}` regardless of source.
    * **1× `BILLING_TRANSACTION_NOT_FOUND`** (404) — `get_billing_transaction` internal lookup miss; `details={"transaction_id": …}`.

    The 3 5xx sites (`set_payment_capability`, `create_payment_task`, `set_token_pricing` catch-alls) stay on `HTTPException` by design (sanitised-5xx handler chain). All three carry the `except ACNHTTPError: raise` + `except HTTPException: raise` defence layers so caller-actionable 4xx raised inside the try body propagates instead of being silently rewritten as 500.

    `INSUFFICIENT_BALANCE` stays in the reserved group of the catalog: `payments.py` only surfaces *resource-existence* failures, not balance failures (those live in the wallet/billing subsystem and may surface at a different boundary).

[^6]: **Follows full migration (sprint #6).** All 5 4xx raise sites in `acn/routes/follows.py` are migrated:

    * **2× `API_KEY_AGENT_MISMATCH`** (403) — `follow_agent` POST and `unfollow_agent` DELETE both gate on `caller["agent_id"] != agent_id`. Uniform `details={"path_agent": …, "key_agent": …}` matching the §2 schema established by sprint #1-#5.
    * **1× `SELF_FOLLOW_FORBIDDEN`** (400) — `follow_agent` catches `SelfFollowError` from the service layer. `details={"follower_id": …}`. Field name diverges from sprint #1's `self_allowlist_forbidden` (`owner_id`) on purpose: follow has no ownership semantics and the service-layer exception class (`SelfFollowError`) plus the proposal RFC both use `follower`.
    * **1× `AGENT_NOT_FOUND`** (404) — `follow_agent` catches `AgentNotFoundException` (followee lookup miss). `details={"agent_id": <target_id>}` — the missing entity is the *target* (followee), matching sprint #1 allowlist semantics where the same code points at the target.
    * **1× `FOLLOW_LIMIT_EXCEEDED`** (429) — `follow_agent` catches `FollowLimitExceededError` when the follower has reached `MAX_FOLLOWS` (currently 10000). `details={"follower_id": …, "max_follows": MAX_FOLLOWS}`. The 429 status is per the `acn-follow-proposal.md` spec ("超出返回 429"); `details.max_follows` is the documented contract knob — clients can pre-flight on retry without hardcoding the constant. Field names diverge from sprint #1's `allowlist_capacity_exceeded` (`owner_id` / `max_size`) on purpose, same rationale as `self_follow_forbidden`.

    `follows.py` has **0 5xx catch-all sites** — `follow_agent` and `unfollow_agent` only catch the three domain-specific exceptions (`SelfFollowError` / `AgentNotFoundException` / `FollowLimitExceededError`); there is no trailing `except Exception:` to defend, so the cross-module catch-all defence (P3) does not apply to this module.

    5 contract tests in `tests/routes/test_follows_error_schema.py` pin every raise site (1:1 coverage), asserting flat shape + exact `error_code` + exact `details` dict + `X-Request-ID` echo for each.

[^1]: **Migrated routes are converted at the route handler body level only.** 4xx errors raised by the *route handler function body* go through `ACNHTTPError` and emit the flat schema. 4xx errors raised by *shared dependencies* invoked before the handler body — `OwnerOrInternalDep`, `InternalTokenDep`, `AgentApiKeyDep`, the `A2AFromAgentValidationMiddleware` ASGI middleware — still raise `HTTPException` and emit the legacy `{"detail": "..."}` shape. They flip to flat schema when the `dependencies` module is migrated (sprint row #10 in `[docs/BACKLOG.md](../BACKLOG.md)`). SDK clients hitting a migrated endpoint must therefore keep both parsers in scope until row #10 lands; the parsing template below already does this via the `if "error_code" in body` branch.

### SDK parsing template

A defensively-written SDK should detect the new shape by presence of `error_code`:

```python
def parse_acn_error(response_body: dict) -> AcnError:
    if "error_code" in response_body:
        # Pilot routes (post-#11) and all 5xx — flat schema.
        return AcnError(
            code=response_body["error_code"],
            message=response_body["message"],
            details=response_body.get("details", {}),
            request_id=response_body.get("request_id"),
        )
    # Legacy 4xx (non-pilot routes) — nested or string detail.
    detail = response_body.get("detail", "")
    if isinstance(detail, dict) and "detail" in detail:
        # Pre-#11 communication policy_rejected nested shape.
        return AcnError(code=detail["detail"], message="", details=detail, request_id=None)
    return AcnError(code="legacy_unknown", message=str(detail), details={}, request_id=None)
```

The `if "error_code" in body` branch is the migration anchor — it works correctly today (pilot + 5xx) and continues to work as more routes flip to ✅. SDK code does not need updating per migration PR; the `else` branch shrinks naturally as the sprint progresses and can be removed once the matrix is all ✅.

---

## 5. SDK consumer guidelines

### Stable contract surface

Branch only on these:

- `error_code` (string, ASCII snake_case) — the canonical machine-readable selector
- HTTP status code — semantic class (4xx caller-actionable, 5xx server fault)
- `details.<documented-key>` per `error_code` — see Section 2 catalog

### Unstable surface — do not branch on these

- `message` — wording may change at any time without notice. It exists for human display in logs / error toasts / dev tooling.
- Undocumented `details` keys — only the per-code keys listed in Section 2 are part of the contract. ACN may add more keys in any direction.
- The presence of `request_id` is stable; the value format (currently UUID v4) is not.

### Logging and reporting

- Log `request_id` alongside any client-side error to enable cross-channel correlation when filing a support ticket.
- Do **not** log `details` payloads verbatim if they may contain user input (`reject_reason` is recipient-supplied prose; treat it like any other user content).

---

## 6. Route author migration guide

This section is for ACN backend contributors migrating a non-pilot route into the flat schema.

### Step 1 — Decide whether to add a new code or reuse one


| Situation                                                              | Action                                                                           |
| ---------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Existing code matches the situation (e.g. another "X not found" route) | Reuse — keeps the catalog small and predictable for SDK code-gen                 |
| Situation is genuinely new                                             | Add a new `ErrorCode` member + `_DEFAULT_MESSAGES` entry; document in this file  |
| Same code with different per-route details                             | Reuse the code; vary `details` keys; document each route's `details` schema here |


Naming convention: `<resource>_<verb_or_state>` in snake_case. `agent_not_found` ✅; `notFoundAgent` ✗; `agent-not-found` ✗.

### Step 2 — Replace `raise HTTPException(...)` with `raise ACNHTTPError(...)`

Anti-pattern (legacy):

```python
raise HTTPException(status_code=403, detail="API key does not match agent_id")
```

ACN-canonical:

```python
raise ACNHTTPError(
    ErrorCode.API_KEY_AGENT_MISMATCH,
    403,
    details={"path_agent": agent_id, "key_agent": agent_info["agent_id"]},
)
```

Notes:

- **Do not** pass `message=` unless you genuinely need a route-specific override; the default `_DEFAULT_MESSAGES[code]` is the right answer for almost every site.
- `details` keys must match the catalog entry in Section 2; if you add a key, update Section 2 in the same PR.
- 5xx sites must keep using `raise HTTPException(status_code=500, detail=str(e))` — `ACNHTTPError` rejects 5xx at construction time to keep the existing 5xx sanitisation chain in charge of those responses.

### Step 3 — Update tests

- Replace `assert r.json()["detail"] == "..."` with `assert r.json()["error_code"] == "..."` (flat top-level).
- Add a shape-invariant assertion: `assert {"error_code", "message", "details", "request_id"} <= r.json().keys()`.
- 4xx assertions must NOT inspect the `detail` field — its absence is part of the contract for migrated routes.

### Step 4 — Flip the row in this document

Update Section 4's coexistence matrix from ⏳ to ✅ for the migrated route group, in the same PR.

---

## 7. Migration sprint roadmap

The pilot migration covered communication routes (~14 4xx sites). The sprint to cover the remaining 11 modules is tracked in `[docs/BACKLOG.md](../BACKLOG.md)` under "Phase 2 review v2 P1 #11 — Error schema migration sprint".

Suggested ordering (cheapest / most impactful first):

1. `**allowlist`** — already has domain exceptions (`AllowlistCapacityExceededError`, `SelfAllowlistError`); 1:1 mapping to reserved catalog codes
2. `**registry**` — frequently consumed by SDK; `agent_not_found` / `agent_already_exists` mappings are obvious
3. `**subnets**` — `subnet_not_found` is already reserved in the catalog
4. `**tasks**` — `task_not_found` reserved
5. ~~`**payments**` — `insufficient_balance` reserved~~ ✅ #5 landed. Actual sprint #5 surfaced **resource-existence** failures (capability / task / pricing / transaction not found), not balance failures — `INSUFFICIENT_BALANCE` stays reserved until the wallet/billing subsystem genuinely raises it. New codes: `PAYMENT_CAPABILITY_NOT_FOUND`, `PAYMENT_TASK_NOT_FOUND`, `TOKEN_PRICING_NOT_CONFIGURED`, `BILLING_TRANSACTION_NOT_FOUND` (4); reused: `AGENT_NOT_FOUND`, `API_KEY_AGENT_MISMATCH`, `FROM_AGENT_MISMATCH` (3).
6. ~~`**follows**` — small surface, similar shape to `allowlist`~~ ✅ #6 landed. New codes: `FOLLOW_LIMIT_EXCEEDED`, `SELF_FOLLOW_FORBIDDEN` (2); reused: `AGENT_NOT_FOUND`, `API_KEY_AGENT_MISMATCH` (2). 5 4xx sites total, **0 5xx catch-all sites** (the only `try/except` blocks in `follow_agent` / `unfollow_agent` catch domain-specific exceptions, no trailing `except Exception`). Field-name divergence vs sprint #1 (`follower_id` / `max_follows` instead of `owner_id` / `max_size`) was deliberate — see footnote `[^6]`.
7. `**onchain**` — needs new codes for ERC-8004 specific failures
8. `**manifest**` — small surface
9. `**analytics**` — small surface, mostly 4xx pass-through today
10. `**dependencies**` — auth-shared module; high care due to security relevance
11. `**websocket**` — last (different protocol surface; may need separate schema treatment)

After all rows in Section 4 flip to ✅, the SDK fallback `else` branch in Section 4's parsing template can be removed and the 30-day 5xx `error` field deprecation can land — at which point the schema is fully unified and ACN can declare the migration complete.

---

## 8. OpenAPI schema visibility

The `ACNErrorResponse` Pydantic model in `[acn/core/errors.py](../../acn/core/errors.py)` defines the *contract*, but it is **not** automatically attached to pilot routes' OpenAPI documentation. `acn/api.py` registers a generic `app.exception_handler(ACNHTTPError)`; FastAPI cannot statically infer which routes raise `ACNHTTPError` at which status codes, so `/openapi.json` does not advertise the flat schema for pilot endpoints today.

Practical impact for SDK type-gen consumers (e.g. `openapi-python-client`, `openapi-typescript`):

- The generated type for pilot 4xx responses falls back to `HTTPValidationError` / generic `dict`, not `ACNErrorResponse`.
- `error_code` / `message` / `details` / `request_id` field types must be modelled by hand in SDK code today.

To advertise the schema in OpenAPI, a route author can add an explicit `responses=` block:

```python
from acn.core.errors import ACNErrorResponse

@router.post(
    "/send",
    responses={
        403: {"model": ACNErrorResponse, "description": "policy / auth rejection"},
        404: {"model": ACNErrorResponse, "description": "agent not found"},
    },
)
```

Doing this for every pilot route is tracked as a separate ticket in `[docs/BACKLOG.md](../BACKLOG.md)` ("OpenAPI schema visibility for ACN flat error response") and is intentionally **not** part of #11's pilot scope — the contract test (`tests/core/test_error_schema.py`) is sufficient to enforce the runtime shape today, and OpenAPI advertisement is best done atomically with each route migration so the doc and the migration matrix stay in lockstep.

---

## 9. Cross-references

- `[acn/core/errors.py](../../acn/core/errors.py)` — `ErrorCode` / `ACNHTTPError` / `ACNErrorResponse` source
- `[acn/api.py](../../acn/api.py)` — `_acn_http_error_handler` central handler + sanitised 5xx handlers
- `[tests/core/test_error_schema.py](../../tests/core/test_error_schema.py)` — schema invariants
- `[tests/test_error_sanitisation.py](../../tests/test_error_sanitisation.py)` — 5xx sanitisation contract
- `[docs/BACKLOG.md](../BACKLOG.md)` — migration sprint tracker, 5xx field deprecation ticket
- `[docs/features/acn-communication-economic-model.md](acn-communication-economic-model.md)` — Phase 2 P1 #10 (`X-ACN-SDK-Min-Version`) and #11 decision history

