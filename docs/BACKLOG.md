# BACKLOG

低优先级改进清单。非紧急，但值得做。新条目追加到对应分区末尾，做完了直接删掉或打 `[done]`。

---

## Communication

### Inbox refactor follow-ups

Context: commits `8c540a9` / `bc5b331` / `c71da67` 把消息存储从 "per-agent archive" 改为 "offline inbox"。以下是当时识别但未做的延伸优化。

- ~~`**_store_inbox` 合并 pipeline**~~ ✅ 已完成（P2-A）
`zadd` + `zremrangebyrank` + `expire` 三次 round-trip → 单个 `pipeline(transaction=False)` 批次。`_INBOX_CAP` / `_INBOX_TTL` 提升为模块常量。测试 fixture 同步更新（pipeline 命令是同步调用，用 `assert_called_with` 而非 `assert_awaited_with`）。
- ~~`**route()` 前置 `is_online()` 预检**~~
**已修**：`route()` 在 `get_agent()` 返回的 `agent_info.status` 非 `"online"` 时立即 short-circuit：写 inbox、返回 `{"status": "inbox", "route_id": ...}`，不打开 HTTP 连接、不写 DLQ。零额外 Redis round-trip（status 已随 `get_agent()` 一并读取）。心跳 TTL 延迟导致误判的场景见代码注释。`retry_dlq` 自动受益（调用 `route()`）。测试：`tests/infrastructure/test_message_router_inbox.py::TestOfflinePrecheck`（4 tests）。
- ~~**按 `route_id` 精准 ack**~~ ✅ 已完成（P2-B）
新增 `POST /history/{agent_id}/ack` endpoint，body `{"route_ids": [...]}` 精确 `zrem` 指定消息。`MessageRouter.ack_inbox()` 实现：`ZRANGE 0 -1`（上限 50）→ Python 过滤 → pipeline `ZREM`，2 次 round-trip。`?ack=true` 全清语义保留，向后兼容。Layer-1 smoke tests 新增两个方法存在性守卫。

### ~~Legacy key cleanup~~ ✅ 已完成

- ~~**清理 `acn:messages:agent:` 遗留 key**~~
- ~~**清理 `acn:messages:log:{route_id}` 遗留 key**~~

`acn/scripts/cleanup_legacy_message_keys.py`：dry-run 默认，`--execute` 真删。SCAN + UNLINK 批量处理两类 key；`acn:messages:log:stream` 在 PRESERVE 集合里，不会被误删。

### ~~Phase 2 PR #2（allowlist + A2A `from_agent`）follow-ups~~ ✅ 已全部完成

Context: commits `39c0ed0`（PR #2 v1+v2+v3）+ `d928a06`（ruff sweep）+ `<P2-sweep commit>`（6 项 P2 一并消化）。3 轮 audit 后所有 P0/P1 已修；本地 PG 14.19 上 6/6 staging verify 通过；P2 sweep 后全测试 894 通过、单测层加了 TOCTOU race 回归。

- ~~**A2A middleware 路径用 `endswith("/jsonrpc")` 而非精确匹配**（v2 P2-A1）~~ ✅
改为 `path not in _ENFORCED_PATHS`（frozenset 精确匹配）。`endswith` 路径风险（未来 SDK 加 `/api/v1/jsonrpc` 被无声扩张拦截）封堵。
- ~~`**_forward.replay()` 第二次调用返回 `http.disconnect` 偏激进**（v2 P2-A2）~~ ✅
`_forward(scope, receive, send, body)` 增加 `receive` 参数；`replay()` 第二次调用 `await receive()` 委托原 ASGI receive，`http.disconnect` 自然由上游服务器在客户端真断时下发，符合 ASGI 规范。
- ~~`**auth_middleware.py` 用 `logging.getLogger` 而非 `structlog`**（v2 P2-A3）~~ ✅
切到 `structlog.get_logger(__name__)`；两处 `logger.warning(..., extra={...})` 改成 kwargs 形式（`logger.warning("event", k=v)`），结构化字段不再被埋进 stdlib `LogRecord.__dict__["extra"]`。
- ~~**单元测试侧未验证 TOCTOU race**（v2 P2-A5）~~ ✅
`tests/services/test_allowlist_service.py::test_add_capacity_trigger_propagates_under_concurrency` — `asyncio.gather` 5 路并发 `add`，mock pg_repo 模拟 trigger 行为（首条 INSERT 成功，余 4 条抛 `AllowlistCapacityExceededError`）。Pin 三个不变量：异常透传、Redis 不污染、聚合结果确定性（1 winner + 4 capacity error）。
- ~~**Trigger SQL `cap=500` 与 Python `MAX_ALLOWLIST_SIZE=500` 双源真值**（v3 P2-A6）~~ ✅
`acn/api.py::_verify_allowlist_cap_alignment` 在 lifespan 内（仅当 PG 启用且 `AllowlistService` 已 wired）跑一次 `pg_get_functiondef('enforce_agent_allowlist_capacity'::regproc)`，正则提取 `cap CONSTANT integer := <N>`，与 Python `MAX_ALLOWLIST_SIZE` 比对：一致 → `info` log、不一致 → `warning` log（含 `sql_cap` / `python_cap` / `advice`）、查不到函数（旧 alembic head）→ `info skipped`、introspection 失败 → `warning failed`。完全 non-fatal，trigger 自身仍是 canonical guard。
- ~~`**_http_exception_handler` 的 `Retry-After` lookup 大小写敏感**（v3 P2-A7）~~ ✅
`next((v for k, v in exc_headers.items() if k.lower() == "retry-after"), None)`。HTTP header 名 case-insensitive（RFC 9110 §5.1）的语义对齐。

### ~~Phase 3 Module B `attention_fee` — TTL refund worker~~ ✅ Done (2026-05-05)

Context: 2026-05-05 commit landed the attention_fee `lock + ack-release`
flow (see `[acn-communication-economic-model.md](features/acn-communication-economic-model.md)`).
The worker (`acn/services/manifest_ttl_refund_worker.py`) was shipped in
the same phase and is wired into `api.py` lifespan as a background asyncio
task running every 5 minutes.

Implementation summary:

- `ManifestService.write` now persists `expires_at_ms` into the HASH detail
key; `_decode_entry` reads it back into `ManifestEntry.expires_at_ms`.
- `run_once(redis_client, escrow_provider)` scans `acn:manifest:{*}:*` HASHes
via cursor SCAN, filters for entries where:
  - `extra.attention_fee.escrow_id` is set (has fee)
  - `acked_at` is absent (not yet acknowledged)
  - `expires_at_ms` is past `now − REFUND_GRACE_SECONDS` (5 min grace)
  - `extra.attention_fee.refunded_at` is absent (not already refunded)
- Calls `escrow_provider.refund_v2(escrow_id, reason="attention_fee_ttl_expired")`.
- On success, stamps `extra.attention_fee.refunded_at` in Redis (ACN-side
idempotency guard — backend also rejects double-refund on its own state).
- 18 unit tests covering happy path, all skip conditions, error handling, dry-run.
- `api.py` lifespan starts the worker only when `escrow_client_instance` is
available; first run delayed 5 min to avoid colliding with deploy restarts.

Legacy rows (pre-Phase 3, `expires_at` absent) are silently skipped.

### Phase 3 Module B `attention_fee` — ack crash-recovery race (P2 v1 limitation)

The ack path stamps `acked_at_ms` (HSETNX in
`ManifestService.mark_acked`) BEFORE calling Backend's
`release_partial`. The HSETNX guard is the right choice for the
common case (it bounds local replay so we never double-release), but
introduces a known crash-recovery race:

1. ACN stamps `acked_at_ms` successfully → HSETNX returns 1.
2. ACN process exits / crashes / loses network before issuing
  `release_partial` (or the request is in-flight when a deploy
   restarts the pod).
3. SDK retries the ack call → `mark_acked` returns 0 → ACN responds
  with 4xx `ATTENTION_FEE_ALREADY_ACKED`.
4. Backend escrow is still LOCKED. Funds are stuck until the TTL
  refund worker (above) or manual ops intervention reclaims them.

Mitigation options for v2 of this feature:

- **Release-first ordering**: call `release_partial` BEFORE stamping,
treat backend's "escrow already RELEASED" 4xx as success on
retry, and only then HSETNX. Requires backend to make
`release_partial` idempotent on a same-recipient retry, which
it currently isn't (it returns a generic 4xx that we'd have to
string-match). The robust fix lives in
`EscrowService.release_partial` accepting an `idempotency_key`
the SDK can echo.
- **Two-phase commit via a "release in flight" marker**: write a
separate `release_in_progress=1` flag before the backend call,
flip it to `acked_at_ms` only after a 200. Crash-recovery scan
re-attempts releases for entries with the flag set. Heavier
but doesn't depend on backend changes.

Both options are deferred to the same release window as the TTL
refund worker — a single Phase 3 Module B v2 PR will own the
escrow-side idempotency story end-to-end.

### Phase 2 Group C #9 — wire-level behaviour change announcement

Context: commit `9fb38b9` collapsed `MessageService.broadcast_message` →
`BroadcastService.broadcast`. One **wire-level behaviour change** that's
strictly an improvement but worth telling SDK / dashboard owners about
before they notice it via a silent metric shift:

- **Before**：HTTP `/communication/broadcast` with `strategy="parallel"` (or `"sequential"`) — first non-`PolicyRejected` delivery error (`ConnectionError` / 5xx / timeout) **raised out of the service** → HTTP 500 + remaining targets in the fan-out **never contacted**. The old `for` loop aborted on the first exception.
- **After**：`BroadcastService._send_parallel` runs all targets via `asyncio.gather` and converts per-target exceptions to `results[agent_id] = {"error": <safe>}`. **Never raises**. Result: HTTP 200 with `responses[]` showing per-target `status: "failed"` for the broken ones and `status: "success"` for the rest.

