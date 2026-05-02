# ACN Error Schema

**Status**: ✅ Pilot landed — Phase 2 review v2 P1 #11 (May 2026)
**Pilot scope**: communication routes (`/api/v1/communication/*`)
**Min SDK consumer**: ACN python client `0.5.0+` (synchronised with P1 #10 `X-ACN-SDK-Min-Version`)

This document is the canonical specification of the ACN error response schema. SDK authors and dashboard maintainers should treat it as the contract; route authors and reviewers should treat it as the migration guide.

The implementation lives in [`acn/core/errors.py`](../../acn/core/errors.py); the central exception handler is registered in [`acn/api.py`](../../acn/api.py).

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

| Field        | Type                          | Stability                                                                                              |
| ------------ | ----------------------------- | ------------------------------------------------------------------------------------------------------ |
| `error_code` | `string` (snake_case ASCII)   | **Stable contract** — the only field SDK clients should branch on                                      |
| `message`    | `string`                      | Human-readable prose. **Not stable** — SDK clients MUST NOT string-match on it                         |
| `details`    | `object` (code-specific)      | Per-code structured context. Field semantics depend on `error_code`; undocumented fields are unstable  |
| `request_id` | `string` (UUID v4)            | Per-request correlation id. Echoed in the `X-Request-ID` response header                               |

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

Tracker: see "Phase 2 review v2 P1 #11 — Error schema migration sprint" in [`docs/BACKLOG.md`](../BACKLOG.md) for the field name, double-emit start date, removal target date, and owner.

---

## 2. ErrorCode catalog

The `ErrorCode` enum in [`acn/core/errors.py`](../../acn/core/errors.py) is a **forward catalog** — codes that aren't yet raised by any pilot route are included so future migrations can adopt them without reopening this PR. Every declared code has a `_DEFAULT_MESSAGES` entry; a CI test (`tests/core/test_error_schema.py::TestCatalogStructure`) enforces the structural invariant.

### Pilot use (communication routes)

| `error_code`               | HTTP status | Used by                                                | `details` schema                                         |
| -------------------------- | ----------- | ------------------------------------------------------ | -------------------------------------------------------- |
| `agent_not_found`          | 404         | `/send` `/broadcast` `/broadcast-by-tag` `/history` `/history/ack` `/internal/send` | `{ agent_id: string }`                            |
| `api_key_agent_mismatch`   | 403         | `/history` `/history/ack`                              | `{ path_agent: string, key_agent: string }`              |
| `from_agent_mismatch`      | 403         | `/send` `/broadcast` `/broadcast-by-tag`               | `{ authenticated_as: string, from_agent: string }`       |
| `communication_rejected`   | 403         | `/send` `/internal/send` (defensive)                   | `{ reason: string, reject_reason: string \| null }`      |
| `unknown_strategy`         | 422         | `/broadcast`                                           | `{ strategy: string, expected: string[] }`               |
| `internal_server_error`    | 5xx         | All routes (sanitised by 5xx handler)                  | `{}`                                                     |

### Reserved (declared, not yet raised)

`subnet_not_found` / `task_not_found` / `allowlist_capacity_exceeded` / `self_allowlist_forbidden` / `wallet_rate_limit_exceeded` / `authentication_required` / `internal_token_invalid` / `insufficient_balance` / `resource_conflict`

Reserved codes will be picked up by the migration sprint as each route is converted (see Section 7).

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

### Non-pilot routes

The remaining 11 route modules (`allowlist`, `subnets`, `tasks`, `follows`, `payments`, `onchain`, `dependencies`, `manifest`, `analytics`, `registry`, `websocket`) still raise vanilla `HTTPException` and are caught by the existing `_http_exception_handler` 4xx pass-through, which emits the legacy `{"detail": "..."}` / `{"detail": {...}}` shape. See the coexistence matrix below — these routes are NOT broken, they just speak the old contract until the migration sprint reaches them.

---

## 4. Transitional coexistence matrix

During the migration sprint, ACN-emitted 4xx responses carry **two distinct shapes** depending on which route emitted them. This is the single most important fact for SDK upgrade:

| Route group                                                                                  | Exception class    | 4xx body shape                                                                          | Status |
| -------------------------------------------------------------------------------------------- | ------------------ | --------------------------------------------------------------------------------------- | ------ |
| Pilot — `/communication/send`, `/broadcast`, `/broadcast-by-tag`, `/history`, `/history/ack`, `/internal/send` | `ACNHTTPError`     | `{ error_code, message, details, request_id }` (flat)                                   | ✅ Migrated |
| `/api/v1/allowlist/*`                                                                        | `HTTPException`    | `{ "detail": "..." }`                                                                   | ⏳ Pending |
| `/api/v1/subnets/*`                                                                          | `HTTPException`    | `{ "detail": "..." }`                                                                   | ⏳ Pending |
| `/api/v1/tasks/*`                                                                            | `HTTPException`    | `{ "detail": "..." }`                                                                   | ⏳ Pending |
| `/api/v1/follows/*`                                                                          | `HTTPException`    | `{ "detail": "..." }`                                                                   | ⏳ Pending |
| `/api/v1/payments/*`                                                                         | `HTTPException`    | `{ "detail": "..." }`                                                                   | ⏳ Pending |
| `/api/v1/onchain/*`                                                                          | `HTTPException`    | `{ "detail": "..." }`                                                                   | ⏳ Pending |
| `/api/v1/agents/*` (registry)                                                                | `HTTPException`    | `{ "detail": "..." }`                                                                   | ⏳ Pending |
| `/api/v1/communication/manifest/*`                                                           | `HTTPException`    | `{ "detail": "..." }`                                                                   | ⏳ Pending |
| `/api/v1/analytics/*`                                                                        | `HTTPException`    | `{ "detail": "..." }`                                                                   | ⏳ Pending |
| `/ws/*` (websocket)                                                                          | `HTTPException`    | `{ "detail": "..." }`                                                                   | ⏳ Pending |
| Auth dependency rejects (any route)                                                          | `HTTPException`    | `{ "detail": "..." }`                                                                   | ⏳ Pending |
| All routes — 5xx                                                                             | `HTTPException` (sanitised) | `{ error, error_code, message, details, request_id }` (flat, with deprecation `error`) | ✅ Aligned |

Each migration PR in the sprint flips a row from ⏳ → ✅. SDK consumers can depend on this matrix as the source of truth for which response shape a given endpoint emits.

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

| Situation                                                              | Action                                                                            |
| ---------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Existing code matches the situation (e.g. another "X not found" route) | Reuse — keeps the catalog small and predictable for SDK code-gen                  |
| Situation is genuinely new                                             | Add a new `ErrorCode` member + `_DEFAULT_MESSAGES` entry; document in this file   |
| Same code with different per-route details                             | Reuse the code; vary `details` keys; document each route's `details` schema here  |

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

The pilot migration covered communication routes (~14 4xx sites). The sprint to cover the remaining 11 modules is tracked in [`docs/BACKLOG.md`](../BACKLOG.md) under "Phase 2 review v2 P1 #11 — Error schema migration sprint".

Suggested ordering (cheapest / most impactful first):

1. **`allowlist`** — already has domain exceptions (`AllowlistCapacityExceededError`, `SelfAllowlistError`); 1:1 mapping to reserved catalog codes
2. **`registry`** — frequently consumed by SDK; `agent_not_found` / `agent_already_exists` mappings are obvious
3. **`subnets`** — `subnet_not_found` is already reserved in the catalog
4. **`tasks`** — `task_not_found` reserved
5. **`payments`** — `insufficient_balance` reserved
6. **`follows`** — small surface, similar shape to `allowlist`
7. **`onchain`** — needs new codes for ERC-8004 specific failures
8. **`manifest`** — small surface
9. **`analytics`** — small surface, mostly 4xx pass-through today
10. **`dependencies`** — auth-shared module; high care due to security relevance
11. **`websocket`** — last (different protocol surface; may need separate schema treatment)

After all rows in Section 4 flip to ✅, the SDK fallback `else` branch in Section 4's parsing template can be removed and the 30-day 5xx `error` field deprecation can land — at which point the schema is fully unified and ACN can declare the migration complete.

---

## 8. Cross-references

- [`acn/core/errors.py`](../../acn/core/errors.py) — `ErrorCode` / `ACNHTTPError` / `ACNErrorResponse` source
- [`acn/api.py`](../../acn/api.py) — `_acn_http_error_handler` central handler + sanitised 5xx handlers
- [`tests/core/test_error_schema.py`](../../tests/core/test_error_schema.py) — schema invariants
- [`tests/test_error_sanitisation.py`](../../tests/test_error_sanitisation.py) — 5xx sanitisation contract
- [`docs/BACKLOG.md`](../BACKLOG.md) — migration sprint tracker, 5xx field deprecation ticket
- [`docs/features/acn-communication-economic-model.md`](acn-communication-economic-model.md) — Phase 2 P1 #10 (`X-ACN-SDK-Min-Version`) and #11 decision history
