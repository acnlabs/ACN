# BACKLOG

低优先级改进清单。非紧急，但值得做。新条目追加到对应分区末尾，做完了直接删掉或打 `[done]`。

---

## Communication

### Inbox refactor follow-ups

Context: commits `8c540a9` / `bc5b331` / `c71da67` 把消息存储从 "per-agent archive" 改为 "offline inbox"。以下是当时识别但未做的延伸优化。

- `**_store_inbox` 合并 pipeline**
当前 `zadd` + `zremrangebyrank` + `expire` 是三次独立的 Redis round-trip，可合并为单个 `pipeline()` 降低延迟。
影响文件：`[acn/infrastructure/messaging/message_router.py](../acn/infrastructure/messaging/message_router.py)` `_store_inbox()`.
- ~~`**route()` 前置 `is_online()` 预检**~~
**已修**：`route()` 在 `get_agent()` 返回的 `agent_info.status` 非 `"online"` 时立即 short-circuit：写 inbox、返回 `{"status": "inbox", "route_id": ...}`，不打开 HTTP 连接、不写 DLQ。零额外 Redis round-trip（status 已随 `get_agent()` 一并读取）。心跳 TTL 延迟导致误判的场景见代码注释。`retry_dlq` 自动受益（调用 `route()`）。测试：`tests/infrastructure/test_message_router_inbox.py::TestOfflinePrecheck`（4 tests）。
- **按 `route_id` 精准 ack**
当前 `?ack=true` 是"全清"粗粒度，agent 若用较小 `limit` 分批拉取会丢数据。
新增 `POST /history/{agent_id}/ack` 接口，body 接收 `route_ids: list[str]`，服务端按 member 精确 `zrem`。
向后兼容：`?ack=true` 保留，语义不变。
影响文件：`[acn/routes/communication.py](../acn/routes/communication.py)`, `[acn/services/message_service.py](../acn/services/message_service.py)`, `[acn/infrastructure/messaging/message_router.py](../acn/infrastructure/messaging/message_router.py)`.

### Legacy key cleanup

- **清理 `acn:messages:agent:` 遗留 key**
旧代码向每个 agent 的 sorted set 双写消息历史，新代码不再写但也不主动清。生产环境这些 key 会一直占着 Redis 内存直到手动 `FLUSHDB`。
写一次性清理脚本：`SCAN 0 MATCH acn:messages:agent:* COUNT 1000` + `UNLINK` 每批，放到 `acn/scripts/`。
- **清理 `acn:messages:log:{route_id}` 遗留 key**（P1-1 后续）
切换到 `acn:messages:log:stream` 后，旧的 per-route 字符串 key 会靠自带的 7 天 TTL 自然消失，但想立刻回收内存可以跑一次性脚本：`SCAN 0 MATCH acn:messages:log:* COUNT 1000` → 过滤掉 `stream` 这个字面 key → `UNLINK` 每批。

---

## Task / Agent

### P0 sweep follow-ups

Context: commits for SCALE_AUDIT P0-1..P0-4。完成了正确性修复，留下性能/清理项。

- `**RedisTaskRepository.delete` 用 pipeline 批量取 participation**
当前对每个参与者串行 `HGETALL`（N 次 round-trip）以拿 `participant_id`。任务有几千参与者时 delete 会很慢。改为一次 pipeline 并发拉所有 participation hash，或 Lua 脚本单次返回 pid→uid 映射。
影响文件：`[acn/infrastructure/persistence/redis/task_repository.py](../acn/infrastructure/persistence/redis/task_repository.py)` `delete()`.
- `**RedisTaskRepository.delete` 的 user-index `lrem` 也放 pipeline**
同一原因：对每个 (user × pid) 对串行 `lrem`，O(user_count × pid_count)。pipeline 化即可。
影响文件：同上。

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
- **Adhoc counter（`METRICS` 没声明的）仍然能无限增长 label key 集合**
`_sanitize_labels` 对未在 `METRICS` 字典里登记的 metric 名只做 charset/length 守卫，不做 key 白名单。等价于"自由 label 模式"。如果有人 `inc_counter("my_thing", labels={"user_id": ...})` 还是会 cardinality 爆炸。要么强制所有 metric 必须先注册，要么对 unknown metric 也限制 label key 数（例如最多 3 个）。

### Per-agent activity via PG `activity_events`（P1-9 后续）

- ~~`**Analytics.get_agent_activity` 的 per-agent 消息/错误计数目前恒为 `None**`~~
**已修（Routes 契约全扫 sprint）**：`messages_sent` 和 `errors` 现在从 PG `activity_events` 聚合，`messages_received` 仍为 `None`（需要 task-join 聚合，见下条）。
修改文件：`acn/monitoring/analytics.py` `get_agent_activity()`、`acn/services/activity_service.py` 新增 `get_activity_counts` / `get_last_activity_at`、`acn/core/interfaces/activity_repository.py` + `acn/infrastructure/persistence/postgres/activity_repository.py` 新增 `count_by_agent_and_type` / `get_last_activity_at`、`acn/api.py` 注入 `activity_service` 到 `Analytics`。
- ~~`**messages_received` 仍需 task-join 聚合**~~ ✅ 已修（比 BACKLOG 预期简单）
`task_approved` / `task_rejected` 事件在 `event_metadata["agent_id"]` 里已存有 target agent 的 ID，无需 JOIN `participations`。
`IActivityRepository.count_received_by_agent` + `PostgresActivityRepository` 实现 + `ActivityService.get_received_count` + `Analytics.get_agent_activity` 现返回 `messages_received: int | None`。
未覆盖：`task_cancelled` inbound（creator 取消）仍无 metadata.agent_id，影响可接受（取消已计入 `errors`）。

### Analytics 的 PG 迁移方向（P2-3 延伸）

- `**get_agent_stats` / `get_subnet_stats` 的真源应在 PG 而非 Redis scan**
P2-3 把扫描 pattern 修对了，但底层还是 `scan_iter("acn:agents:*")` + 段数过滤这种偏 workaround 的写法。Agent/Subnet 的权威数据已经在 PG `agents` / `subnets` 表里（`PostgresAgentRepository` / `PostgresSubnetRepository`）。迁移方向：把 `Analytics` 改成注入 `IAgentRepository` / `ISubnetRepository` 而不是裸 Redis，统计用 SQL `GROUP BY status / subnet_id / tags`，Redis 降级仅在"无 PG"配置下兜底。
影响文件：`[acn/monitoring/analytics.py](../acn/monitoring/analytics.py)` 构造函数 + `get_agent_stats()` / `get_subnet_stats()`、`[acn/api.py](../acn/api.py)` 构造 Analytics 时传入 repo。

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