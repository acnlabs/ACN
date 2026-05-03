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
| `agent_not_found`        | 404         | communication routes (`/send` `/broadcast` `/broadcast-by-tag` `/history` `/history/{agent_id}/ack` `/internal/send`); allowlist routes (POST); registry routes (`GET /agents/{id}` `POST /heartbeat` `GET /me` `GET /agent-card.json` `GET /agent-registration.json` `GET /endpoint` `GET /policy` `PATCH /policy` `DELETE /agents/{id}` `POST /claim` `POST /transfer` `POST /release` `GET /wallets`, plus the catch-all proxy); subnets routes (`POST /subnets/{agent_id}/subnets/{subnet_id}` `DELETE /subnets/{agent_id}/subnets/{subnet_id}` `GET /subnets/{agent_id}/subnets`); payments routes (`POST /{agent_id}/payment-capability` `POST /{agent_id}/token-pricing`); follows routes (`POST /agents/{agent_id}/follows/{target_id}` — followee lookup miss); onchain routes (`POST /onchain/agents/{agent_id}/bind` `GET /onchain/agents/{agent_id}` `GET /onchain/agents/{agent_id}/reputation` `GET /onchain/agents/{agent_id}/validation` — every route's `except AgentNotFoundException` branch) | `{ agent_id: string }`                             |
| `api_key_agent_mismatch` | 403         | communication routes (`/history` `/history/{agent_id}/ack`); allowlist routes (POST/DELETE/GET); registry routes (`POST /heartbeat`); subnets routes (`POST /subnets/{agent_id}/subnets/{subnet_id}` `DELETE /subnets/{agent_id}/subnets/{subnet_id}` `GET /subnets/{agent_id}/subnets`); payments routes (`POST /{agent_id}/payment-capability` `GET /tasks/agent/{agent_id}` `GET /stats/{agent_id}` `POST /{agent_id}/token-pricing`); follows routes (`POST /agents/{agent_id}/follows/{target_id}` `DELETE /agents/{agent_id}/follows/{target_id}` — path-mismatch gates); analytics routes (`GET /analytics/activities` — cross-tenant filter probe); onchain routes (`POST /onchain/agents/{agent_id}/bind` — path-key mismatch gate); websocket routes (`GET /api/v1/websocket/agent/{agent_id}/status` — path-key mismatch gate)                                                                                                                                                                                                                                                                                               | `{ path_agent: string, key_agent: string }`        |
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

**Field-name choice — `follower_id` vs `owner_id`.** sprint #1 (allowlist) uses `owner_id` for the corresponding `self_allowlist_forbidden` and `allowlist_capacity_exceeded` codes because the allowlist is *owned* by the agent. follow has no ownership semantics — the operating entity is a *follower*, and both the service-layer exception names (`FollowLimitExceededError`, `SelfFollowError`) and the `acn-follow-proposal.md` response bodies use `follower`. The two sprints are semantically parallel (per-agent capacity ceiling + self-reference forbidden) but field-name divergent on purpose. SDK clients should not alias the two — a `follow_limit_exceeded` retry handler reads `details.max_follows`, an `allowlist_capacity_exceeded` retry handler reads `details.max_size`. The `MAX_FOLLOWS` constant value (currently 10000) is published in `details.max_follows` so clients can pre-flight on retry without hardcoding it; it is a public contract knob and any change must coordinate with SDK release notes.

### Payments routes (sprint row #5)


| `error_code`                    | HTTP status | Used by                                                                                                                            | `details` schema             |
| ------------------------------- | ----------- | ---------------------------------------------------------------------------------------------------------------------------------- | ---------------------------- |
| `payment_capability_not_found`  | 404         | `GET /payments/{agent_id}/payment-capability`                                                                                      | `{ agent_id: string }`       |
| `payment_task_not_found`        | 404         | `GET /payments/tasks/{task_id}` (internal-token)                                                                                   | `{ task_id: string }`        |
| `token_pricing_not_configured`  | 404         | `GET /payments/{agent_id}/token-pricing`, `POST /payments/billing/estimate`, `POST /payments/billing/charge` (internal-token)      | `{ agent_id: string }`       |
| `billing_transaction_not_found` | 404         | `GET /payments/billing/transactions/{transaction_id}` (internal-token)                                                             | `{ transaction_id: string }` |


Pilot codes `agent_not_found`, `api_key_agent_mismatch`, and `from_agent_mismatch` are also raised by payments — see the *Used by* column on the pilot table. The 3 remaining 5xx sites (`set_payment_capability`, `create_payment_task`, `set_token_pricing` catch-alls) stay on raw `HTTPException(500)` per the sanitisation contract; all three carry the `except ACNHTTPError: raise` + `except HTTPException: raise` defence layers (P3 cross-module catch-all defence).

`INSUFFICIENT_BALANCE` stays in the reserved group of the `ErrorCode` catalog: `payments.py` only surfaces *resource-existence* failures (the four codes above), not balance failures. Balance failures live one layer deeper (wallet / billing subsystem) and may surface at a different boundary in a future sprint.

### Onchain (ERC-8004) routes (sprint row #7)

The onchain router (`/api/v1/onchain/*`, ERC-8004 NFT identity binding) introduces six new ErrorCodes — all six are *route-local* (not lifted to the cross-module group) because none of these failure modes is expected to surface from any other route module today.

| `error_code`                    | HTTP status | Used by                                                                                  | `details` schema                                       |
| ------------------------------- | ----------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| `erc8004_token_id_missing`      | 422         | `_parse_token_id_or_422` defensive ``value is None`` branch — *unreachable from HTTP today* (every route 404s upstream via `erc8004_not_bound` before reaching the helper). Pinned by helper-level unit test only. | `{ agent_id: string }`                                 |
| `erc8004_token_id_corrupt`      | 422         | `_parse_token_id_or_422` `int()` coercion failure — reached by `GET /onchain/agents/{id}/reputation` and `GET /onchain/agents/{id}/validation` when an agent's stored `erc8004_agent_id` is non-numeric (manually-edited or extremely-old DB row). | `{ agent_id: string }`                                 |
| `erc8004_chain_mismatch`        | 422         | `POST /onchain/agents/{id}/bind` H-erc8004 audit defence — `body.chain` (when provided) disagrees with the server-derived `eip155:{erc8004_chain_id}`. | `{ server_chain: string, client_chain: string }`       |
| `erc8004_token_already_bound`   | 409         | `POST /onchain/agents/{id}/bind` when `_check_duplicate_token` finds the requested `token_id` is already bound to a *different* ACN agent (re-binding the same token to the same agent is idempotent and does not raise). | `{ token_id: int, bound_agent_id: string, requesting_agent_id: string }` |
| `erc8004_registration_mismatch` | 422         | `POST /onchain/agents/{id}/bind` when the on-chain `tokenURI` does not match ACN's expected agent-registration URL. | `{ token_id: int, expected_url: string }`              |
| `erc8004_not_bound`             | 404         | `GET /onchain/agents/{id}/reputation` (×1) + `GET /onchain/agents/{id}/validation` (×1) — agent exists but has no `erc8004_agent_id` yet. | `{ agent_id: string }`                                 |

Pilot codes `agent_not_found` (×4 — every route's `except AgentNotFoundException` branch) and `api_key_agent_mismatch` (×1 — bind path-key gate) are also raised by onchain — see the *Used by* columns on the pilot table.

**Why six distinct codes (vs a single `ERC8004_FAILURE` with `details.kind`).** Each of the six is a *materially different* failure mode that an SDK consumer wants to branch on without inspecting `details`. SDK clients facing `erc8004_token_already_bound` need to surface "this token is already taken — pick a different one or contact the bound agent's owner"; clients facing `erc8004_registration_mismatch` need to surface "register on-chain with the correct agentURI first"; clients facing `erc8004_not_bound` need to surface "this agent has not bound an on-chain identity yet — call `POST /bind` first". Collapsing all of these into a single code with a discriminator field would force every SDK consumer to read the discriminator just to render the right user-facing message — exactly the kind of branching ACN is trying to put on `error_code` in the first place.

**Two 5xx sites preserved.** `bind_onchain_identity` raises `HTTPException(503)` when the configured RPC endpoint reports a different chain_id than `settings.erc8004_chain_id` — this is operator-side misconfig (or an RPC-swap attack scenario where the operator unknowingly points at the wrong network); fail-closed is correct because the client cannot fix it by retrying. `get_agent_validation` raises `HTTPException(503)` when `ERC8004_VALIDATION_CONTRACT` is not configured — same shape (the Validation Registry is still experimental and the contract addresses are not yet publicly published in some environments). Both stay on `HTTPException` rather than migrating to `ACNHTTPError` because the latter rejects 5xx at construction time so the existing 5xx sanitisation handler stays in charge — operator-tier diagnostic prose in the response detail is sanitised before it leaves the process.

**Field-name choice rationale.**

* `bound_agent_id` IS echoed back in `erc8004_token_already_bound.details` *on purpose*. The reverse-index that powers `_check_duplicate_token` is publicly readable on the ERC-8004 contract — anyone can call `ownerOf(token_id)` against the chain and resolve the same mapping client-side. Hiding it from the response body would not actually keep the binding private; it would only force SDK clients to round-trip through the public chain to get a piece of data the route already knows. Echoing it back keeps the SDK contract honest and avoids a false sense of privacy.
* `expected_url` is preserved verbatim in `erc8004_registration_mismatch.details` (not summarised, not truncated). The caller needs the full URL to set as the on-chain `tokenURI` — truncating or omitting it would force the caller to reconstruct it from `gateway_base_url` + `agent_id`, which is fragile (gateway URL changes, future path edits) and provides zero diagnostic value over echoing the canonical string ACN actually expects.
* `stored_value` is *deliberately omitted* from `erc8004_token_id_corrupt.details` even though it is logged operator-side. The corrupt value is potentially attacker-controlled DB content (an attacker who compromises the persistence layer could plant a `stored_value` payload and use error-body echoing to amplify their attack). Logs are operator-side and sanitised; the response body keeps only the route-trusted `agent_id`.
* `token_id` is `int` in `details` (not `str`). The pre-migration error message stringified it inline (`f"Token ID {body.token_id} ..."`), but the migration takes the typed value from the validated request body and passes it through unchanged. SDK clients consuming the new shape can rely on the JSON number type; code generators emit `int` rather than `str` for the field.

**Helper unit-test pin for ``erc8004_token_id_missing``.** `_parse_token_id_or_422(None, ...)` is unreachable from HTTP today — both reputation and validation routes 404 with `erc8004_not_bound` upstream of the helper. We pin the flat-schema contract via a *helper-level* unit test (`test_onchain_error_schema.py::test_helper_raises_acn_http_error_on_none`) so a future refactor that drops the upstream guard does not silently regress the response shape. If the upstream guard ever does come out, this raise becomes the load-bearing 404→422 fallback and the unit test is what catches the contract drift.

12 contract tests in `tests/routes/test_onchain_error_schema.py` pin every raise site (1:1 coverage; 11 HTTP-level + 1 helper-level unit test). Two pre-existing onchain test files (`test_onchain_chain_id_h_erc8004.py`, `test_onchain_token_id_corruption.py`) needed regression fixes — they asserted on the legacy `r.json()["detail"]` field which the new flat schema lacks; the migration commit re-asserts on `error_code` and the strict `details` shape, preserving the original audit-defence intent (chain mismatch caught + corrupt token id surfaces as 422-not-500) while modernising the wire shape.

### Manifest routes (sprint row #8)


| `error_code`                  | HTTP status | Used by                                                              | `details` schema                       |
| ----------------------------- | ----------- | -------------------------------------------------------------------- | -------------------------------------- |
| `manifest_entry_not_found`    | 404         | `DELETE /communication/manifest/{agent_id}/{mid}`                    | `{ agent_id: string, mid: string }`    |
| `manifest_content_not_found`  | 404         | `GET /communication/content/{mid}`                                   | `{ owner_id: string, mid: string }`    |


Manifest has **no** pilot-code reuse — its raise sites are pure resource-existence misses, not auth/ownership/path-mismatch surfaces. `manifest.py` has **0** 5xx catch-all sites: each raise is a simple "service returned `None`/`False`" guard, no `except Exception:` blocks to defend.

**Field-name choice — `agent_id` vs `owner_id`.** The two codes deliberately use different `details` field names because the routes have different parameter shapes. `DELETE /manifest/{agent_id}/{mid}` exposes `agent_id` directly in the URL path and the route layer has no role beyond echoing it back, so `details.agent_id` matches the surface the caller saw. `GET /content/{mid}` has *no* path `agent_id` — the owner is derived from the Bearer API key — so calling that field `agent_id` would mislead SDK clients into thinking they passed it explicitly, and would obscure the security-critical fact that `owner_id` came from server-side key resolution. The `owner_id` choice also pairs naturally with the cross-tenant probe contract below (which always returns the *probing* caller as `owner_id`, never the real owner of the mid).

**Two distinct codes vs a single `manifest_not_found` with `details.kind`.** SDK clients can branch on `error_code` directly without inspecting `details`, mirroring the discriminator pattern used by every other migrated route. The split also lets the cross-sprint `details` consistency test (`tests/test_error_code_details_consistency.py`) give *strict* (not union-schema) protection on each code's `details` shape — a single `manifest_not_found` would have to be added to `UNION_SCHEMA_CODES` and lose the per-code field-name guarantee.

**Existence-leak invariant.** Both codes are also raised on cross-tenant probes (a caller authenticating as agent A asking for agent B's manifest entry / content). The route layer never returns 403 for these cases — that would leak the existence of the entry/content to an attacker probing for other agents' queues. Crucially, the *body shape* must be identical to a legitimate own-resource miss for the same reason: a divergent shape (e.g. omitting `details.owner_id` only on cross-tenant probes) would reintroduce the existence leak the 404-not-403 design exists to prevent. For `manifest_content_not_found` specifically, `details.owner_id` always reflects the *probing* caller's agent ID (as resolved from their API key), never the real owner of the mid — `tests/routes/test_manifest_error_schema.py::test_cross_tenant_miss_emits_same_shape_as_legit_miss` pins this.

3 contract tests in `tests/routes/test_manifest_error_schema.py` pin both raise sites: 1 entry test (DELETE on missing/cross-tenant mid) + 2 content tests (legit own-resource miss + cross-tenant probe with the leak-guard assertion).

### Analytics routes (sprint row #9)

Analytics introduces **no new ErrorCodes** — all 3 4xx raise sites in `acn/routes/analytics.py` reuse the cross-module catalog. The sprint contributes one new `AUTHENTICATION_REQUIRED` reason value (`auth_required_for_agent_filter`) and a documented schema-bucket invariant on the `agent_ids=` multi-id filter path.

| `error_code` (reused)     | HTTP status | Used by                                                                                  | `details` schema                            |
| ------------------------- | ----------- | ---------------------------------------------------------------------------------------- | ------------------------------------------- |
| `authentication_required` | 401         | `GET /api/v1/analytics/activities` — when an `agent_id` or `agent_ids` filter is requested without (or with a malformed) `Authorization: Bearer …` header; or with a Bearer token that does not resolve. | `{ reason: "auth_required_for_agent_filter" \| "invalid_api_key" }` |
| `api_key_agent_mismatch`  | 403         | `GET /api/v1/analytics/activities` — Bearer key resolves, but caller is asking for activity belonging to a *different* agent (single-id `agent_id` filter, or any element of a multi-id `agent_ids` filter). | `{ path_agent: string, key_agent: string }` (strict — see invariant below) |

The other six analytics endpoints (`GET /agents`, `GET /agents/{id}`, `GET /messages`, `GET /latency`, `GET /subnets`, plus the *unfiltered* branch of `GET /activities`) have **no file-local 4xx sites** — auth is delegated to `InternalTokenDep` / no-auth (rate-limited only), both surfaces already covered by sprint #10 (`dependencies`) for the dep path and by the FastAPI 422 path for body-validation failures. The migration is therefore confined to the single filter-bearing route, even though `analytics.py` is multi-route.

**Multi-id `agent_ids` filter — strict-schema invariant.** When `list_activities` receives `agent_ids=` (a comma-separated multi-id filter) and *more than one* of the requested ids does not match the authenticated key, the body still emits the strict `{path_agent, key_agent}` shape — surfacing only the *first sorted* mismatched id in `details.path_agent`. We deliberately do NOT echo back the full set of requested ids (or the full set of mismatched ids), for two reasons:

* **Schema-bucket discipline.** `API_KEY_AGENT_MISMATCH` is in the strict-schema bucket of `tests/test_error_code_details_consistency.py` (pinned across sprints #1 / #5 / #6 / #10). Adding a new key (e.g. `requested_agents: list[str]`) for the multi-id case would force the code into the union-schema bucket and weaken the cross-sprint contract — every other callsite would inherit the looser shape for zero gain at their callsites.
* **Diagnostic redundancy.** The caller already has the full multi-id filter list client-side; echoing it back adds no diagnostic value but locks the response shape into a more permissive contract. Surfacing one canonical mismatched id (deterministically chosen via `sorted(...)[0]`) keeps the SDK type-gen output identical between single-id and multi-id callers.

`tests/routes/test_analytics_error_schema.py::test_multi_agent_ids_mismatch_surfaces_first_sorted` pins this — if the route ever starts echoing the full mismatch list, the test flips and the contributor must consciously demote `API_KEY_AGENT_MISMATCH` to the union-schema bucket.

**Schema-bucket reuse for `AUTHENTICATION_REQUIRED`.** The two reasons emitted by analytics (`auth_required_for_agent_filter` + `invalid_api_key`) both fit the existing union-schema `details.reason` enum; no new keys are introduced. The `invalid_api_key` reason value is byte-identical to the value emitted by `dependencies._resolve_agent_by_bearer` (sprint #10) — same SDK-actionable failure, same value. The new `auth_required_for_agent_filter` value is distinct because the *trigger* differs (filter parameter present + auth absent vs auth present + invalid); SDK clients can tell "you must authenticate to filter" apart from "your authentication failed".

`analytics.py` has **0 5xx catch-all sites** — neither `list_activities` nor any of the other six routes wrap their bodies in `try`/`except Exception`. The cross-module catch-all defence (P3) does not apply.

5 contract tests in `tests/routes/test_analytics_error_schema.py` pin every raise site: 2 for `auth_required_for_agent_filter` (covering missing-header / non-Bearer-prefix branches), 1 for `invalid_api_key`, 2 for `api_key_agent_mismatch` (single-id `agent_id` form + multi-id `agent_ids` form, both pinning the strict `{path_agent, key_agent}` shape).

**Test-wiring note for future contributors.** The contract test file `monkeypatch.setattr`-es `acn.routes.analytics.get_agent_service` rather than relying on `app.dependency_overrides[get_agent_service]`, because `list_activities` fetches the agent service via a *module-level* call inside the route body (`agent_service = get_agent_service()`) rather than declaring it as a FastAPI parameter dependency. `app.dependency_overrides` only intercepts the parameter-Depends machinery, not module-level lookups; without the monkeypatch the agent-service stub is silently bypassed and tests fall through to the `invalid_api_key` branch. The websocket endpoint `WEBSOCKET /ws/{agent_id}` follows the same module-level call pattern; once sprint #11b lands its contract tests for handshake auth failures, expect to use the same wiring trick there. (The HTTP routes in `acn/routes/websocket.py`, migrated under sprint #11a, take the simpler path of overriding the `verify_agent_api_key` FastAPI dependency directly — they consume auth via `AgentApiKeyDep`, not a module-level call.)

### Websocket HTTP routes (sprint row #11a)

`acn/routes/websocket.py` registers three endpoints on its `APIRouter`. Two are conventional HTTP routes; the third is the WebSocket-protocol endpoint (`WEBSOCKET /ws/{agent_id}`) whose error contract uses RFC 6455 close codes and is governed by sprint #11b — entirely out of scope for #11a.

The HTTP routes:

* `GET /api/v1/websocket/connections` — internal-only summary of live connections, gated by `InternalTokenDep`. **0 file-local 4xx raise sites** — auth-fail propagates from `dependencies.py` (already migrated under sprint #10).
* `GET /api/v1/websocket/agent/{agent_id}/status` — gated by `AgentApiKeyDep`, additionally enforces "agent may only query *its own* status". **1 file-local 4xx raise site**: path-vs-key mismatch → `API_KEY_AGENT_MISMATCH` (403) with the cross-module strict shape `{path_agent, key_agent}`. No new ErrorCode.

**Cross-module reuse only.** Sprint #11a reuses `API_KEY_AGENT_MISMATCH` exactly as registry / payments / follows / onchain / analytics emit it, so the AST consistency test at `tests/test_error_code_details_consistency.py` continues to enforce the strict `{path_agent, key_agent}` invariant across all 16 emitters.

**Documentation drift correction.** Sprint #7 had inadvertently declared the websocket router as having no HTTP routes (the `TestNonMigratedRoutersDoNotAdvertiseDefault` docstring and the sprint-row header in this document both made that incorrect claim). #11a corrects the drift by:

1. Adding `responses=ACN_DEFAULT_RESPONSES` to the router so OpenAPI advertises `ACNErrorResponse` for both HTTP routes via FastAPI's uniform router-level merge.
2. Migrating the single 4xx site from raw `HTTPException` to `ACNHTTPError`.
3. Adding the `status` endpoint to `tests/test_openapi_acn_error_response.py::REPRESENTATIVE_ENDPOINTS` so the spec coverage stays pinned.
4. Updating the `TestNonMigratedRoutersDoNotAdvertiseDefault` docstring + this document to correctly describe the split between HTTP routes (now ✅ in #11a) and the WS-protocol endpoint (⏳ in #11b).

**0 catch-all 5xx sites** in this router — neither HTTP endpoint wraps its body in `try`/`except Exception`.

1 contract test in `tests/routes/test_websocket_error_schema.py` pins the single raise site (1:1 coverage), and additionally asserts the WS manager's `is_user_connected` is NOT called on the mismatch path — a regression that swapped check order would let strangers probe whether arbitrary agents are online.

### Cross-module catalog (sprint row #2b)

Six `ErrorCode` members designed to be **shared by `registry`, `subnets`, and `tasks`** so an SDK consumer can write one set of fallback handlers regardless of which module emitted the error. The cross-module set is the deliverable that unblocked rows #3-followup and #4-followup; see [`docs/BACKLOG.md`](../BACKLOG.md) for the per-row status.

| `error_code`              | HTTP status | Raised by                                                                                                                                                                                                                                                                                                                                                                                                                            | `details` schema                                                       |
| ------------------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------- |
| `authentication_required` | 401         | **registry** — `GET /me` (×2: invalid Authorization header format / unrecognised API key); **subnets** — `GET /subnets?owner=…` (owner-filter requires auth), `GET /subnets/{id}/agents` (private subnet requires auth); **tasks** — `require_task_write_auth` agent path (invalid `acn_xxx` API key); **dependencies** (×4 — every router that mounts an auth dep): `_resolve_agent_by_bearer` invalid_api_key, `verify_agent_api_key` invalid_authorization_header_format, `verify_proxy_caller` invalid_authorization_header_format, `verify_owner_or_internal` owner_or_internal_credential_required; **analytics** (×2) — `list_activities` requires Bearer when filtering by `agent_id` / `agent_ids` (`reason="auth_required_for_agent_filter"`) and rejects unresolved Bearer keys (`reason="invalid_api_key"`)                                                                                                                                 | `{ reason: enum, subnet_id?: string }`                                 |
| `internal_token_invalid`  | 401 / 403   | **registry** — `POST /agents/join/internal` (X-Internal-Token missing or mismatched, 401); **dependencies** (×2 — every router that mounts `InternalTokenDep` or `OwnerOrInternalDep`): `verify_internal_token` (403), `verify_owner_or_internal` priority branch (403). Status-code split is intentional: registry's join endpoint pre-dates the dep migration and returns 401 to mirror "you didn't authenticate"; the dep-layer sites return 403 to mirror "we know you're trying — your token is wrong". SDK clients should branch on `error_code`, not status, to handle both.                                                                                                                                                                                                                                                                                                                                                                                                                                  | `{}`                                                                   |
| `missing_permission`      | 403         | **registry** — `POST /agents/dev/register` (dev-mode disabled in this environment); **tasks** — `require_task_write_auth` JWT path (caller lacks `acn:write` scope)                                                                                                                                                                                                                                                                  | `{ reason: enum, required_permission?: string }`                       |
| `ownership_mismatch`      | 403         | **registry** — `POST /register-protected` (owner-token mismatch); `DELETE /agents/{id}` `POST /claim` `POST /transfer` `POST /release` (`PermissionError` re-raises); **subnets** — `GET /subnets?owner=…` (cross-tenant non-admin), `DELETE /subnets/{id}` (`PermissionError` re-raise); **tasks** — every write endpoint that goes through `task_service` raising `PermissionError` (×10, `replace_all=true` cohort)                | `{ agent_id?: string, subnet_id?: string, task_id?: string, reason?: string } \| { requested_owner: string, token_owner: string }` |
| `not_subnet_member`       | 403         | **subnets** — `GET /subnets/{id}/agents` against a private subnet by a non-owner non-admin caller; **tasks** — `GET /tasks/{id}` against a task whose `subnet_id` is set, by an anonymous (no-auth) or non-member caller                                                                                                                                                                                                              | `{ subnet_id: string, agent_id?: string, task_id?: string, reason?: enum }` |
| `invalid_request`         | 400 / 422   | **registry** — `DELETE /api/v1/agents` (bulk-delete filter required, 400), `POST /agents/{id}/claim` (`ValueError` from claim flow, 400); **subnets** — `POST /subnets` (`ValueError` from create flow, 400); **tasks** — `list_tasks` invalid status enum (400), `match_tasks_for_agent` empty tag list (400), every write endpoint raising `ValueError` (×10, `replace_all=true` cohort, 400); **dependencies** — `assert_system_caller` (`from_agent` outside `system:<slug>` reserved namespace, 422). The 422 (vs 400) for `assert_system_caller` is a deliberate exception — see "details.reason" footnote below.                                                                       | `{ reason: string, field?: string, value?: any, allowed?: list, task_id?: string, agent_id?: string }` |

`details.reason` is a stable per-code enum where the value is a fixed identifier, OR a free-form `str(...)` of the underlying domain exception when the code wraps `ValueError` / `PermissionError`. Reason values currently emitted, by `error_code`:

* `authentication_required` — `"invalid_authorization_header_format"` (registry `GET /me`; **dependencies** `verify_agent_api_key` + `verify_proxy_caller` — same reason value used across both header names because the SDK-actionable failure is identical), `"invalid_api_key"` (registry; **dependencies** `_resolve_agent_by_bearer`; **analytics** `list_activities` Bearer-key-fails-to-resolve branch — same reason value as the dependencies site because the SDK-actionable failure is identical), `"owner_filter_requires_auth"` (subnets), `"private_subnet"` (subnets), `"invalid_agent_api_key"` (tasks), `"owner_or_internal_credential_required"` (**dependencies** `verify_owner_or_internal` no-credential branch), `"auth_required_for_agent_filter"` (**analytics** `list_activities` no-Bearer branch — distinct from `invalid_api_key` so SDK clients can tell "you must authenticate to filter" apart from "your auth failed")
* `missing_permission` — `"dev_mode_disabled"` (registry); tasks emits `details.required_permission` (`"acn:write"`) instead of a reason
* `not_subnet_member` — `"anonymous_caller"` (tasks `get_task` no-auth branch), `"not_member"` (tasks `get_task` non-member branch); subnets uses no reason (the field set already disambiguates)
* `invalid_request` — `"bulk_delete_filter_required"` (registry), `"tag_list_empty"` (tasks `match_tasks_for_agent`), `field="status"` enum-value rejection (tasks `list_tasks`), `"system_namespace_required"` (**dependencies** `assert_system_caller` — emitted as 422 to preserve the pre-migration "request was understood, semantic rule violated" status semantics); free-form `str(ValueError)` (registry claim path, subnets create path, tasks 10× `replace_all=true` cohort)
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

All HTTP-mounting route modules are now migrated: `registry`, `subnets`, `tasks`, `payments`, `follows`, `manifest`, `dependencies`, `analytics`, `onchain`, and the HTTP routes of `websocket` are fully aligned as of sprints #2b, #3-followup, #4-followup, #5, #6, #8, #10, #9, #7, and #11a respectively. See the coexistence matrix below.

The remaining migration target is the **WebSocket protocol endpoint** itself (`WEBSOCKET /ws/{agent_id}`), tracked as sprint #11b. Its error contract is bounded by RFC 6455 close codes, not HTTP responses — close code 4401 + free-text reason today, with no `error_code` / `details` typing — so the migration requires separate RFC / design work distinct from the HTTP schema convergence done in sprints #1-#11a. The WS surface is NOT broken; it speaks a different (older, less typed) contract until #11b lands.

---

## 4. Transitional coexistence matrix

During the migration sprint, ACN-emitted 4xx responses carry **two distinct shapes** depending on which route emitted them. This is the single most important fact for SDK upgrade:


| Route group                                                                                                                    | Exception class                          | 4xx body shape                                                                         | Status           |
| ------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------- | -------------------------------------------------------------------------------------- | ---------------- |
| Pilot — `/communication/send`, `/broadcast`, `/broadcast-by-tag`, `/history`, `/history/{agent_id}/ack`, `/internal/send` | `ACNHTTPError`                           | `{ error_code, message, details, request_id }` (flat)                                  | ✅ Migrated       |
| `/api/v1/agents/{id}/allowlist/...` (POST/DELETE/GET)                                                                     | `ACNHTTPError`                           | `{ error_code, message, details, request_id }` (flat)                                  | ✅ Migrated       |
| `/api/v1/agents/`* (registry) [^2]                                                                                        | `ACNHTTPError` (4xx) + `HTTPException` (5xx) | flat                                                                              | ✅ Aligned (#2a + #2b) |
| `/api/v1/subnets/*` [^3]                                                                                                  | `ACNHTTPError` (4xx) + `HTTPException` (5xx, 1× latent bug)             | flat (except 1 latent-bug site silently rewritten to 500)                              | ✅ Aligned (#3 + #3-followup, modulo latent bug) |
| `/api/v1/tasks/*` [^4]                                                                                                    | `ACNHTTPError` (4xx) + `HTTPException` (5xx)             | flat                                                                                   | ✅ Aligned (#4 + #4-followup) |
| `/api/v1/agents/{id}/follows/*` [^6]                                                                                      | `ACNHTTPError`                           | flat                                                                                   | ✅ Aligned (#6)   |
| `/api/v1/payments/*` [^5]                                                                                                 | `ACNHTTPError` (4xx) + `HTTPException` (5xx) | flat                                                                                   | ✅ Aligned (#5)   |
| `/api/v1/communication/manifest/*` and `/api/v1/communication/content/*` [^8]                                             | `ACNHTTPError`                           | flat                                                                                   | ✅ Aligned (#8)   |
| Auth dependency rejects (every router that mounts an auth dep) [^10]                                                      | `ACNHTTPError` (4xx) + `HTTPException` (1× 503 preserved)                | flat                                                                                   | ✅ Aligned (#10)  |
| `/api/v1/analytics/*` [^9]                                                                                                | `ACNHTTPError`                           | flat                                                                                   | ✅ Aligned (#9)   |
| `/api/v1/onchain/*` [^7]                                                                                                  | `ACNHTTPError` (4xx) + `HTTPException` (2× 503 preserved) | flat                                                                  | ✅ Aligned (#7)   |
| `/api/v1/websocket/*` (HTTP routes) [^11a]                                                                                     | `ACNHTTPError`                           | flat                                                                                   | ✅ Aligned (#11a) |
| `/ws/*` (websocket protocol)                                                                                                   | RFC 6455 close codes (no HTTP body)      | close code 4401 + free-text `reason` (no typed `error_code`)                           | ⏳ Pending (#11b) |
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

[^7]: **Onchain (ERC-8004) full migration (sprint #7).** All 12 4xx raise sites in `acn/routes/onchain.py` are migrated; **2 5xx sites preserved**.

    *Sprint #7 introduces 6 NEW route-local ErrorCodes:*

    * **1× `ERC8004_TOKEN_ID_MISSING`** (422) — `_parse_token_id_or_422` defensive `value is None` branch. *Unreachable from HTTP today* — both reputation and validation routes 404 with `ERC8004_NOT_BOUND` upstream of the helper. Pinned by helper-level unit test (`test_onchain_error_schema.py::test_helper_raises_acn_http_error_on_none`) so a future refactor that drops the upstream guard does not silently regress the flat-schema contract.
    * **1× `ERC8004_TOKEN_ID_CORRUPT`** (422) — same helper, `int()` coercion failure when an agent's stored `erc8004_agent_id` is non-numeric (manually-edited or extremely-old DB row). Reached by the reputation route in practice. `details = {agent_id}` only — the corrupt `stored_value` is logged operator-side but *deliberately not* echoed since it is potentially attacker-controlled DB content.
    * **1× `ERC8004_CHAIN_MISMATCH`** (422) — bind H-erc8004 audit defence. `details = {server_chain, client_chain}`. The H-erc8004 audit ticket prevents a client from claiming "this token lives on Ethereum mainnet" while actually pointing at a cheap-testnet token; pre-migration the route emitted a free-form prose detail string, post-migration it emits both halves of the comparison as typed `details` keys so SDK clients can render a precise diagnostic without parsing prose.
    * **1× `ERC8004_TOKEN_ALREADY_BOUND`** (409) — bind path when `_check_duplicate_token` finds another agent owns the requested `token_id`. `details = {token_id, bound_agent_id, requesting_agent_id}` — `bound_agent_id` IS *intentionally* echoed back since it is publicly resolvable on-chain via `ownerOf(token_id)`; hiding it would force SDK clients to round-trip through the chain for data the route already knows. Note: re-binding the same token to the *same* agent is idempotent (the duplicate check explicitly compares against `requesting_agent_id`); only cross-agent collisions surface this code.
    * **1× `ERC8004_REGISTRATION_MISMATCH`** (422) — bind path when `erc8004.verify_registration` returns `False` (on-chain `tokenURI` does not match ACN's expected agent-registration URL). `details = {token_id, expected_url}` — `expected_url` preserved verbatim so the caller can set it as the on-chain `tokenURI` without reconstructing it from `gateway_base_url` + `agent_id`.
    * **2× `ERC8004_NOT_BOUND`** (404) — `get_agent_reputation` and `get_agent_validation` when the agent exists but has no `erc8004_agent_id` yet. Same `{agent_id}` shape at both sites. Distinct from `ERC8004_TOKEN_ID_MISSING` (also 404→422 in spirit) because *this* code is raised at the *route entry point* before the parse-helper is reached, and SDK clients want to render the friendlier "this agent has not bound an on-chain identity yet" message rather than the operator-tier diagnostic.

    *Reused cross-module / pilot codes (5 sites):*

    * **4× `AGENT_NOT_FOUND`** (404) — every route's `except AgentNotFoundException` branch (bind, get_identity, reputation, validation). Uniform `{agent_id}` shape, byte-identical to the existing pilot/payments/follows usage of this code.
    * **1× `API_KEY_AGENT_MISMATCH`** (403) — bind path-key gate, strict `{path_agent, key_agent}` shape shared with sprints #1 / #5 / #6 / #10.

    **2 5xx sites preserved** (declined to migrate):

    * `bind_onchain_identity` 503 when `erc8004.verify_chain_id` reports a different chain_id than `settings.erc8004_chain_id` — operator-side misconfig (or RPC-swap attack scenario where the operator unknowingly points at the wrong network); fail-closed because the client cannot fix it by retrying. `ACNHTTPError` rejects 5xx at construction time so the central 5xx sanitisation handler stays in charge here.
    * `get_agent_validation` 503 when `ERC8004_VALIDATION_CONTRACT` is not configured — same shape (the Validation Registry is still experimental and the contract addresses are not yet publicly published in some environments).

    **12 contract tests** in `tests/routes/test_onchain_error_schema.py` pin every 4xx raise site (1:1 coverage; 11 HTTP-level + 1 helper-level unit test for the unreachable `ERC8004_TOKEN_ID_MISSING` branch). The 4 `AGENT_NOT_FOUND` sites are pinned per-route rather than collapsed into a single parametrised test — a refactor that swaps one route to a different code (e.g. promotes 404 to 410 for one route) would silently diverge from the others without per-site coverage.

    **Two pre-existing test files needed regression fixes.** `test_onchain_chain_id_h_erc8004.py::test_mismatching_chain_rejected_with_422` and `test_onchain_token_id_corruption.py::test_corrupt_token_id_returns_422_not_500` both asserted on the legacy `r.json()["detail"]` prose, which the new flat schema lacks. The fixes re-assert on `error_code` and the strict `details` shape, preserving each test's original audit-defence intent (chain mismatch caught + corrupt token id surfaces as 422-not-500) while modernising the wire shape.

[^8]: **Manifest full migration (sprint #8).** All 2 4xx raise sites in `acn/routes/manifest.py` are migrated to two new ErrorCode members:

    * **1× `MANIFEST_ENTRY_NOT_FOUND`** (404) — `delete_manifest_entry` when `manifest_service.delete` returns `False` (entry missing or owned by a different agent — same surface, see existence-leak invariant in §2). `details={"agent_id": <path>, "mid": <path>}`.
    * **1× `MANIFEST_CONTENT_NOT_FOUND`** (404) — `fetch_manifest_content` when `manifest_service.fetch_content` returns `None` (content expired, missing, or belonging to a different owner). `details={"owner_id": <key-derived>, "mid": <path>}` — `owner_id` is the API-key-resolved caller (NOT a path parameter), so it always reflects who is *asking*, never the real owner of the mid. The `owner_id` field name (vs `agent_id` used by the entry-route) is a deliberate divergence — see §2 manifest subsection for rationale.

    `manifest.py` has **0** 5xx catch-all sites — both raise sites are simple service-return guards with no surrounding `except Exception:` blocks. The cross-module catch-all defence (P3) does not apply.

    **Cross-tenant probe shape contract.** The body shape (especially `error_code` and `details`) must be **identical** between a legitimate own-resource miss and a cross-tenant probe. A divergent shape would reintroduce the existence leak the 404-not-403 design exists to prevent. For `MANIFEST_CONTENT_NOT_FOUND`, `details.owner_id` therefore *always* reflects the *probing* caller's agent ID, never the real owner of the mid (which the caller has no right to know). `tests/routes/test_manifest_error_schema.py::test_cross_tenant_miss_emits_same_shape_as_legit_miss` pins this — if it ever flips to `details.owner_id == "<real owner>"`, the route has started leaking ownership info and that's the regression it exists to catch.

    3 contract tests in `tests/routes/test_manifest_error_schema.py` pin both raise sites: 1 entry test (DELETE on missing/cross-tenant mid) + 2 content tests (legit own-resource miss + cross-tenant probe).

    Sprint #8 closes the long-standing heterogeneity in the `/api/v1/communication/*` namespace (see BACKLOG note): SDK type-gen against `/openapi.json` now emits `ACNErrorResponse` for *every* endpoint under that prefix, not just `/send` and `/broadcast`.

[^9]: **Analytics full migration (sprint #9).** All 3 4xx raise sites in `acn/routes/analytics.py` (all confined to `list_activities`) are migrated to reused cross-module catalog codes — no new ErrorCodes added:

    * **2× `AUTHENTICATION_REQUIRED`** (401) — `list_activities` filter-with-no-Bearer (`details.reason="auth_required_for_agent_filter"`, *new reason value* in this sprint) and Bearer-key-fails-to-resolve (`reason="invalid_api_key"`, byte-identical to dependencies sprint #10).
    * **1× `API_KEY_AGENT_MISMATCH`** (403) — cross-tenant filter probe via `?agent_id=…` or `?agent_ids=…`. Strict `{path_agent, key_agent}` shape; for multi-id `agent_ids` filters the route surfaces only the *first sorted* mismatched id in `details.path_agent` so the response stays in the strict-schema bucket of `tests/test_error_code_details_consistency.py`. See §2 "Multi-id `agent_ids` filter — strict-schema invariant" for the rationale.

    `analytics.py` has **0 5xx catch-all sites** — the cross-module catch-all defence (P3) does not apply to this module. The other six analytics endpoints have no file-local 4xx sites; auth on those routes is delegated to `InternalTokenDep` (already migrated in sprint #10) or no-auth (rate-limited only).

    **5 contract tests** in `tests/routes/test_analytics_error_schema.py` pin every raise site (with a 2:1:2 split): 2 for `auth_required_for_agent_filter` covering the missing-header / non-Bearer-prefix branches, 1 for `invalid_api_key`, and 2 for `api_key_agent_mismatch` covering single-id (`agent_id`) and multi-id (`agent_ids`) filter forms. Test wiring uses `monkeypatch.setattr` on `acn.routes.analytics.get_agent_service` because the route fetches the agent service via a *module-level* call rather than as a FastAPI parameter dependency — `app.dependency_overrides` does not intercept that pattern. See §2's "Test-wiring note" for the propagation guidance to sprint #11.

[^10]: **Auth dependency full migration (sprint #10).** All 8 4xx raise sites in `acn/routes/dependencies.py` are migrated to reused cross-module catalog codes — no new ErrorCodes needed:

    * **4× `AUTHENTICATION_REQUIRED`** (401) — `_resolve_agent_by_bearer` (`details.reason="invalid_api_key"`), `verify_agent_api_key` (`reason="invalid_authorization_header_format"`), `verify_proxy_caller` (same reason value as the agent-API path; the SDK-actionable failure is identical regardless of which header was malformed), `verify_owner_or_internal` no-credential branch (`reason="owner_or_internal_credential_required"`).

    * **2× `INTERNAL_TOKEN_INVALID`** (403) — `verify_internal_token` and `verify_owner_or_internal` priority branch. Both emit empty `details = {}` deliberately: a wrong internal token is operator-side misconfig, not caller-actionable, so leaking the wrong token via response-body logs would only widen the blast radius. Status-code split with the registry sprint #2b site (which uses 401) is intentional — see the cross-module table note.

    * **1× `API_KEY_AGENT_MISMATCH`** (403) — `verify_owner_or_internal` Bearer-key-resolves-to-different-agent path. Reuses the canonical `{path_agent, key_agent}` shape established in sprint #1; `error_code` discriminates this site from the four AUTHENTICATION_REQUIRED ones above without status-code overlap (401 vs 403).

    * **1× `INVALID_REQUEST`** (422) — `assert_system_caller` rejects `from_agent` outside the reserved `system:<slug>` namespace. `details = {field, reason="system_namespace_required", value}`. Emitted as 422 (not 400) to preserve the pre-migration "request was understood, semantic rule violated" status semantics — SDK clients expecting 422 for the legacy emission don't see a status-code regression.

    **1 5xx site preserved.** `get_allowlist_service` raises `HTTPException(503)` with `headers={"Retry-After": "300"}` when the deployment is missing PostgreSQL configuration. Migration to `ACNHTTPError` was declined for two reasons: (1) `ACNHTTPError` rejects 5xx at construction time so the central 5xx sanitisation handler stays in charge; (2) the 503-with-Retry-After contract is load-bearing — clients use it to tell "feature configured-disabled, surface to operator" apart from "transient failure, retry blindly". The 503 already goes through `_http_exception_handler` and is sanitised correctly today.

    **8 contract tests** in `tests/routes/test_dependencies_error_schema.py` pin every 4xx raise site (1:1 coverage), each driven through a representative migrated route (`POST /communication/send` for AgentApiKeyDep, `POST /agents/{id}` proxy for ProxyCallerDep, `GET /payments/tasks/{id}` for InternalTokenDep, `GET /communication/manifest/{id}` for OwnerOrInternalDep, `POST /communication/internal/send` for assert_system_caller). The test file documents *why* each route was chosen so a future refactor that retires one of those endpoints can swap to an equivalent without losing coverage.

    **Sprint #10 unblocks a long-standing pilot-era footnote** that has been carried since #11 landed. That footnote warned SDK clients to keep both parsers in scope when calling migrated routes because *auth-dep* 4xx still emitted the legacy shape — the warning has been deleted as of this sprint, and the matrix row "Auth dependency rejects" in §4 is now ✅ Aligned.

[^11a]: **Websocket HTTP routes full migration (sprint #11a).** The `acn/routes/websocket.py` router exposes 3 endpoints; only the HTTP-mounted pair is in scope for this sprint. `WEBSOCKET /ws/{agent_id}` is tracked separately as #11b — its error contract is RFC 6455 close codes, not HTTP responses, and requires its own RFC.

    **1× `API_KEY_AGENT_MISMATCH`** (403) — `get_agent_websocket_status` enforces "agent may only query *its own* connection status" on `GET /api/v1/websocket/agent/{agent_id}/status`. Reuses the canonical `{path_agent, key_agent}` strict shape established in sprint #1, identical to emitters in registry / payments / follows / onchain / analytics. No new ErrorCode.

    **0 new ErrorCodes.** The migration is a pure cross-module-catalog reuse. The cross-sprint AST consistency test (`tests/test_error_code_details_consistency.py`) continues to enforce the strict `{path_agent, key_agent}` invariant across all 16 emitters.

    **0 5xx sites preserved** — neither HTTP endpoint wraps its body in `try`/`except Exception`. The catch-all defence (P3) does not apply.

    **Documentation drift correction.** Sprint #7 had inadvertently emptied `tests/test_openapi_acn_error_response.py::TestNonMigratedRoutersDoNotAdvertiseDefault.NON_MIGRATED_ENDPOINTS` under the (incorrect) belief that the `websocket` router owned no HTTP routes. #11a fixes the docstring on that class, adds `responses=ACN_DEFAULT_RESPONSES` to the router so OpenAPI advertises the flat schema for both HTTP routes via FastAPI's uniform router-level merge, and adds the `status` endpoint to `REPRESENTATIVE_ENDPOINTS` so the spec coverage stays pinned. The companion `connections` endpoint inherits the same router-level block via FastAPI's uniform merge so its OpenAPI spec is correct without a separate sample.

    **1 contract test** in `tests/routes/test_websocket_error_schema.py` pins the single raise site (1:1 coverage). The test additionally asserts the WS manager's `is_user_connected` is NOT called on the mismatch path — a regression that swapped check order would let strangers probe whether arbitrary agents are online via response-time differential.

### SDK parsing template

A defensively-written SDK should detect the new shape by presence of `error_code`:

```python
def parse_acn_error(response_body: dict) -> AcnError:
    if "error_code" in response_body:
        # Migrated routes + all auth-dep rejects (post-sprint #10)
        # + all 5xx — flat schema.
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

Per-sprint progress, code-level breakdown, and the suggested
ordering live in **[`docs/BACKLOG.md`](../BACKLOG.md) → "Phase 2
review v2 P1 #11 — Error schema migration sprint"**, which is the
single source of truth (this section is intentionally short — a
duplicate roadmap has caused stale-doc drift in the past, e.g. a
sprint #5 update that hit BACKLOG but missed this file).

This document focuses on the **public contract** SDK clients
depend on (sections 1-6, 8); BACKLOG focuses on the **internal
migration process**. The two are linked but separate concerns.

### Migration exit criteria

The migration is complete and ACN can declare full schema
unification when **all** of the following hold (all observable
from this document, BACKLOG, and `/openapi.json`):

- Every row in Section 4's coexistence matrix is ✅.
- Every footnote `[^N]` (N = sprint index) is published with
  per-site enumeration and contract notes.
- The 30-day 5xx `error` field deprecation window has expired
  (target: **2026-06-01** — see BACKLOG "5xx field deprecation
  ticket"); the field is removed from `_http_exception_handler`
  and `_unhandled_exception_handler`.
- The SDK parsing template's `if "error_code" in body` fallback
  branch is removed (currently load-bearing during transition;
  drops out once Section 4 is uniformly ✅).

Until then, SDK 0.5.0 must accept both shapes — see the parsing
template in Section 4.

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

