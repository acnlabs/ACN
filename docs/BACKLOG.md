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
- ~~**`_forward.replay()` 第二次调用返回 `http.disconnect` 偏激进**（v2 P2-A2）~~ ✅
  `_forward(scope, receive, send, body)` 增加 `receive` 参数；`replay()` 第二次调用 `await receive()` 委托原 ASGI receive，`http.disconnect` 自然由上游服务器在客户端真断时下发，符合 ASGI 规范。
- ~~**`auth_middleware.py` 用 `logging.getLogger` 而非 `structlog`**（v2 P2-A3）~~ ✅
  切到 `structlog.get_logger(__name__)`；两处 `logger.warning(..., extra={...})` 改成 kwargs 形式（`logger.warning("event", k=v)`），结构化字段不再被埋进 stdlib `LogRecord.__dict__["extra"]`。
- ~~**单元测试侧未验证 TOCTOU race**（v2 P2-A5）~~ ✅
  `tests/services/test_allowlist_service.py::test_add_capacity_trigger_propagates_under_concurrency` — `asyncio.gather` 5 路并发 `add`，mock pg_repo 模拟 trigger 行为（首条 INSERT 成功，余 4 条抛 `AllowlistCapacityExceededError`）。Pin 三个不变量：异常透传、Redis 不污染、聚合结果确定性（1 winner + 4 capacity error）。
- ~~**Trigger SQL `cap=500` 与 Python `MAX_ALLOWLIST_SIZE=500` 双源真值**（v3 P2-A6）~~ ✅
  `acn/api.py::_verify_allowlist_cap_alignment` 在 lifespan 内（仅当 PG 启用且 `AllowlistService` 已 wired）跑一次 `pg_get_functiondef('enforce_agent_allowlist_capacity'::regproc)`，正则提取 `cap CONSTANT integer := <N>`，与 Python `MAX_ALLOWLIST_SIZE` 比对：一致 → `info` log、不一致 → `warning` log（含 `sql_cap` / `python_cap` / `advice`）、查不到函数（旧 alembic head）→ `info skipped`、introspection 失败 → `warning failed`。完全 non-fatal，trigger 自身仍是 canonical guard。
- ~~**`_http_exception_handler` 的 `Retry-After` lookup 大小写敏感**（v3 P2-A7）~~ ✅
  `next((v for k, v in exc_headers.items() if k.lower() == "retry-after"), None)`。HTTP header 名 case-insensitive（RFC 9110 §5.1）的语义对齐。

### Alembic chain hygiene

Context: 修 `7ee2ed3a177c`（`expand_verification_code_to_64chars`）的 fresh-DB upgrade fail 时连带发现 — `alembic downgrade base` 全链回滚会在 initial schema 阶段炸。

- **`8d958bd38c11_initial_schema.py` participations FK 未命名导致 downgrade 不可用**（P2）
  initial schema `participations` 表通过 `sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"], ondelete="CASCADE")` 建 FK，**没有显式 `name=...`**；PG 自动生成 `participations_task_id_fkey`，但 SQLAlchemy 元数据里这个约束 `.name is None`。
  `alembic downgrade base` 在 initial schema 的 downgrade（`op.drop_table("participations")` 之前若有 drop_constraint）或 metadata 反向 emit 时报：
  `sqlalchemy.exc.CompileError: Can't emit DROP CONSTRAINT for constraint ForeignKeyConstraint(...); it has no name`
  影响：production 几乎不会用 downgrade 全链回滚，但 CI 跑 `alembic check` / 测试矩阵会卡住。
  修法：给 initial schema 的 FK 加显式 `name="participations_task_id_fkey"`，或 downgrade 链改用 `op.execute("ALTER TABLE participations DROP CONSTRAINT IF EXISTS participations_task_id_fkey")`。
  影响文件：[alembic/versions/8d958bd38c11_initial_schema.py](../alembic/versions/8d958bd38c11_initial_schema.py)。

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

- ~~`**Analytics.get_agent_activity` 的 per-agent 消息/错误计数目前恒为 `None**`~~
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

### `submit_task` 单参与者路径未走 CAS

H3 sprint 中只覆盖了 `complete_task` / `reject_task` / `cancel_task`。`submit_task` 单参与者分支（`task.submit()` 或 `task.resubmit()`）还是无条件 `await self.repository.save(task)`。两个并发 submit 会双触发 escrow.submit_v2 + 双 activity record。风险偏低（assignee 单一，并发概率小），但为状态机一致性也应该 CAS 化。

修法：取 `expected_status = IN_PROGRESS or REJECTED`（按当前 status 分支决定），然后 `compare_and_save(task, expected_status)`，CAS 输了走 `get_task` 幂等返回。

影响文件：`acn/services/task_service.py` `submit_task`。

### Dependency 阶段失败请求不被限流（H7 延伸）

H7 给所有 task 写端点挂了 `@limiter.limit` + per-identity bucketing，但 slowapi 的 `async_wrapper._check_request_limit` 是 endpoint 装饰器层 —— **dependency 阶段抛 `HTTPException` 的请求根本不会进 endpoint，limiter 不参与**。

后果：

- 攻击者反复发 invalid `acn_xxx` Bearer：`require_task_write_auth` 会触发 `agent_service.get_agent_by_api_key` 真去 DB 查（缓存只缓存有效 key，invalid key 永远穿透），可被滥用打 DB
- 同理 invalid JWT 会反复打 Auth0 JWKS 校验路径（已有缓存，但 dependency 失败请求不计 budget）
- `verify_proxy_caller` / `verify_internal_token` 同类问题

H7 范围内不修，因为本身只针对 *已认证* agent 的高频写。修法属于 middleware 层限流：