Why this is an improvement, not a regression:

- All targets are reached (no partial fan-out)
- Per-target outcome is observable (clients can decide what to do per-recipient)
- Aligns HTTP path with the A2A path's existing semantics (which already used `BroadcastService`)
- Matches the `best_effort` strategy's spirit — and `best_effort` was the only strategy the deleted code actually distinguished

Action items (none blocking, but worth doing before next release):

- ~~**Strategy case-insensitivity**~~ ✅ 已完成（同 P2-2 sweep）
Route now `.lower()`-normalises before `BroadcastStrategy(...)`. SDKs sending uppercase no longer 422.
- **Dashboard owners**: any alert that fires on `acn_broadcast_sent{status="error"}` or HTTP 5xx rate from `/broadcast` will see lower-than-before fire rate. The signal moved into `responses[].status == "failed"` (new) and `acn_messages_rejected_by_policy_total{path="broadcast_target"}` (already existed for policy denials).
- **SDK release notes**: mention the 500→200 shift; recommend clients inspect `responses[].status` rather than HTTP status to detect per-target failures.
- **OpenAPI / docs**: `/broadcast` response schema now includes top-level `broadcast_id: str` (12-hex). `/broadcast-by-tag` same.

### Phase 2 review v2 P1 #10 — `X-ACN-SDK-Min-Version` warning header (✅ shipped)

`PATCH /api/v1/agents/{id}/policy` now emits the `X-ACN-SDK-Min-Version`
response header whenever the resolved mode is `manifest` or
`allowlist`. Default value `0.5.0` is configurable via
`Settings.policy_manifest_min_sdk_version` (env: `POLICY_MANIFEST_MIN_SDK_VERSION`).

Action items for surrounding tooling (none blocking; signal what to
do BEFORE Phase 3 default-mode flip in
`[acn-communication-economic-model.md](features/acn-communication-economic-model.md)`):

