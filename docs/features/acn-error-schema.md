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
| `agent_not_found`        | 404         | communication routes (`/send` `/broadcast` `/broadcast-by-tag` `/history` `/history/{agent_id}/ack` `/internal/send`); allowlist routes (POST); registry routes (`GET /agents/{id}` `POST /heartbeat` `GET /me` `GET /agent-card.json` `GET /agent-registration.json` `GET /endpoint` `GET /policy` `PATCH /policy` `DELETE /agents/{id}` `POST /claim` `POST /transfer` `POST /release` `GET /wallets`, plus the catch-all proxy); subnets routes (`POST /subnets/{agent_id}/subnets/{subnet_id}` `DELETE /subnets/{agent_id}/subnets/{subnet_id}` `GET /subnets/{agent_id}/subnets`) | `{ agent_id: string }`                             |
| `api_key_agent_mismatch` | 403         | communication routes (`/history` `/history/{agent_id}/ack`); allowlist routes (POST/DELETE/GET); registry routes (`POST /heartbeat`); subnets routes (`POST /subnets/{agent_id}/subnets/{subnet_id}` `DELETE /subnets/{agent_id}/subnets/{subnet_id}` `GET /subnets/{agent_id}/subnets`)                                                                                                                                                                                                                                                                                               | `{ path_agent: string, key_agent: string }`        |
| `from_agent_mismatch`    | 403         | `/send` `/broadcast` `/broadcast-by-tag`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | `{ authenticated_as: string, from_agent: string }` |
| `communication_rejected` | 403         | `/send` `/internal/send` (defensive); registry catch-all proxy (`POST/PUT/PATCH /{agent_id}{/rest_path}`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | `{ reason: string, reject_reason: string | null }` |
| `unknown_strategy`       | 422         | `/broadcast`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | `{ strategy: string, expected: string[] }`         |
| `internal_server_error`  | 5xx         | All routes (sanitised by 5xx handler)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | `{}`                                               |


### Allowlist routes (sprint row #1)


| `error_code`                  | HTTP status | Used by                                     | `details` schema                      |
| ----------------------------- | ----------- | ------------------------------------------- | ------------------------------------- |
| `self_allowlist_forbidden`    | 400         | `/agents/{id}/allowlist/{target_id}` (POST) | `{ owner_id: string }`                |
| `allowlist_capacity_exceeded` | 429         | `/agents/{id}/allowlist/{target_id}` (POST) | `{ owner_id: string, max_size: int }` |


`agent_not_found` and `api_key_agent_mismatch` rows above are also raised here — see the *Used by* column.

### Registry routes (sprint row #2a — partial)


| `error_code`       | HTTP status | Used by                                               | `details` schema        |
| ------------------ | ----------- | ----------------------------------------------------- | ----------------------- |
| `subnet_not_found` | 400         | `POST /register` (DEV) and `POST /register-protected` | `{ subnet_id: string }` |


Pilot codes `agent_not_found` / `api_key_agent_mismatch` / `communication_rejected` are also raised by registry — see the *Used by* column on the pilot table.

The remaining 10 4xx sites in `acn/routes/registry.py` (dev-mode rejection, owner-token mismatch, ownership / permission errors, bulk-delete safety guard, `ValueError` claim path, and `Authorization` header rejects) need new catalog codes and are tracked as sprint rows #2b and #2c in `[docs/BACKLOG.md](../BACKLOG.md)`. Until those land, registry endpoints can emit *either* the flat schema (for migrated raise sites) *or* the legacy `{"detail": "..."}` shape (for unmigrated sites). The §4 SDK parsing template handles both correctly.

### Tasks routes (sprint row #4 — partial)

| `error_code`     | HTTP status | Used by                                                                                                                                                                                                       | `details` schema       |
| ---------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------- |
| `task_not_found` | 404         | `GET /tasks/{id}`, `POST /tasks/{id}/accept`, `POST /tasks/{id}/invite`, `POST /tasks/{id}/submit`, `POST /tasks/{id}/review`, `POST /tasks/{id}/cancel`, `GET /tasks/{id}/participations`, `POST /tasks/{id}/participations/{pid}/cancel`, `POST /tasks/{id}/participations/{pid}/approve`, `POST /tasks/{id}/participations/{pid}/reject`, `GET /tasks/{id}/internal`, `POST /tasks/agent/{id}/accept`, `POST /tasks/agent/{id}/submit` | `{ task_id: string }`  |

The remaining 26 4xx sites in `acn/routes/tasks.py` need new catalog codes — 9× `PermissionError` re-raises (403), 9× `ValueError` re-raises (400 — body / status validation), and 8 endpoint-specific 4xx (auth gates, body validation, subnet-membership enforcement on private tasks). They land alongside sprint rows #2b/#2c once the auth/permission codes settle on names — the ownership/permission semantics overlap heavily with registry's deferred set, so reusing the same vocabulary keeps the catalog small.

### Subnets routes (sprint row #3 — partial)

`subnet_not_found` is now raised at 7 sites in `acn/routes/subnets.py` — the public lookup endpoints (`GET /subnets/{id}`, `GET /subnets/{id}/agents`), the agent join/leave flows (`POST /subnets/{agent_id}/subnets/{subnet_id}`, `DELETE /subnets/{agent_id}/subnets/{subnet_id}`), the owner-protected delete's `except SubnetNotFoundException` branch (`DELETE /subnets/{id}`), and the two internal-token admin endpoints (`POST /subnets/{id}/members/{agent_id}`, `DELETE /subnets/{id}/members/{agent_id}`). Pilot codes `agent_not_found` and `api_key_agent_mismatch` are also raised here — see the *Used by* columns on the pilot table.

The remaining 7 4xx sites in `acn/routes/subnets.py` are deferred:

- **6 sites need new catalog codes** — 2 401 *authentication required* on the listing / private-subnet view paths, 3 403 *permission denied* on the same paths plus the owner-only `DELETE /subnets/{id}` path's `except PermissionError`, and 1 400 *invalid request* on `POST /subnets` (`ValueError` from the create flow). The auth/permission codes will be picked up alongside sprint row #2b once registry's auth gates settle on names; the 400 *invalid request* code lands with row #2c's `ValueError` claim path.
- **1 site is a pre-existing latent bug** — the `else: raise HTTPException(404)` short-circuit inside `delete_subnet`'s `try` body is silently rewritten to 500 by the surrounding catch-all `except Exception`. The migration intentionally does **not** convert this site because `ACNHTTPError` would have the same fate (it inherits from `Exception`, not `HTTPException`, by design). The fix requires the `except ACNHTTPError: raise` defence ticket in `[docs/BACKLOG.md](../BACKLOG.md)` and is tracked there alongside the registry catch-all defence work.

### Reserved (declared, not yet raised)

`wallet_rate_limit_exceeded` / `authentication_required` / `internal_token_invalid` / `insufficient_balance` / `resource_conflict`

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

The route modules `follows`, `payments`, `onchain`, `dependencies`, `manifest`, `analytics`, and `websocket` still raise vanilla `HTTPException` and are caught by the existing `_http_exception_handler` 4xx pass-through, which emits the legacy `{"detail": "..."}` / `{"detail": {...}}` shape. The `registry`, `subnets`, and `tasks` modules are partially migrated as of sprint rows #2a, #3, and #4 respectively — see the coexistence matrix below. These routes are NOT broken, they just speak the old contract until the migration sprint reaches them.

---

## 4. Transitional coexistence matrix

During the migration sprint, ACN-emitted 4xx responses carry **two distinct shapes** depending on which route emitted them. This is the single most important fact for SDK upgrade:


| Route group                                                                                                                    | Exception class                          | 4xx body shape                                                                         | Status           |
| ------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------- | -------------------------------------------------------------------------------------- | ---------------- |
| Pilot — `/communication/send`, `/broadcast`, `/broadcast-by-tag`, `/history`, `/history/{agent_id}/ack`, `/internal/send` [^1] | `ACNHTTPError`                           | `{ error_code, message, details, request_id }` (flat)                                  | ✅ Migrated       |
| `/api/v1/agents/{id}/allowlist/...` (POST/DELETE/GET) [^1]                                                                     | `ACNHTTPError`                           | `{ error_code, message, details, request_id }` (flat)                                  | ✅ Migrated       |
| `/api/v1/agents/`* (registry) [^1] [^2]                                                                                        | mixed (`ACNHTTPError` + `HTTPException`) | mixed (flat for migrated 4xx, nested `{"detail": "..."}` for unmigrated 4xx)           | 🟡 Partial (#2a) |
| `/api/v1/subnets/*` [^1] [^3]                                                                                                  | mixed (`ACNHTTPError` + `HTTPException`) | mixed (flat for migrated 4xx, nested `{"detail": "..."}` for unmigrated 4xx)           | 🟡 Partial (#3)  |
| `/api/v1/tasks/*` [^1] [^4]                                                                                                    | mixed (`ACNHTTPError` + `HTTPException`) | mixed (flat for migrated 4xx, nested `{"detail": "..."}` for unmigrated 4xx)           | 🟡 Partial (#4)  |
| `/api/v1/follows/*`                                                                                                            | `HTTPException`                          | `{ "detail": "..." }`                                                                  | ⏳ Pending        |
| `/api/v1/payments/*`                                                                                                           | `HTTPException`                          | `{ "detail": "..." }`                                                                  | ⏳ Pending        |
| `/api/v1/onchain/*`                                                                                                            | `HTTPException`                          | `{ "detail": "..." }`                                                                  | ⏳ Pending        |
| `/api/v1/communication/manifest/*`                                                                                             | `HTTPException`                          | `{ "detail": "..." }`                                                                  | ⏳ Pending        |
| `/api/v1/analytics/*`                                                                                                          | `HTTPException`                          | `{ "detail": "..." }`                                                                  | ⏳ Pending        |
| `/ws/*` (websocket)                                                                                                            | `HTTPException`                          | `{ "detail": "..." }`                                                                  | ⏳ Pending        |
| Auth dependency rejects (any route)                                                                                            | `HTTPException`                          | `{ "detail": "..." }`                                                                  | ⏳ Pending        |
| All routes — 5xx                                                                                                               | `HTTPException` (sanitised)              | `{ error, error_code, message, details, request_id }` (flat, with deprecation `error`) | ✅ Aligned        |


Each migration PR in the sprint flips a row from ⏳ → ✅. SDK consumers can depend on this matrix as the source of truth for which response shape a given endpoint emits.

[^2]: **Registry partial migration (sprint #2a).** 19 of the 29 4xx raise sites in `acn/routes/registry.py` are migrated — the ones that map directly to existing catalog codes: 15× `AGENT_NOT_FOUND` + 1× `API_KEY_AGENT_MISMATCH` + 2× `SUBNET_NOT_FOUND` + 1× `COMMUNICATION_REJECTED` (which also flattens the legacy nested `{"detail": {"detail": "..."}}` proxy shape). The remaining 10 4xx sites (dev-mode rejection, owner-token mismatch, the 2 `Authorization` header rejects, the internal-token requirement, ownership / permission errors ×3, the bulk-delete safety guard, and the `ValueError` claim path) need new catalog codes and ship as sprint rows #2b and #2c. The 7 5xx sites in registry (502 / 503 / catch-all 500) stay on `HTTPException` by design — `ACNHTTPError` rejects 5xx at construction time so the existing sanitised-5xx handler chain stays in charge. SDK clients must therefore handle BOTH the flat schema and the legacy `{"detail": "..."}` shape on registry endpoints — the §4 SDK parsing template (`if "error_code" in body`) does this correctly. Sprint row #2 flips fully ✅ when #2c lands.

[^3]: **Subnets partial migration (sprint #3).** 13 of the 20 4xx raise sites in `acn/routes/subnets.py` are migrated — the ones that map directly to existing catalog codes: 7× `SUBNET_NOT_FOUND` + 3× `AGENT_NOT_FOUND` + 3× `API_KEY_AGENT_MISMATCH`. The remaining 7 4xx sites split into two groups: (1) **six sites need new catalog codes** — 2× 401 *authentication required* (listing / private-subnet view), 3× 403 *permission denied* (same paths plus `delete_subnet`'s `except PermissionError`), and 1× 400 *invalid request* on `POST /subnets` (`ValueError` from the create flow). These pick up alongside sprint rows #2b/#2c once auth/permission codes settle on names. (2) **One site is a pre-existing latent bug** — the `else: raise HTTPException(404)` short-circuit inside `delete_subnet`'s `try` body is silently rewritten to 500 by the surrounding catch-all `except Exception` (today and after migration, since `ACNHTTPError` is also `Exception`-typed by design). Fixing it requires the `except ACNHTTPError: raise` defence ticket in `[docs/BACKLOG.md](../BACKLOG.md)` and is tracked there alongside the registry catch-all defence work. The 8 5xx sites in subnets stay on `HTTPException` by design (sanitised-5xx handler chain). Sprint row #3 flips fully ✅ when both the new auth/permission codes and the catch-all defence ticket land.

[^4]: **Tasks partial migration (sprint #4).** 13 of the 39 4xx raise sites in `acn/routes/tasks.py` are migrated — every `except TaskNotFoundException: raise … from None` site (`GET /tasks/{id}`, `POST /tasks/{id}/{accept,invite,submit,review,cancel}`, `GET /tasks/{id}/participations`, the three `POST /tasks/{id}/participations/{pid}/{cancel,approve,reject}`, `GET /tasks/{id}/internal`, `POST /tasks/agent/{id}/{accept,submit}`). All 13 use the same uniform shape `ACNHTTPError(TASK_NOT_FOUND, 404, {"task_id": …}) from None`, making this the most uniform sprint of the migration so far. The remaining 26 4xx sites need new catalog codes — 9× `PermissionError` re-raises (403, ownership semantics overlapping registry's deferred set), 9× `ValueError` re-raises (400, body / status validation), and 8 endpoint-specific 4xx (auth gates on `list_tasks` / `match_tasks_for_agent`, body validation, subnet-membership enforcement on private tasks). They pick up alongside sprint rows #2b/#2c once the auth / permission codes settle on names. The 1 5xx site (`create_task` catch-all) stays on `HTTPException` by design (sanitised-5xx handler chain). Sprint row #4 flips fully ✅ when the auth / permission / validation codes land.

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
5. `**payments**` — `insufficient_balance` reserved
6. `**follows**` — small surface, similar shape to `allowlist`
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