- 选项 A：用 `slowapi.middleware.SlowAPIMiddleware`（整 app 级别全局默认限流）
- 选项 B：自己写一个 `BaseHTTPMiddleware`，对所有 `/api/v1/*` 请求按 `_get_real_ip` 做粗粒度全局限流（如 600/min/IP），独立于 endpoint-level limit
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

### dict 字段单字段无独立 size cap（H6 延伸）

H6 装了 1 MiB 总 body cap，所有 string 字段也都加了 `max_length`。但 dict 字段（`metadata` / `message` / `agent_card` / `ui_spec` / `wallet_addresses` / `token_pricing` / `proof`）当前**只受总 body cap 约束**——单个 dict 字段就能占满 ~1 MiB body。

后果：
- 任意 task 创建可以塞 ~1 MiB 的 `metadata` 进 PG JSONB 字段，单条记录就吃 1 MiB 存储
- `SendMessageRequest.message` 同理可塞满 inbox（每条消息 1 MiB × inbox cap 100 = 单 agent 100 MiB Redis 占用）
- 攻击成本是 1 MiB body × N 次合法写请求

为什么暂不在 H6 修：
- 总 body cap + per-string max_length 已经挡住"single giant string field" DoS（实际更常见的攻击形态）
- per-field dict size cap 需要序列化后字节数检查，每字段一个 `field_validator`，复杂度上升
- DB 层兜底（PG JSONB 没硬上限，但可以加 CHECK constraint 或应用层 audit metric 监控异常 size）

修法选项：
- 选项 A：写一个 `validate_dict_size(max_bytes)` 复用 validator，每个关键 dict 字段挂上去（典型 64 KB / 256 KB）
- 选项 B：在 BodySizeLimit 之外加一个"per-known-field 解析后大小检查"的 ASGI middleware，反复解析 JSON 太贵
- 选项 C：把 dict 字段都改成有结构的 Pydantic submodel，用 nested `max_length` 约束每个字符串子字段

倾向：选项 A，针对 metadata/message/agent_card/ui_spec 各加 64 KB 上限，逐个评估。

影响文件：`acn/routes/tasks.py` `TaskCreateRequest.metadata` `ui_spec`、`acn/routes/communication.py` `SendMessageRequest.message` 等、`acn/routes/registry.py` `AgentJoinRequest.agent_card`。

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

### Audit fire-and-forget 缺采样限速（H-audit 延伸）

H-audit 给所有 401/403/SSRF block 都加了 `fire_and_forget_event`，但没有去重 / 采样。一个攻击者用同 IP + 同 reason 打满 1k req/s 时：

- 每个事件 ≈ 4 Redis ops（xadd + lpush + ltrim + expire），约 4k extra ops/s
- 同 IP + 同 reason 1 秒内重复落 1000 条审计日志，分析价值低，存储/带宽占满

后果：现代 Redis 顶得住，但和正常业务竞争连接池 / 影响 SLO。

修法选项：
- 选项 A：在 `record_auth_failure` 里加 in-process LRU `(ip, reason) -> last_logged_at`，1 秒内重复事件只写 1 条 + counter（counter 每 1 分钟 flush 一次）
- 选项 B：完全交给 Redis xadd 后置去重——`maxlen=100k` cap 已经能防"打爆磁盘"，前置去重只是省 ops/带宽
- 选项 C：在 audit pipeline 加 sampled aggregation（"X 次 api_key_invalid from IP Y in 1min" 一条事件）

倾向 A——in-process LRU 简单，对热点路径成本低（dict lookup），且不影响低频事件的实时性。容量 1k entries 够覆盖典型 IP 多样性。

影响文件：`acn/monitoring/audit.py` `record_auth_failure`。

### ~~`acn:audit:type:*` list 缺 expire（历史问题，H-audit 期间发现）~~ ✅ 已修（P2-#4）

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

- SCAN `acn:agents:by_erc8004_id:*` 找出所有已绑定 agent
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

### Admin bulk delete 缺 by-id 模式（C1b/H-audit 延伸）

`scan_unsafe_endpoints.py` 检测出的 unsafe agents 没法被脚本自动清扫，因为：

- `DELETE /api/v1/agents/{agent_id}` 要求 Auth0 admin JWT（不接受 internal token）—— 脚本拿不到 admin JWT
- `DELETE /api/v1/agents`（bulk）接受 internal token，但只支持 `name_prefix=` 模糊匹配（startswith），用单个 agent 的完整 name 当 prefix 会过删（比如 `name=alice` 会顺手删掉 `alice-2`）

后果：endpoint 历史清扫只能人工跑 admin JWT 的 by-id DELETE，自动化通道断了。`scan_unsafe_endpoints.py` 当前只能 scan + 报告，无法 execute。

修法选项：
- 选项 A：给 `DELETE /api/v1/agents/{agent_id}` 加上 `verify_internal_or_admin_jwt` —— 让 internal token 也能 by-id 删（与 bulk endpoint 一致）
- 选项 B：给 bulk delete 加一个新参数 `agent_ids: list[str]`（互斥于 `name_prefix`），脚本传精确 ID 列表
- 选项 C：脚本嵌入 admin JWT 颁发流程（复杂、不推荐）

倾向 B——保持现有 by-id endpoint 的 JWT 要求（admin 身份明确），新增 bulk by-id 模式给运维自动化用，audit 复用 Phase C 的 `ADMIN_BULK_DELETE` summary。

影响文件：`acn/routes/registry.py` `admin_bulk_delete_agents`（加 `agent_ids` 参数）、`acn/scripts/scan_unsafe_endpoints.py`（重新挂上 `--execute`）。