- **SDK release notes**: clearly call out the contract — any client
pinned below `policy_manifest_min_sdk_version` that PATCHes its
policy to `manifest` / `allowlist` will silently miss every
inbound message until upgraded. Recommend clients READ the header
and surface a fail-fast error / log warning.
  - **(P1 #11 follow-up)** Same SDK 0.5.0 release window also adds
  the `X-ACN-SDK-Min-Version` header — see `[docs/features/acn-error-schema.md](features/acn-error-schema.md)`
  section 4 (transitional coexistence matrix). Pilot routes
  (`/communication/*`) emit a flat `{error_code, message, details, request_id}` body; non-pilot routes still emit the legacy
  `{"detail": "..."}` shape until the migration sprint flips them.
  SDK 0.5.0 must accept BOTH shapes during the transition (the
  `if "error_code" in body` parsing template in section 4.5 is
  the recommended detection branch).
- **Dashboards / ops scripts**: capture the header for any policy
PATCH; alert if a fleet of agents is on a client version below
the advertised minimum AFTER a `manifest` / `allowlist` flip.
- **Versioning policy**: bump
`Settings.policy_manifest_min_sdk_version` each time the ACN
python client adds a contractually-required handler (currently
needs both `manifest_notification` and `policy_changed`). Keep
this ≤ the lowest published client version that implements both.

### Phase 2 review v2 P1 #11 — Error schema migration sprint (✅ pilot shipped)

Pilot scope: communication routes (`/api/v1/communication/*`) — 14
4xx sites migrated from `HTTPException` to `ACNHTTPError`, 4xx + 5xx
share the flat `{error_code, message, details, request_id}` schema.
Spec: `[docs/features/acn-error-schema.md](features/acn-error-schema.md)`.
Catalog & helper: `[acn/core/errors.py](../acn/core/errors.py)`.
Central handler: `[acn/api.py](../acn/api.py)` `_acn_http_error_handler`.

#### Sprint roadmap — non-pilot routes

Each row flips ⏳ → ✅ in section 4 of `acn-error-schema.md` as the
PR lands. Suggested ordering (cheapest / most impactful first):


| #          | Route module                           | New / reused codes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | Status     |
| ---------- | -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| 1          | `allowlist`                            | reuse `ALLOWLIST_CAPACITY_EXCEEDED` / `SELF_ALLOWLIST_FORBIDDEN` (already in catalog)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | ✅          |
| 2a         | `registry` (partial — safe migration)  | reuse `AGENT_NOT_FOUND` (×17), `API_KEY_AGENT_MISMATCH`, `SUBNET_NOT_FOUND` (×2 — promoted from reserved), `COMMUNICATION_REJECTED` (also flattens proxy nested-detail)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | ✅          |
| 2b         | `registry` (auth/ownership/validation) | reuse cross-module catalog: `AUTHENTICATION_REQUIRED` (×2), `INTERNAL_TOKEN_INVALID` (×1), `MISSING_PERMISSION` (×1 dev-mode), `OWNERSHIP_MISMATCH` (×4 — owner-token mismatch + 3 PermissionError), `INVALID_REQUEST` (×2 — bulk-delete safety + ValueError claim path), `AGENT_NOT_FOUND` (×1 — missed `PATCH /agents/{id}/social-card-url` site)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | ✅          |
| 2c         | `registry` (registration policy)       | folded into row #2b — `DEV_MODE_DISABLED` → `MISSING_PERMISSION` with `details.reason="dev_mode_disabled"`, `BULK_DELETE_FILTER_REQUIRED` → `INVALID_REQUEST` with `details.reason="bulk_delete_filter_required"`. No separate ErrorCode added (cross-module RFC consolidated).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | ✅          |
| 3          | `subnets` (partial — safe migration)   | reuse `SUBNET_NOT_FOUND` (×7), `AGENT_NOT_FOUND` (×3), `API_KEY_AGENT_MISMATCH` (×3)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | ✅          |
| 3-followup | `subnets` (auth/permission/validation) | reuse cross-module catalog: 2× `AUTHENTICATION_REQUIRED` (owner-filter + private-subnet view), 1× `OWNERSHIP_MISMATCH` (cross-tenant list_subnets), 1× `OWNERSHIP_MISMATCH` (delete_subnet `PermissionError`), 1× `NOT_SUBNET_MEMBER` (private-subnet member list cross-tenant), 1× `INVALID_REQUEST` (`create_subnet` `ValueError`). Also added local `except ACNHTTPError: raise` defence layer to `list_subnets` catch-all. Latent bug at L398 (`else: raise HTTPException(404)` swallowed by `except Exception`) deferred to cross-module catch-all defence P3 ticket.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | ✅          |
| 4          | `tasks` (partial — safe migration)     | reuse `TASK_NOT_FOUND` (×13 — every `except TaskNotFoundException` site, 3 different auth surfaces)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | ✅          |
| 4-followup | `tasks` (auth/permission/validation)   | reuse cross-module catalog: 10× `OWNERSHIP_MISMATCH` (`PermissionError` sites, `replace_all=true` cohort), 10× `INVALID_REQUEST` (`ValueError` sites, paired with PermissionError cohort), 1× `AUTHENTICATION_REQUIRED` (`require_task_write_auth` agent-key 401), 1× `MISSING_PERMISSION` (`require_task_write_auth` JWT 403), 2× `INVALID_REQUEST` (`list_tasks` invalid status + `match_tasks_for_agent` empty tags), 2× `NOT_SUBNET_MEMBER` (`get_task` private-subnet gate, `reason` distinguishes anonymous vs non-member).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | ✅          |
| 5          | `payments`                             | new: `PAYMENT_CAPABILITY_NOT_FOUND` / `PAYMENT_TASK_NOT_FOUND` / `TOKEN_PRICING_NOT_CONFIGURED` (×3 sites) / `BILLING_TRANSACTION_NOT_FOUND`. reuse `AGENT_NOT_FOUND` (×2 — `AgentNotFoundException` catch + `registry.get_agent`-returns-None path), `API_KEY_AGENT_MISMATCH` (×4 — `set_payment_capability`, `get_agent_payment_tasks`, `get_agent_payment_stats`, `set_token_pricing` path-mismatch sites), `FROM_AGENT_MISMATCH` (×1 — `create_payment_task` body-field mismatch). 13 4xx sites total; 3 catch-all 5xx sites stay `HTTPException(500)` with `except ACNHTTPError: raise` + `except HTTPException: raise` defence (sanitised by central handler). 13 contract tests pin every raise site (1:1 coverage). **Note**: pre-sprint roadmap row read "reuse `INSUFFICIENT_BALANCE`" — that was wrong. `INSUFFICIENT_BALANCE` stays reserved (declared, not raised) until the wallet / billing subsystem genuinely surfaces "insufficient balance" at the route layer; the current `payments.py` only surfaces resource-existence failures (capability / task / pricing / transaction not found), not balance failures.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | ✅          |
| 6          | `follows`                              | new: `FOLLOW_LIMIT_EXCEEDED` / `SELF_FOLLOW_FORBIDDEN`. reuse `AGENT_NOT_FOUND` (×1 — followee lookup miss in `follow_agent`), `API_KEY_AGENT_MISMATCH` (×2 — `follow_agent` POST + `unfollow_agent` DELETE path-mismatch gates). 5 4xx sites total; **0 catch-all 5xx sites** in this router (no `except Exception:` in `follow_agent` / `unfollow_agent` — the three caught exceptions are domain-specific: `SelfFollowError` / `AgentNotFoundException` / `FollowLimitExceededError`). 5 contract tests pin every raise site (1:1 coverage). `details.follower_id` / `details.max_follows` chosen over allowlist's `owner_id` / `max_size` because follow has no ownership semantics — service-layer exception names and `acn-follow-proposal.md` response bodies all use `follower`; semantically parallel to sprint #1, field-name divergent on purpose.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | ✅          |
| 7          | `onchain`                              | new: `ERC8004_TOKEN_ID_MISSING` (×1 — `_parse_token_id_or_422` defensive None branch, `details = {agent_id}`; HTTP-unreachable today because both reputation/validation routes 404 with `ERC8004_NOT_BOUND` upstream — pinned by helper-level unit test), `ERC8004_TOKEN_ID_CORRUPT` (×1 — same helper, `int()` coercion failure; `details = {agent_id}` only — corrupt `stored_value` is logged operator-side but *deliberately not* echoed since it is potentially attacker-controlled DB content), `ERC8004_CHAIN_MISMATCH` (×1 — bind H-erc8004 audit defence, `details = {server_chain, client_chain}`), `ERC8004_TOKEN_ALREADY_BOUND` (×1 — bind 409 when `_check_duplicate_token` finds another agent owns the token; `details = {token_id, bound_agent_id, requesting_agent_id}` — `bound_agent_id` *intentionally* echoed back since it is publicly resolvable on-chain via `ownerOf(token_id)`, hiding it would force SDK clients to round-trip through the chain for data the route already knows), `ERC8004_REGISTRATION_MISMATCH` (×1 — bind 422 when on-chain `tokenURI` ≠ expected agent-registration URL; `details = {token_id, expected_url}` — `expected_url` preserved verbatim so caller can set it as on-chain `tokenURI` without reconstructing it from gateway_base_url + agent_id), `ERC8004_NOT_BOUND` (×2 — reputation + validation routes when agent has no `erc8004_agent_id`; same `{agent_id}` shape at both sites, distinct from `ERC8004_TOKEN_ID_MISSING` because *this* code is raised at route entry while the latter is the parse-helper's defensive fallback). reuse `AGENT_NOT_FOUND` (×4 — every route's `except AgentNotFoundException` branch: bind, get_identity, reputation, validation), `API_KEY_AGENT_MISMATCH` (×1 — bind path-key mismatch, strict `{path_agent, key_agent}` shape). 12 4xx sites total; **2 5xx sites preserved** (`bind_onchain_identity` 503 when RPC chain_id disagrees with configured chain — operator misconfig / RPC swap attack, fail-closed; `get_agent_validation` 503 when Validation Registry contract address not configured — `ACNHTTPError` rejects 5xx at construction time so the central 5xx sanitisation handler stays in charge for both). 12 contract tests pin every raise site (1:1 coverage; the `ERC8004_TOKEN_ID_MISSING` test is a helper-level unit test rather than HTTP-level because the branch is unreachable from HTTP today). Two existing test files (`test_onchain_chain_id_h_erc8004.py`, `test_onchain_token_id_corruption.py`) needed regression fixes — they asserted on the legacy `r.json()["detail"]` field which the new flat schema lacks. | ✅          |
| 8          | `manifest`                             | new: `MANIFEST_ENTRY_NOT_FOUND` (×1 — `delete_manifest_entry` miss/cross-tenant probe; `details = {agent_id, mid}` keyed by path), `MANIFEST_CONTENT_NOT_FOUND` (×1 — `fetch_manifest_content` miss/cross-tenant probe; `details = {owner_id, mid}` keyed by API-key-derived owner, NOT `agent_id` because this route has no path `agent_id`). 2 4xx sites total; **0 catch-all 5xx sites** in this router (the two raise sites are simple "service returned None/False" guards, no `except Exception:` blocks). Two distinct ErrorCodes (vs a single `MANIFEST_NOT_FOUND` with `details.kind`) chosen so SDK clients branch on `error_code` without inspecting `details`, and so the cross-sprint consistency test gives strict (not union-schema) protection on each code's `details` shape. Cross-tenant probes intentionally surface the *exact* same `error_code`/`details` shape as legitimate misses — see §4 footnote `[^8]` and `test_manifest_error_schema.py::test_cross_tenant_miss_emits_same_shape_as_legit_miss` for the existence-leak invariant. 3 contract tests pin both raise sites (1 entry test + 2 content tests covering legit-miss / cross-tenant).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | ✅          |
| 9          | `analytics`                            | reuse cross-module catalog only — no new ErrorCodes. `AUTHENTICATION_REQUIRED` (×2 — `list_activities` `auth_required_for_agent_filter` when an `agent_id`/`agent_ids` filter is requested without a Bearer token + `invalid_api_key` when the Bearer key fails to resolve), `API_KEY_AGENT_MISMATCH` (×1 — `list_activities` cross-tenant filter probe; `details = {path_agent, key_agent}` strict shape, surfaces only the *first sorted* mismatched id when the caller passes a multi-id `agent_ids` filter so the body stays in the strict schema bucket — see `test_analytics_error_schema.py::test_multi_agent_ids_mismatch_surfaces_first_sorted` for the regression pin). 3 4xx sites total, all confined to `list_activities`; **0 catch-all 5xx sites** in this router (the other six analytics endpoints have no file-local 4xx sites — `InternalTokenDep` from sprint #10 covers their auth). The `auth_required_for_agent_filter` reason value is *new in sprint #9* and joins the cross-module `AUTHENTICATION_REQUIRED` reason vocabulary alongside the existing `invalid_api_key` / `invalid_authorization_header_format` / `owner_or_internal_credential_required` set. 5 contract tests pin every raise site (2 for `auth_required_for_agent_filter` covering missing-header / non-Bearer-prefix branches, 1 for `invalid_api_key`, 2 for `api_key_agent_mismatch` covering single-id / multi-id filter forms). Test wiring needs `monkeypatch.setattr` on `acn.routes.analytics.get_agent_service` in addition to `app.dependency_overrides` because the route fetches the agent service via a *module-level* call rather than as a FastAPI parameter dependency — without the monkeypatch the agent-service stub is silently ignored and tests fall through to the `invalid_api_key` branch.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | ✅          |
| 10         | `dependencies` (auth-shared module)    | reuse cross-module catalog: `AUTHENTICATION_REQUIRED` (×4 — `_resolve_agent_by_bearer` invalid_api_key, `verify_agent_api_key` invalid_authorization_header_format, `verify_proxy_caller` invalid_authorization_header_format, `verify_owner_or_internal` owner_or_internal_credential_required), `INTERNAL_TOKEN_INVALID` (×2 — `verify_internal_token` and `verify_owner_or_internal` priority branch; both with empty `details` to avoid leaking the wrong token via response logs), `API_KEY_AGENT_MISMATCH` (×1 — `verify_owner_or_internal` owner-key-for-different-agent path), `INVALID_REQUEST` (×1 — `assert_system_caller` 422 with `{field, reason="system_namespace_required", value}`). 8 4xx sites total; **1 5xx site preserved** (`get_allowlist_service` 503 with `Retry-After: 300` — declined to migrate because `ACNHTTPError` rejects 5xx at construction time and the 503-with-retry-hint contract is load-bearing for the configured-disabled vs crashed distinction). 8 contract tests pin every 4xx raise site (1:1 coverage), each driven through a representative route so the central handler runs end-to-end. **Sprint #10 unblocks footnote `[^1]`** (the long-standing "auth-dep 4xx still on legacy shape" caveat in `acn-error-schema.md`) — that footnote has been deleted in this sprint and the SDK parsing template's "auth dep rejects" disclaimer goes away. The 422 site stays 422 (vs being downgraded to 400) so SDK clients on the migrated path don't see a status-code regression vs the legacy emission.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | ✅          |
| 11a        | `websocket-http`                       | reuse cross-module catalog only — no new ErrorCodes. `API_KEY_AGENT_MISMATCH` (×1 — `get_agent_websocket_status` path-key mismatch on `GET /api/v1/websocket/agent/{agent_id}/status`; strict `{path_agent, key_agent}` shape, identical emitter pattern as registry/payments/follows/onchain/analytics). 1 4xx site total; **0 catch-all 5xx sites**. Sprint #7 had inadvertently declared `websocket` as having no HTTP router (incorrect — the router exposes both `GET /api/v1/websocket/connections` gated by `InternalTokenDep` and the path-checked `…/agent/{agent_id}/status`); #11a corrects that documentation drift, adds `responses=ACN_DEFAULT_RESPONSES` to the router, and adds the `status` endpoint to `REPRESENTATIVE_ENDPOINTS` so OpenAPI advertises the flat schema for both HTTP routes via the uniform router-level merge. 1 contract test pins the single raise site (1:1 coverage). Test wiring overrides the `verify_agent_api_key` dependency directly (rather than monkey-patching `get_agent_service`) because the route consumes auth via `AgentApiKeyDep` which is a FastAPI dependency — same pattern as #5 / #6 / #7 onchain. The sibling WebSocket protocol endpoint (`WEBSOCKET /ws/{agent_id}`) is NOT touched here; its error contract is governed by RFC 6455 close codes and is split out as #11b.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | ✅          |
| 11b        | `websocket-protocol`                   | Implemented 2026-05-03 (commit `76afef5`). All 5 handshake-phase error sites migrated to typed dual-channel contract (application error-frame + compact `{c, r}` close-reason). 0 new ErrorCodes; 3 new `AUTHENTICATION_REQUIRED.details.reason` values; site #4 split into 4a (4401) and 4b (4403) for SDK ergonomics. Post-implementation revision: D1c → D1b (details moved to application frame due to 123-byte close-reason budget overflow). **Bake-window cleanup 2026-05-04** (commit `73d2faa`): `WEBSOCKET_CLOSE_REASON_FORMAT` flag and legacy branch removed; compact is now the only wire shape. 5xx `error` field also removed in the same commit. 61 tests pass.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | ✅ Complete |


#### ~~5xx field deprecation ticket~~ ✅ Done early (2026-05-04)

The `error` field was removed from `_http_exception_handler` and `_unhandled_exception_handler` in `acn/api.py` on 2026-05-04 (commit `73d2faa`), ahead of the originally scheduled 2026-06-01 target. No real external SDK 0.5.x clients existed, so the waiting window was moot. Tests updated accordingly.

#### ~~P3 — OpenAPI schema visibility for ACN flat error response~~ ✅ Landed

**Resolution** (commit landing this entry): all 5 already-migrated
routers (`communication` pilot + sprint #1 `allowlist` + sprint #2
`registry` + sprint #3 `subnets` + sprint #4 `tasks`) now opt into a
shared default `responses=` block via:

```python
from acn.core.errors import ACN_DEFAULT_RESPONSES

router = APIRouter(
    prefix=...,
    tags=[...],
    responses=ACN_DEFAULT_RESPONSES,
)
```

The constant is defined once in `acn/core/errors.py` and covers
status codes 400 / 401 / 403 / 404 / 409 / 429 — each mapped to
`{"model": ACNErrorResponse, "description": ...}`. SDK type-gen
consumers now see the canonical flat schema for every 4xx response
across all migrated modules.

**Granularity choice — router-level default, NOT per-endpoint**.
The original ticket text below suggested per-endpoint `responses=`
blocks listing only the status codes a specific endpoint actually
raises. We chose router-level default instead because:

- per-endpoint precision has near-zero practical benefit for SDK
consumers (generated client code branches on the response *body*,
not on which subset of status codes a single endpoint might emit)
- per-endpoint maintenance cost is high (every new `ACNHTTPError`
raise site needs a matching decorator update with drift risk on
every refactor)
- router-level default is drift-proof, costs zero ongoing attention,
and over-specifies a few unused status codes per endpoint — pure
spec noise, never a correctness issue
- if a specific endpoint ever needs to advertise a *narrower* set
(or an additional non-default code), FastAPI supports per-endpoint
override on top of the router-level default — `add when needed, not in advance`.

**422 is intentionally NOT in the default**. FastAPI auto-emits 422
for pydantic validation failures with its own `HTTPValidationError`
schema. Aligning that with `ACNErrorResponse` is a separate P3
ticket below ("`RequestValidationError` alignment"); the default
here would prematurely pin a schema we explicitly chose not to
align yet.

**5xx codes are also absent**. The central
`_http_exception_handler` and `_unhandled_exception_handler` in
`acn/api.py` emit a 5xx body that *also* matches `ACNErrorResponse`
shape (during the deprecation window the body additionally carries
a legacy `error` field — see deprecation ticket above), but
advertising 5xx in `responses=` is misleading: 5xx are sanitised,
opaque, and not branched on by SDK clients the same way 4xx are.

**Forward-looking contract for sprint #5-#11**. New schema migration
sprints SHOULD pass `responses=ACN_DEFAULT_RESPONSES` when creating
their `APIRouter(...)`. The constant is the single source of truth;
adding a status code (e.g. 451) is a one-line edit that propagates
to every router that opted in.

**Tests**. `tests/test_openapi_acn_error_response.py` (17 tests):

- `ACNErrorResponse` is in `components.schemas` with the four-field
flat shape and `error_code` / `message` / `request_id` required.
- One representative endpoint per migrated router advertises all 6
default status codes, each referencing
`#/components/schemas/ACNErrorResponse`. (Per-endpoint sampling
is sufficient because the router-level mechanism is uniform — if
the representative endpoint has the spec, all do.)
- **Negative coverage** is empty as of sprint #11a. All HTTP-mounting
routers are migrated. Sprint #7 had prematurely declared the list
empty under the (incorrect) belief that the `websocket` router
owned only a WebSocket endpoint; in fact it also exposes two HTTP
endpoints (`GET /api/v1/websocket/connections` and
`GET /api/v1/websocket/agent/{agent_id}/status`). Sprint #11a
migrated the `status` endpoint's single 4xx site and added it to
`REPRESENTATIVE_ENDPOINTS`, restoring the invariant. The remaining
sprint (#11b `websocket-protocol`) addresses the
`WEBSOCKET /ws/{agent_id}` endpoint specifically — its error
contract uses RFC 6455 close codes, not HTTP responses, so it
never appears in the `NON_MIGRATED_ENDPOINTS` list (the OpenAPI
spec doesn't advertise WS-protocol errors as HTTP responses in
the first place). The
`TestNonMigratedRoutersDoNotAdvertiseDefault` class stays in the
test file as a structural anchor — a single
`test_class_remains_a_structural_anchor` placeholder asserts the
list is empty, so anyone who adds a new non-migrated HTTP router
in the future is guided through the docstring to re-engage drift
detection. (Drift detection lifecycle: routers used to flip from
negative to positive coverage in the same commit, e.g. sprint #6
`follows` flipped at sprint #5→#6 and sprint #7 `onchain` flipped
at sprint #9→#7.)

**Caveat — 5 routers ≠ 5 URL prefixes**. The "5 migrated modules"
framing is at the *router* layer, not the URL prefix layer. The
`/api/v1/communication/`* namespace in particular is split across
two routers:

- `acn/routes/communication.py` (✅ migrated, this commit) — owns
`POST /send`, `POST /broadcast`, `POST /broadcast-by-tag`, `GET /history/{agent_id}`, `POST /ack`, `POST /internal/send`
- `acn/routes/manifest.py` (✅ sprint #8) — owns `GET /manifest/{agent_id}`, `DELETE /manifest/{agent_id}/{mid}`, `GET /content/{mid}`

So SDK consumers parsing `/openapi.json` today will see *mixed* 4xx
type-gen across the `/api/v1/communication/`* namespace was a known
heterogeneity throughout sprints #1–#7: `ACNErrorResponse` for
`/send` and `/broadcast`, generic `dict` for `/manifest/*` and
`/content/*`. Sprint #8 (manifest) closes this gap — the entire
`/api/v1/communication/*` namespace now type-generates against
`ACNErrorResponse`, and SDK release-notes can describe the
migration as "all `/communication/*` endpoints" without caveats.

Out of scope: regenerating downstream SDK type-gen artefacts. That
is the SDK release-notes owner's responsibility.

**Original ticket text retained below for archeology / context.**

---

`ACNErrorResponse` is defined in `[acn/core/errors.py](../acn/core/errors.py)` but is not advertised in `/openapi.json` for pilot routes today: FastAPI cannot statically infer which routes raise `ACNHTTPError`, so SDK type-gen consumers see `HTTPValidationError` / generic `dict` for 4xx responses instead of the canonical flat shape.

Path forward — for each pilot or migrated route, add an explicit `responses=` block on the route decorator:

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

Suggested batching: do this *atomically* with each row of the sprint roadmap above (route migration + OpenAPI advertisement in the same PR), so the doc and the matrix flip together. Pilot routes (already ✅ in the matrix) can be retro-fitted in a single follow-up PR — estimated 1-2 hours, no new tests required.

Out of scope for this ticket: regenerating downstream SDK type-gen artefacts. That is the SDK release-notes owner's responsibility.

#### ~~P3 — Add `except ACNHTTPError: raise` defence on catch-all 5xx blocks~~ ✅ Landed

**Resolution** (commit landing this entry): all 11 catch-all
`except Exception` blocks across `registry.py` (3) + `subnets.py`
(7 — `list_subnets` already had local defence from sprint
#3-followup) + `tasks.py` (1) now carry the matching pair:

```python
except ACNHTTPError:
    raise
except HTTPException:
    raise
except Exception as e:
    logger.error(...)
    raise HTTPException(status_code=500, detail=...) from e
```

Three catch-all blocks (one per file: `create_subnet`,
`dev_register_agent`, `create_task`) keep an in-file rationale
comment so first-time readers don't have to chase the BACKLOG ticket
for context. The other 9 are intentionally compact (just two
`raise` lines) because the rationale is identical and grep-able.

**Latent bug fix**: `delete_subnet`'s `else: raise HTTPException( 404, "Subnet not found")` short-circuit (in the same `try` body as
the catch-all) now correctly propagates as 404 instead of being
silently rewritten to 500 — the new `except HTTPException: raise`
line catches it. Regression test pinned in
`tests/routes/test_subnets_error_schema.py::TestSubnetsCatchAllDefence:: test_delete_subnet_returns_none_propagates_404`. Pre-defence this
test would have asserted 500.

**Forward-looking contract test**: 
`test_create_subnet_inner_acnhttperror_propagates` mocks
`subnet_service.create_subnet` to raise `ACNHTTPError` directly
inside the `try` body and asserts the catch-all does NOT swallow it.
Pins the contract for any future refactor that moves an
`ACNHTTPError` raise into a try body (intentional or accidental).

**Original ticket text retained below for archeology / context.**

---

**Affected files**:

- `acn/routes/registry.py` — 3 catch-all `except Exception as e: raise HTTPException(500, str(e))` blocks at L316 (`register` / dev), L425 (`register_protected`), L807 (`_join_agent_impl`).
- `acn/routes/subnets.py` — 8 catch-all `except Exception` blocks (`create_subnet`, `list_subnets`, `get_subnet_agents`, `join_subnet`, `leave_subnet`, `delete_subnet`, plus the two internal admin endpoints `admin_add_subnet_member` and `admin_remove_subnet_member`). Sprint #3's audit also surfaced one **active fragility**: the `else: raise HTTPException(404, "Subnet not found")` short-circuit *inside* `delete_subnet`'s `try` body (line ≈367) is silently rewritten to 500 by the surrounding `except Exception`. This is a pre-existing latent bug in the legacy `HTTPException` form already; a future migration of that site to `ACNHTTPError` would have the same fate (since `ACNHTTPError` is intentionally `Exception`-typed, not `HTTPException`-typed — see `acn/core/errors.py` docstring rationale). The fix is the same `except ACNHTTPError: raise` defence; for `delete_subnet` specifically the defence must also include a separate `except HTTPException: raise` line to repair the pre-existing 404 → 500 silent rewrite.

For registry, sprints #2a and #3 confirmed no `ACNHTTPError` is currently raised *inside* any of these `try` blocks — the migrated raises all live either *before* the `try` or in *specific* `except` clauses. The fragility is forward-looking: sprints #2b, #2c, and #3-followup will add new `ACNHTTPError(...)` raises to these handlers (`DEV_MODE_DISABLED`, `OWNER_TOKEN_MISMATCH`, auth/permission codes, `INVALID_CLAIM_REQUEST`). If any of those raises lands *inside* a try block (even temporarily during refactor), the `except Exception` will silently swallow it and convert a caller-actionable 4xx into a sanitised 500.

Defence (low cost):

```python
try:
    agent = await agent_service.register_agent(...)
    return AgentRegisterResponse(...)
except ACNHTTPError:
    # caller-actionable 4xx — propagate verbatim, do not wrap as 500
    raise
except Exception as e:
    logger.error("agent_registration_failed", error=str(e))
    raise HTTPException(status_code=500, detail=str(e)) from e
```

Combined ≈11 locations × 3 lines each = ≈35-line change.

**Status update (post sprint #2b/#3-followup/#4-followup, May 2026)**:
The Step 2 sprint sweep (commits `8c9388a` / `8a9d7f2` / `a2955bc` /
`3c632fa`) landed but did **not** include this defence holistically.
Only one local defence was applied: `subnets.py::list_subnets` got
`except ACNHTTPError: raise` because sprint #3-followup actually
introduced new ACN raises into its `try` body, turning the latent
fragility into an active bug for that endpoint specifically. The
remaining 10 catch-all blocks are still undefended:

- `acn/routes/registry.py` — 3 catch-all blocks (audited, **no
current** ACN raises live inside their try bodies → still latent).
- `acn/routes/subnets.py` — 7 catch-all blocks (was 8 before the
`list_subnets` local fix); `delete_subnet` still carries its
pre-existing 404→500 silent rewrite (the `else: raise HTTPException(404)` site, which we explicitly chose **not** to
migrate during sprint #3-followup so the latent fix doesn't leak
into a separate concern — cf. the inline NOTE in `subnets.py`).
- `acn/routes/tasks.py` — sprint #4-followup audited the catch-alls;
none carry ACN raises in-try, so they are also latent-only.

The defence still wants to land as a single focused PR (separate
commit history is more valuable than amending the four Step 2
commits retroactively, since pre-existing latent bugs and new active
risks are conceptually distinct).

Why not now (after Step 2):

- For registry / tasks: post-sprint audits confirm zero current
breakage paths — pure latent defence.
- For subnets `delete_subnet`: the 404→500 silent rewrite pre-dates
any migration; bundling its fix into the cross-module defence PR
keeps the bug fix loud and auditable.
- A standalone defence PR can also add the matching `except HTTPException: raise` line uniformly, repairing pre-existing
latent bugs in legacy `HTTPException` raises (same shape as the
`ACNHTTPError` defence, no behaviour change for already-correct
paths).

#### Process note — sprint sweep test-coverage gap (post Step 2 audit)

A self-audit on the four Step 2 commits (`8c9388a` / `8a9d7f2` /
`a2955bc` / `3c632fa`) found **5 wire-shape regressions** across 3
non-schema test files (`test_auth_failure_audit_h_audit.py`,
`test_get_task_h8_auth.py`, `test_tasks_rate_limit_h7.py`). They all
asserted on the legacy `r.json()["detail"]` body or on `pytest.raises( HTTPException)`. The Step 2 commits passed the focused
`test_*_error_schema.py` subset but the regressions only surfaced when
the full `tests/routes/` suite was run during audit. Fixed in commit
`16b6ca8` (audit-followup). Retroactive lesson:

> **Any commit that flips a route's emitted error wire-shape (from
> `HTTPException` to `ACNHTTPError` or vice versa, or removes the
> double-emit `error` 5xx field) MUST run the full `tests/` suite
> before merge — running only the dedicated `test_*_error_schema.py`
> modules or even the full `tests/routes/` subtree is insufficient,
> because module-level sanitisation tests (e.g.
> `tests/test_error_sanitisation.py`) and integration tests live
> outside `tests/routes/` and may still assert on legacy shapes.**

Concretely for the remaining schema migration sprint (#11b websocket-protocol):
the PR should include a
`pytest tests/ -q --no-cov --ignore=tests/integration` smoke run in the description (≈9 min on
a warm cache) in addition to the schema-test subset that's already
part of the sprint acceptance criteria. The cost is small relative
to the cost of a post-merge revert when a non-schema test breaks on
`main`.

The "tests/routes/ is enough" rule was the *first* iteration of this
note, written after the Step 2 audit found 5 wire-shape regressions
in 3 route-test files. A subsequent finding in the cross-module
catch-all defence sweep (May 2026) showed that `tests/test_error_ sanitisation.py` (module-scope, not under `tests/routes/`) can also
carry stale assertions when an in-progress deprecation lands in the
working tree before its companion `acn/api.py` change. Widening the
rule to `tests/` ensures both kinds of drift are caught pre-merge.

#### ~~P3 — Hoist shared route-test fixtures to `tests/routes/conftest.py~~` ✅ Landed

Hoisted in a focused follow-up commit after sprint row #3 — `tests/routes/conftest.py` now owns:

- `_reset_state` (autouse) — disables `slowapi.limiter`, clears `_api_key_cache`, clears `app.dependency_overrides`
- `_FLAT_SCHEMA_FIELDS` constant — the canonical four-field set for ACN flat error responses
- `_assert_flat_shape(body)` helper — the shared schema-shape invariant

The three schema test files (`test_allowlist_error_schema.py`, `test_registry_error_schema.py`, `test_subnets_error_schema.py`) drop their per-file copies and inherit / import from the conftest.

Six pre-existing route test files (`test_allowlist_routes.py`, `test_agent_endpoint_disclosure.py`, `test_agent_policy_patch.py`, `test_manifest_routes.py`, `test_agent_social_card_url_patch.py`, `test_agent_card_url_sanitize.py`) keep their own `_reset_state` autouse fixtures. Pytest's fixture override rules (closest scope wins) mean the conftest copy is silently overridden in those scopes — five of the six file-local copies are byte-identical to the conftest version (idempotent), and `test_agent_card_url_sanitize.py` deliberately omits the API-key-cache clear because it doesn't authenticate. The `tests/routes/conftest.py` module docstring explains this override pattern so future contributors don't read it as a hazard.

Net effect: ~70 LOC removed from the three schema files; ~30 LOC of conftest infrastructure added; future schema migration sprints (rows #4–#11) inherit the reset fixture for free. Verified by 102/102 passing tests across all nine route test files (three hoisted, six preserving their overrides).

#### ~~P3 — `RequestValidationError` alignment~~ ✅ Done (2026-05-05)

`api.py` 新增 `@app.exception_handler(RequestValidationError)`：
- 返回 `{error_code: "validation_failed", message: "Request validation failed.",
  details: {pydantic_errors: [...]}, request_id}` — 与 ACN 平坦 schema 一致
- Pydantic 的完整 `errors()` 列表（含 `loc`/`msg`/`type`）保留在 `details.pydantic_errors`
- `ErrorCode.VALIDATION_FAILED` 已加入 `acn/core/errors.py`；OpenAPI 测试（28 tests）全通过

### ~~Alembic chain hygiene~~ ✅ Done (2026-05-05)

Context: 修 `7ee2ed3a177c`（`expand_verification_code_to_64chars`）的 fresh-DB upgrade fail 时连带发现 — `alembic downgrade base` 全链回滚会在 initial schema 阶段炸。

- ~~`**8d958bd38c11_initial_schema.py` participations FK 未命名导致 downgrade 不可用**（P2）~~ ✅ Done
Added `name="participations_task_id_fkey"` to the `sa.ForeignKeyConstraint` in
`alembic/versions/8d958bd38c11_initial_schema.py`. SQLAlchemy metadata now carries
the name so `alembic downgrade base` can emit `DROP CONSTRAINT` correctly.

---

## Task / Agent

### P0 sweep follow-ups

Context: commits for SCALE_AUDIT P0-1..P0-4。完成了正确性修复，留下性能/清理项。

- ~~`**RedisTaskRepository.delete` 用 pipeline 批量取 participation + user-index `lrem` 也放 pipeline**~~ ✅ 已完成（P2-D）
  - Step-1：pipeline 批量 `HGETALL` 所有 participation hash（N 次串行 → 1 次批次）；`_dict_to_participation` 在 Python 侧逐条解析结果。
  - Step-2+3：主 task hash、所有 index（`zrem/srem/delete`）和 participation sidecar key 合并为一个 pipeline 批次。
  - Step-4：所有用户 `lrem` 调用（O(users × pids) 串行 → 1 次 pipeline 批次）。
  - 测试更新：`test_task_repository_delete_cleanup.py` 重写以正确 mock pipeline 上下文管理器，检验 `pipe.hgetall/delete/lrem` 调用。

---

## Payments / Billing

### P1 sweep follow-ups

Context: commits for SCALE_AUDIT P1-4 / P1-5。

- ~~**In-flight `PaymentTask` 永不过期的兜底清理**（P1-5 后续）~~ ✅ 已修
  - `PaymentTaskManager.sweep_stale_tasks(stale_after_days=7)` — 扫 `acn:payment_tasks:`*（跳过含 `:` 的 index/audit sidecar key），将超龄非终态 task 强制改为 `FAILED`，审计记录 `reason: stale_sweep`；`_save_task` 随即给其加 180 天 TTL。
  - `update_task_status` 新增 `metadata: dict | None` 参数，合并到审计日志 data 字段。
  - `api.py` 每 6 小时运行一次 `_payment_sweeper` background task，和 `_heartbeat_watchdog` 并列，shutdown 时一并取消。
- ~~**Billing fallback 的 PG 迁移路径**（P1-4 后续）~~ ✅ 已修（可见性部分）
  - `BillingService.storage_mode` 属性：返回 `"postgres"` 或 `"redis_fallback"`。
  - 启动时若为 fallback，lifespan 打 `logger.warning("billing_on_redis_fallback", ...)`。
  - `/ready` 响应新增 `"billing_storage"` 字段（纯信息，不影响 HTTP 状态码——fallback 是降级不是故障）。
  - 后续若要 strict 模式（强制 PG），在 `BillingService.__init__` 加 `strict: bool = False` 参数，`True` 时 `_billing_repository is None` 直接 `raise`。

---

## Monitoring

### P1-2 follow-ups

- **Metrics key 的 `scan_iter` 在 `prometheus_export` / `get_all_metrics` 是 O(N_keys)**
P1-2 砍掉了 `(from_agent, to_agent)` 的高基数 label 后，稳态 key 数已经被压成可控量级，但 export 路径仍然是全扫。如果将来又因新需求长出几万个 label 组合，scan 就会拖慢 scrape。考虑维护一个 `acn:metrics:_index` set 记录所有活跃 key，export 时直接 SMEMBERS 替换 SCAN。
影响文件：`[acn/monitoring/metrics.py](../acn/monitoring/metrics.py)` `prometheus_export()` / `get_all_metrics()`.
- ~~**Adhoc counter（`METRICS` 没声明的）仍然能无限增长 label key 集合**~~ ✅ 已完成（Metrics cardinality guard sprint）
新增 `_MAX_ADHOC_LABEL_KEYS = 3`：未在 `METRICS` 注册的 metric 最多保留 3 个 label key，超出的 key 被截断并打 WARNING（模块级 `_warned_adhoc_overflow` 去重，每个 metric 名只打一次）。同时将 `acn_broadcast_sent` 补进 `METRICS` 正式注册，消除了代码里唯一的 ad-hoc 调用。

### Per-agent activity via PG `activity_events`（P1-9 后续）

- ~~`**Analytics.get_agent_activity` 的 per-agent 消息/错误计数目前恒为 `None**~~`
**已修（Routes 契约全扫 sprint）**：`messages_sent` 和 `errors` 现在从 PG `activity_events` 聚合，`messages_received` 仍为 `None`（需要 task-join 聚合，见下条）。
修改文件：`acn/monitoring/analytics.py` `get_agent_activity()`、`acn/services/activity_service.py` 新增 `get_activity_counts` / `get_last_activity_at`、`acn/core/interfaces/activity_repository.py` + `acn/infrastructure/persistence/postgres/activity_repository.py` 新增 `count_by_agent_and_type` / `get_last_activity_at`、`acn/api.py` 注入 `activity_service` 到 `Analytics`。
- ~~`**messages_received` 仍需 task-join 聚合**~~ ✅ 已修（比 BACKLOG 预期简单）
`task_approved` / `task_rejected` 事件在 `event_metadata["agent_id"]` 里已存有 target agent 的 ID，无需 JOIN `participations`。
`IActivityRepository.count_received_by_agent` + `PostgresActivityRepository` 实现 + `ActivityService.get_received_count` + `Analytics.get_agent_activity` 现返回 `messages_received: int | None`。
未覆盖：`task_cancelled` inbound（creator 取消）仍无 metadata.agent_id，影响可接受（取消已计入 `errors`）。

### Analytics 的 PG 迁移方向（P2-3 延伸）

- ~~`**get_agent_stats` / `get_subnet_stats` 的真源应在 PG 而非 Redis scan**~~ ✅ 已完成（P2-C）
  - `Analytics.__init`__ 新增 `agent_repo: IAgentRepository | None` 和 `subnet_repo: ISubnetRepository | None`。
  - `get_agent_stats`：repo 可用时调用 `find_all()` 在 Python 侧聚合 `by_status / by_subnet / by_tag / recent_registrations`；无 repo 时 fallback 到 Redis scan。
  - `get_subnet_stats`：repo 可用时调用 `find_all()` + `count_by_subnet()`；无 repo 时 fallback 到 Redis scan。
  - 顺带修复历史 bug：`by_status` 初始化键从 `"active/inactive"` 改为 `"online/offline/unknown"`；`get_system_health` 读 `by_status["online"]`（原来读 `"active"` 永远为 0）。
  - `api.py` 构造 `Analytics` 时传入 `agent_repository` / `subnet_repository`。

### Redis tag 索引（P2-4 延伸）

- `**find_open_tasks(tags=...)` 在 Redis 分支仍是 Python-side filter**
P2-4 把 `TaskPool.find_tasks_for_agent` 的重复过滤层消掉了，PG 分支立刻享受原生 `required_tags @> ARRAY[...]` 的 SQL 过滤，但 Redis 分支里 `find_open_tasks` 还是 `ZREVRANGE(acn:tasks:open)` 一页 + `task.matches_tags(tags)` 在 Python 端逐条过滤。
真正的 scale 修法：
  - `save(task)` / 状态变更时维护每个 tag 一个 `acn:tasks:by_tag:{tag}`（zset，score=created_at，member=task_id）
  - `find_open_tasks(tags=[t1, t2])` 用 `ZINTERSTORE` 或按需 `ZREVRANGEBYSCORE` 每个 tag 的 zset 后交集
  - 子网可见性维度独立，交集后再做子网过滤
  目前没赶着做的原因：Redis 分支本就是 "no-PG fallback"，生产部署会用 PG 分支（已经天然零成本过滤）；tag-index 方案比修 pattern 复杂，值得等一个真实 scale 信号再做。
  影响文件：`[acn/infrastructure/persistence/redis/task_repository.py](../acn/infrastructure/persistence/redis/task_repository.py)` `save` / `_update_status` / `find_open_tasks`。

### ~~broadcast-by-tag 的 `total` 字段语义不准确（P3）~~ ✅ 已修

- **修法**：在截断前记录 `total_sent = len(responses)`，截断后的数量改用新字段 `returned` 表示。响应结构变为 `{"total": <实际广播数>, "returned": <本次返回数>, ...}`。`logger` 同步改为记录 `total_sent` / `returned`。

---

## Routes smoke tests

### ~~扩大覆盖范围~~ ✅ 已完成（Routes 契约全扫 sprint）

所有 7 个 router 已加两层 smoke test：`TestMethodNamesStillExist`（防改名）+ `TestRouteServiceContract`（`assert_called_with` 校验参数位置）。测试文件：`tests/routes/test_route_service_contracts.py`（41 tests 全绿）。

同时修复了扫描发现的全部契约 bug：

- `routes/websocket.py`：`connect(agent_id, websocket)` 参数反转 → `connect(websocket, user_id=agent_id, already_accepted=True)`；`get_active_connections` / `is_connected` 不存在 → 改用 `get_stats()` / 新增 `is_user_connected`
- `routes/communication.py`：`record_message/record_broadcast` → `inc_message_count/inc_counter`；`retry_failed_messages` → `retry_dlq`
- `routes/payments.py`：`discover_agents` → `find_agents_accepting_payment`；`get_agent_tasks` → `get_tasks_by_agent`；`get_agent_stats` → `get_payment_stats`；`create_payment_task` 补 `network` 进 metadata

### ~~Routes ↔ services 契约全扫（P1-9 / SCALE_AUDIT 收尾审核发现）~~ ✅ 已完成

见上。

---

## 遗留问题备忘

### ~~`acn_broadcasts_total` 僵尸 metric~~ ✅ 已清理

删除了 `METRICS` 里从未被写入的 `acn_broadcasts_total` 声明，同步将 `analytics.py` 的 `broadcast_pattern` SCAN 从 `acn:metrics:acn_broadcasts_total:`* 改为 `acn:metrics:acn_broadcast_sent:`*（实际写入的 counter，labels: `["type", "status"]`）。

---

## Security audit follow-ups

### ~~`submit_task` 单参与者路径未走 CAS~~ ✅ Done (2026-05-05)

H3 sprint 中只覆盖了 `complete_task` / `reject_task` / `cancel_task`。`submit_task` 单参与者分支现已
改为 `compare_and_save(task, expected_status=task.status_before_transition)`：
- 捕获 `expected_status = task.status`（`IN_PROGRESS` 或 `REJECTED`）before calling `task.submit()` / `task.resubmit()`
- CAS 输了 → `return await self.get_task(task_id)`（幂等返回）
- 副作用（escrow `submit_v2` / activity record）只在 CAS 胜出后执行，杜绝双触发

---

### `BillingService.process_transaction` / `refund_transaction` 缺乏并发保护（H3 延伸）

P1 H3 已经把 task 状态机的 `complete_task` / `reject_task` / `cancel_task` 改成 CAS save，避免双发奖。但 `BillingService.process_transaction` / `refund_transaction` 走的还是「读 → 内存 status check → 调 callback 真扣钱 → 写回」三段式，并发同样会双扣。

目前没有路由直接暴露这两个方法（只在 service 内部使用，且通过未实现的 `payment_manager.update_status` 调用，被 `try/except` 吞掉），所以**当前无可触发的攻击面**。但上线后任何路由 / 内部 admin 工具一旦接入，立刻有真扣双付风险。

修法（参考 H3 task_repository 的 CAS 模式）：

- `BillingTransactionStatus` 加 `PROCESSING` / `REFUNDING` 中间态
- `IBillingRepository` 加 `claim_status_transition(tx_id, expected, new)`
- PG 用 `UPDATE ... WHERE status=? RETURNING` * 实现
- Redis fallback 用 Lua 脚本 CAS（参考 `LUA_CAS_TASK_STATUS`）
- `BillingService.process_transaction`：先 CAS PENDING→PROCESSING，CAS 输了直接幂等返回；CAS 赢了再调 deduct/add callbacks，最后 finalize PROCESSING→COMPLETED/FAILED

影响文件：`acn/services/billing_service.py`、`acn/core/interfaces/billing_repository.py`、`acn/infrastructure/persistence/postgres/billing_repository.py`、`acn/infrastructure/persistence/redis/billing_repository.py`（如存在）。

### ~~`submit_task` 单参与者路径未走 CAS~~ ✅ Done (重复条目，见上方 ✅ 条目)

已实现：`task_service.py` `submit_task` 单参与者路径在第 718 行捕获 `expected_status`，调用 `compare_and_save(task, expected_status=expected_status)`。与上方 2026-05-05 的 ✅ 条目完全一致，本条为重复记录。

### Dependency 阶段失败请求不被限流（H7 延伸）

H7 给所有 task 写端点挂了 `@limiter.limit` + per-identity bucketing，但 slowapi 的 `async_wrapper._check_request_limit` 是 endpoint 装饰器层 —— **dependency 阶段抛 `HTTPException` 的请求根本不会进 endpoint，limiter 不参与**。

后果：

- 攻击者反复发 invalid `acn_xxx` Bearer：`require_task_write_auth` 会触发 `agent_service.get_agent_by_api_key` 真去 DB 查（缓存只缓存有效 key，invalid key 永远穿透），可被滥用打 DB
- 同理 invalid JWT 会反复打 Auth0 JWKS 校验路径（已有缓存，但 dependency 失败请求不计 budget）
- `verify_proxy_caller` / `verify_internal_token` 同类问题

H7 范围内不修，因为本身只针对 *已认证* agent 的高频写。修法属于 middleware 层限流：

- 选项 A：用 `slowapi.middleware.SlowAPIMiddleware`（整 app 级别全局默认限流）
- 选项 B：自己写一个 `BaseHTTPMiddleware`，对所有 `/api/v1/`* 请求按 `_get_real_ip` 做粗粒度全局限流（如 600/min/IP），独立于 endpoint-level limit
- 选项 C：把认证逻辑里的"未命中缓存就立刻拒绝"逻辑加上 negative cache（短 TTL 缓存 invalid key 的失败结果，避免反复打 DB）

优先级低，因为：dependency 失败本身已经返回 401/403，反代/WAF 层（生产部署一般有）的常规 IP 限流就能挡掉大量低成本探测；真正昂贵的 DB query 也有连接池兜底。但记下来，以防部署形态变化。

影响文件：`acn/api.py` middleware 链、`acn/routes/dependencies.py`（_resolve_agent_by_bearer 加 negative cache）、`acn/auth/middleware.py` verify_token 缓存。

### ~~路径/查询参数缺 `max_length`（H6 延伸）~~ ✅ 已修（P2-#3）

H6 把所有 body Pydantic 模型的字符串字段都加了 `max_length`，并装了 1 MiB 总 body cap。但路径参数（`subnet_id` / `agent_id` 等 `str` path / query params）仍是无界 — 攻击者可以发 `GET /api/v1/subnets/<10MB-string>/...`，体积不大但仍会进入 Redis key 拼接 / SQL like 查询 / audit log。

风险点：

- Starlette 默认 URL 头部大约 64 KB；URL path 在 ASGI 层不会被 H6 body middleware 拦下
- 这些参数会进 Redis key（`acn:subnet:{subnet_id}`）和 SQL（PostgreSQL VARCHAR 字段约束在 schema 层）
- audit log 直接写 `subnet_id` 到结构化字段，超长字符串会膨胀日志体积

修法：

- 在 routes 层用 FastAPI `Path(..., max_length=N)` / `Query(..., max_length=N)` 显式限制
- 或者在 dependencies 里加一个通用的 `validate_id_length` 依赖
- 优先级：subnets / registry / tasks 的 path params；query params 中 search 类（tags、status 等）

影响文件：`acn/routes/subnets.py`、`acn/routes/registry.py`（部分）、`acn/routes/tasks.py`（部分）。

### ~~dict 字段单字段无独立 size cap（H6 延伸）~~ ✅ Done (2026-05-05)

选项 A 已落地：`acn/core/validators.py` 提供 `make_dict_size_validator` /
`check_dict_size_64k` / `check_dict_size_256k` 工厂，挂到以下字段（JSON 序列化后字节数）：


| 文件                 | 字段                                       | 上限     |
| ------------------ | ---------------------------------------- | ------ |
| `communication.py` | `SendMessageRequest.message`             | 256 KB |
| `communication.py` | `BroadcastRequest.message`               | 256 KB |
| `communication.py` | `BroadcastByTagRequest.message`          | 256 KB |
| `tasks.py`         | `TaskCreateRequest.metadata`             | 64 KB  |
| `tasks.py`         | `TaskCreateRequest.ui_spec`              | 64 KB  |
| `registry.py`      | `AgentRegisterRequest.agent_card`        | 64 KB  |
| `payments.py`      | `PaymentCapabilityRequest.token_pricing` | 64 KB  |
| `payments.py`      | `CreatePaymentTaskRequest.metadata`      | 64 KB  |


`wallet_addresses` / `proof` 字段结构已受 `dict[str, str]` 类型约束（单值 `max_length` 限定），
暂不额外加 dict size cap（攻击面极小）。

### HTTP 请求走私防御依赖 ASGI server（H6 延伸）

`BodySizeLimitMiddleware` 的 Content-Length 预检只 break 第一个 `content-length` 头。Uvicorn 用 h11 解析 HTTP，会在请求到 ASGI scope **之前**拒绝重复 Content-Length / Transfer-Encoding+Content-Length 共存（HTTP smuggling 主要攻击形态），所以当前部署下不暴露问题。

但如果未来切换 ASGI server（Daphne / Hypercorn / AWS Lambda ASGI adapter）：

- 这些 server 对重复头的处理可能宽松
- middleware 层应改成扫描所有 `content-length` 头并检查是否一致 / 取最大值
- 同步审 `Transfer-Encoding: chunked` 与 Content-Length 共存的行为

修法：把 `for ... break` 改为一次遍历，收集所有 CL 值，不一致就 reject。

影响文件：`acn/middleware.py`。

### ~~`_PENDING_AUDIT_TASKS` 无界 + 缺 shutdown drain（H-audit 延伸）~~ ✅ 已修（P2-#1，B+C 组合）

`fire_and_forget_event` 用 module-level set 持强引用避免 GC 回收 task（必须的，不能改回去）。但目前：

1. **set 没上限**：Redis 持续慢/挂 + 持续 auth failure，set 无界增长。每个 task 间接持住 audit 客户端、协程 frame、log_event closure 闭包变量。极端场景（Redis 挂 10 分钟 + 1k auth failure/s）= 60w pending tasks。
2. **shutdown 不 drain**：lifespan teardown 时 audit logger `_started=False` 拦掉新事件，但 in-flight task 不被 await。event loop 关闭时被强制 cancel，audit 数据丢失。

当前不修的原因：

- 容量风险有 redis xadd `maxlen=100k` cap 兜底（每个 task 失败也只是少一条审计），存储侧可控
- shutdown 丢数据 ≤ 几秒前的 best-effort 事件，文档已说明 audit 是 best-effort

未来部署形态变化时（k8s rolling restart 频率高 / Redis 慢调用占比大）需要修：

- 选项 A：set 改 `bounded asyncio.Queue` + 单 consumer（限定并发，超限直接 drop + counter）
- 选项 B：shutdown 时 `await asyncio.wait_for(asyncio.gather(*_PENDING_AUDIT_TASKS), timeout=3)`
- 选项 C：给 `_safe_write` 加 per-call timeout（`asyncio.wait_for(audit.log_event(...), 2.0)`），超时直接放弃

倾向 B+C 组合：单事件兜底 timeout（防 Redis 挂时累积），shutdown 兜底 drain（防 graceful restart 丢数据）。

影响文件：`acn/monitoring/audit.py` `fire_and_forget_event`、`acn/api.py` lifespan teardown。

### ~~Audit fire-and-forget 缺采样限速（H-audit 延伸）~~ ✅ Done (2026-05-05)

选项 A 已落地：`acn/monitoring/audit.py` 的 `record_auth_failure` 前加了 `_auth_sample_should_write()`：
- `_AUTH_SAMPLE_WINDOW_S = 1.0`：同 `(source_ip, reason)` 对在 1 秒内只写第一条
- `_AUTH_SAMPLE_MAX_KEYS = 1_000`：缓存最多 1 000 个 IP×reason 对，超限按插入顺序淘汰最旧 key
- 零额外 Redis 开销；低频事件（窗口过期后首次）总是写入，非攻击场景不受影响

### ~~`acn:audit:type:`* list 缺 expire（历史问题，H-audit 期间发现）~~ ✅ 已修（P2-#4）

`AuditLogger.log_event` 写三处：

- `acn:audit:stream`（xadd + xtrim maxlen=100k）✓
- `acn:audit:day:{YYYY-MM-DD}` list（lpush + ltrim 100k + **expire 30d**）✓
- `acn:audit:type:{event_type.value}` list（lpush + ltrim 100k，**没有 expire**）✗

后果：每个 event_type list 只受 100k entry cap 约束，永久驻留。冷门 event type（早期跑过的实验性事件）会无限期占 Redis 内存，即使每天 0 写入。`security_ssrf_blocked` / `admin_bulk_delete` 这种新增 type 长期看也会从 fresh 退化成"百分之一比例都是历史数据"，分析价值打折。

修法：在 `log_event` 里给 `type_key` 加 `expire(30 * 86400)`，跟 day_key 一致。30 天数据足够覆盖大部分事后追查。

影响文件：`acn/monitoring/audit.py` `log_event`（约第 130-160 行 `lpush + ltrim` 后）。

### ~~Lifespan startup 应预热 ERC-8004 RPC chain_id 校验（H-erc8004 延伸）~~ ✅ 已修（P2-#5，fail-fast on mismatch）

H-erc8004 把 chain_id 校验放在 bind 端点的运行时——首个 bind 请求触发 RPC `eth_chainId`，结果 cache 在 `ERC8004Client._cached_chain_id`。这够用：

- 攻击面已堵：bind 不可能在 RPC 链 ID 不匹配时成功
- cache 让后续 bind 零额外 RPC 开销

但仍有改进空间：

1. **运维事故晚发现**：如果运维把 `ERC8004_RPC_URL` 切到错链，第一个 bind 请求才会发现并返回 503。startup 时如果直接预热 + log error / exit-fast 更早暴露
2. **discover endpoint 不做 chain_id 校验**：`/onchain/discover` 只调 `discover_agents`，不查 chain_id —— 切错链时 discover 会返回错误链上的 agents，对外宣称是配置链的数据
3. **read-only 路径（reputation / validation）也不校验**：同 discover 问题

修法：

- lifespan startup 调一次 `await erc8004.verify_chain_id(settings.erc8004_chain_id)`：mismatch 直接 `raise SystemExit`（fail-fast），unreachable 时 `logger.error` + 继续启动（避免 RPC 临时挂导致服务起不来）
- 或更宽松：lifespan 调 `verify_chain_id` 但仅 warning，让 bind 端点的运行时检查兜底

倾向 fail-fast：chain_id mismatch 是配置事故而非临时故障，启动就崩比让运行时一个个 bind 失败更易诊断。

影响文件：`acn/api.py` lifespan startup、`acn/routes/onchain.py` 首次 bind 时不再需要 verify（可优化但兼容）。

### `verify_chain_id` cache 永不过期 → runtime RPC swap 防御缺口（H-erc8004 延伸）

H-erc8004 把 `_cached_chain_id` 设计成永久缓存（chain_id 是不变量）。这对"运维换链需要重启"的 case 是正确的 fail-closed 行为，但有一个 runtime 攻击向量被遗漏：

**攻击路径**：

1. ACN cold start → 第一次 bind → `verify_chain_id` 打 RPC → cache 写入 8453（合法值）
2. 之后所有 bind：`verify_chain_id` cache 命中，**完全跳过 RPC**
3. 攻击者通过 DNS hijack / BGP / 短暂拿到运维凭据 改 `ERC8004_RPC_URL`
4. `verify_registration` 仍然每次打 RPC（无 cache），但用的是**被劫持的 RPC**
5. 攻击者伪造 `tokenURI(token_id)` 响应匹配 ACN expected URL → bind 成功 → 错误绑定

后果：chain id 防御**只在 cold start 时有效**，cache 之后这层就失效了。只剩 verify_registration 的字符串比对（攻击者可控）。

为什么本 sprint 不修：

- 当前部署形态下 RPC swap 主要是配置事故而非主动攻击；startup verify_chain_id（前面的 BACKLOG 条目）已能兜底配置事故
- 加 TTL 会带来"RPC 间歇故障 → 5 分钟内 bind 全失败"的可用性 trade-off，需要生产数据指导调
- 真要防主动 RPC swap，多源 RPC 校验（不同 provider 同时验）才是正解

修法选项：

- 选项 A：`_cached_chain_id` 加 TTL（如 5 分钟），周期性重读捕获 swap。简单但 RPC 故障敏感
- 选项 B：bind 端点每次都调 RPC chain_id，不 cache。实时性最好但延迟 +1 RPC 往返
- 选项 C：多源 RPC（主用 Base RPC + 备用 Alchemy/Infura）每次同时调，结果不一致拒绝。最贵但最稳

倾向 A + startup verify 组合，等到能观察生产 RPC SLO 数据再调。

影响文件：`acn/services/erc8004_client.py` `get_chain_id` / `_cached_chain_id`。

### ~~`verify_chain_id` cold-start thundering herd（H-erc8004 延伸）~~ ✅ 已修（P2-#2，asyncio.Lock + 双检查）

`ERC8004Client.get_chain_id` 的 cache write 不是原子操作：

```python
if self._cached_chain_id is None:
    self._cached_chain_id = int(await self._w3.eth.chain_id)
return self._cached_chain_id
```

冷启动 + N 个并发 bind：所有请求都看到 `is None` → 都启动一次 RPC → cache 被覆盖 N 次。结果一致（chain_id 不变量），不是 bug，但浪费 N-1 次 RPC quota。

生产场景：典型 RPC provider（Alchemy free tier）有 25 req/s 软限。冷启动遇到 burst（如 deploy 后大量 retry）会触发限流。

修法：`asyncio.Lock` 保护 cache write，单 winner 调 RPC 写 cache，其他请求 await lock 然后命中 cache。

不在 H-erc8004 范围内修，因为：当前 cold start bind 流量极低（agent bind 是低频操作），thundering herd 实际触发概率低。但 chain_id check 将来可能扩展到 read-only 路径（reputation / discover），那时 burst 概率上升，需要修。

影响文件：`acn/services/erc8004_client.py` `get_chain_id`。

### 历史 `agent.erc8004_chain` 数据回收（H-erc8004 延伸）

H-erc8004 修复**只防新 bind**——已经存入数据库的 `agent.erc8004_chain` 仍然来自之前 client 自报路径，可能：

- 是默认 `eip155:8453`（绝大多数情况，恰巧匹配 server 配置但来源不可信）
- 是攻击者自报的伪造链（如 `eip155:1`，让查询方误以为 token 在 Ethereum mainnet）

这些值通过 `GET /onchain/agents/{id}` 与 `GET /api/v1/registry/{id}/wallets` 仍在对外吐出。新 bind 不会再产生坏数据，但旧数据原地不动。

修法：写一个 one-off `acn/scripts/recanonicalize_erc8004_chain.py`：

- SCAN `acn:agents:by_erc8004_id:`* 找出所有已绑定 agent
- 对每个：load agent → 把 `erc8004_chain` 强制重置为 `f"eip155:{settings.erc8004_chain_id}"` → save
- 默认 `--dry-run`（输出 N 条、原值分布），`--execute` 真改
- 跑前 + 跑后各打一次 audit log（`ADMIN_BULK_RECONCILE` 或类似 event）

不在 H-erc8004 范围内做的原因：单纯数据清扫，无新代码风险面，可以推迟到上线后窗口期跑一次。但**不做就有"老数据持续误导查询方"的尾巴**。

影响文件：`acn/scripts/recanonicalize_erc8004_chain.py`（new）、`acn/monitoring/audit.py`（如新增 event_type）。

### `BindRequest.chain` 字段长期可移除（H-erc8004 延伸）

H-erc8004 把 `chain` 改成 client-optional + 服务端派生 + 收到值必须匹配。短期保留是为了向后兼容 ACN Python client（`acn_client.client` 默认会传 `eip155:8453`）。

中期：

- ACN Python client 库下个版本删掉 `chain` 默认值，让服务端派生
- 1-2 个版本兼容期后，BindRequest 删掉 `chain` 字段，OpenAPI / docs 同步更新
- 下游消费者（外部 dashboard 等）通过 `GET /onchain/agents/{id}` 拿 server-derived chain，不依赖请求字段

短期保留是合理的；但留个 ticket 提醒别让向后兼容窗口无限延长。

影响文件：`acn/routes/onchain.py` `BindRequest`、`acn/clients/python/acn_client/client.py`。

### ~~Admin bulk delete 缺 by-id 模式（C1b/H-audit 延伸）~~ ✅ Done (2026-05-05)

选项 B 已落地：`admin_bulk_delete_agents` 新增 `agent_ids: str | None` query 参数
（逗号分隔的 agent ID 列表，如 `agent_ids=acn_abc,acn_def`）：
- 提供 `agent_ids` 时跳过全表扫描，直接按 ID 精确 fetch，缺失 ID 写 WARNING log
- `name_prefix` / `owner` / `agent_ids` 三过滤器可组合
- safety guard 更新：`dry_run=false` 必须提供三者之一
- `ADMIN_BULK_DELETE` audit summary 新增 `agent_ids` 字段

后续：`scan_unsafe_endpoints.py` 可重新挂上 `--execute` 并传 `agent_ids=` 精确删除。

---

## Org Harness

**Phase 1 Kernel shipped** (ADR-0014): `/api/v1/orgs*`, CLI `acn org …`,
minimal work + thin Loop. Design: [`docs/org-harness/`](org-harness/README.md).

Still deferred (Phase 2+):

- **DEF-WORK-PLUGIN** — Task Pool as full `IWorkPattern` plugin host
- **DEF-MEM** — Org Memory / SOPs (Pattern-local; Mem0/Zep/PG+vector)
- **DEF-ORGREP** — Cross-org reputation (beyond per-agent ERC-8004 reads)
- **DEF-DISPUTE** — Dispute / jury after escrow window (ADR-0010 Future)
- **DEF-FED** — Federation across ACN instances ([federation.md](federation.md))
- **DEF-ORGC** — Portable `/orgs/*` Core API (only if multi-Pattern discovery needs it)
- **DEF-SAGA** — Settlement saga v1 (Gated v0 atomicity)
- **DEF-RAILS** — Agentic payment rails (ADR-0009 P2)

Details: [`org-harness/org-pattern-adapter-spec-v0.md` § Deferred](org-harness/org-pattern-adapter-spec-v0.md#7-deferred-enhancements).