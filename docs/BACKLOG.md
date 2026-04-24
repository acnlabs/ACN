# BACKLOG

低优先级改进清单。非紧急，但值得做。新条目追加到对应分区末尾，做完了直接删掉或打 `[done]`。

---

## Communication

### Inbox refactor follow-ups

Context: commits `8c540a9` / `bc5b331` / `c71da67` 把消息存储从 "per-agent archive" 改为 "offline inbox"。以下是当时识别但未做的延伸优化。

- `**_store_inbox` 合并 pipeline**
当前 `zadd` + `zremrangebyrank` + `expire` 是三次独立的 Redis round-trip，可合并为单个 `pipeline()` 降低延迟。
影响文件：`[acn/infrastructure/messaging/message_router.py](../acn/infrastructure/messaging/message_router.py)` `_store_inbox()`.
- `**route()` 前置 `is_online()` 预检**
离线 agent 目前仍然会走一次 A2A HTTP 调用直到超时才进 except 写 inbox，浪费 httpx 连接和超时时间。
加一步 `registry.get_agent(to_agent).is_online()` 预检，离线直接写 inbox 并跳过 HTTP。
需要同时考虑 alive signal 的延迟（心跳 TTL 过期但 agent 实际在线）。
影响文件：`[acn/infrastructure/messaging/message_router.py](../acn/infrastructure/messaging/message_router.py)` `route()`.
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

- **`RedisTaskRepository.delete` 用 pipeline 批量取 participation**
当前对每个参与者串行 `HGETALL`（N 次 round-trip）以拿 `participant_id`。任务有几千参与者时 delete 会很慢。改为一次 pipeline 并发拉所有 participation hash，或 Lua 脚本单次返回 pid→uid 映射。
影响文件：`[acn/infrastructure/persistence/redis/task_repository.py](../acn/infrastructure/persistence/redis/task_repository.py)` `delete()`.
- **`RedisTaskRepository.delete` 的 user-index `lrem` 也放 pipeline**
同一原因：对每个 (user × pid) 对串行 `lrem`，O(user_count × pid_count)。pipeline 化即可。
影响文件：同上。

---

## Payments / Billing

### P1 sweep follow-ups

Context: commits for SCALE_AUDIT P1-4 / P1-5。

- **In-flight `PaymentTask` 永不过期的兜底清理**（P1-5 后续）
P1-5 仅给**终态**（completed/cancelled/refunded/disputed/…）的 task 加了 180 天 TTL。一个停在 `PAYMENT_PENDING` 永远不进终态的 task 仍然占用 Redis 内存。属于业务清理职责，建议加一个后台 sweeper：扫 `acn:payment_tasks:*` → 解析 `created_at` → 超过 N 天（例如 7 天）仍未离开 `PAYMENT_PENDING` / `PAYMENT_REQUESTED` 的，强制 `update_task_status(FAILED, reason="expired")`，让它走终态分支被 TTL 接管。
影响文件：`[acn/protocols/ap2/core.py](../acn/protocols/ap2/core.py)` 新增 `sweep_stale_payment_tasks()` + scheduler 接入。
- **Billing fallback 的 PG 迁移路径**（P1-4 后续）
P1-4 给 Redis fallback 加了 90 天 TTL，但这只是"不爆 Redis"的护栏，不是真源。生产部署应该强制要求 `IBillingRepository` 不为 None，并在启动时校验。考虑加一个 `BillingService.__init__` 的 strict 模式开关，或在 health check 里上报"running on Redis fallback"红灯。
影响文件：`[acn/services/billing_service.py](../acn/services/billing_service.py)`、`[acn/api.py](../acn/api.py)` 启动检查。

---

## Monitoring

### P1-2 follow-ups

- **Metrics key 的 `scan_iter` 在 `prometheus_export` / `get_all_metrics` 是 O(N_keys)**
P1-2 砍掉了 `(from_agent, to_agent)` 的高基数 label 后，稳态 key 数已经被压成可控量级，但 export 路径仍然是全扫。如果将来又因新需求长出几万个 label 组合，scan 就会拖慢 scrape。考虑维护一个 `acn:metrics:_index` set 记录所有活跃 key，export 时直接 SMEMBERS 替换 SCAN。
影响文件：`[acn/monitoring/metrics.py](../acn/monitoring/metrics.py)` `prometheus_export()` / `get_all_metrics()`.
- **Adhoc counter（`METRICS` 没声明的）仍然能无限增长 label key 集合**
`_sanitize_labels` 对未在 `METRICS` 字典里登记的 metric 名只做 charset/length 守卫，不做 key 白名单。等价于"自由 label 模式"。如果有人 `inc_counter("my_thing", labels={"user_id": ...})` 还是会 cardinality 爆炸。要么强制所有 metric 必须先注册，要么对 unknown metric 也限制 label key 数（例如最多 3 个）。

### Per-agent activity via PG `activity_events`（P1-9 后续）

- ~~**`Analytics.get_agent_activity` 的 per-agent 消息/错误计数目前恒为 `None`**~~
  **已修（Routes 契约全扫 sprint）**：`messages_sent` 和 `errors` 现在从 PG `activity_events` 聚合，`messages_received` 仍为 `None`（需要 task-join 聚合，见下条）。
  修改文件：`acn/monitoring/analytics.py` `get_agent_activity()`、`acn/services/activity_service.py` 新增 `get_activity_counts` / `get_last_activity_at`、`acn/core/interfaces/activity_repository.py` + `acn/infrastructure/persistence/postgres/activity_repository.py` 新增 `count_by_agent_and_type` / `get_last_activity_at`、`acn/api.py` 注入 `activity_service` 到 `Analytics`。

- **`messages_received` 仍需 task-join 聚合**
  `task_approved` / `task_rejected` 等 inbound 事件的 `actor_id` 是 task creator，不是被评审的 solver agent。精确计算 "收到的消息" 需要 JOIN `tasks` 表通过 `participation.agent_id` 定位 solver。暂时保持 `None` + `data_source_note` 说明。

### Analytics 的 PG 迁移方向（P2-3 延伸）

- **`get_agent_stats` / `get_subnet_stats` 的真源应在 PG 而非 Redis scan**
  P2-3 把扫描 pattern 修对了，但底层还是 `scan_iter("acn:agents:*")` + 段数过滤这种偏 workaround 的写法。Agent/Subnet 的权威数据已经在 PG `agents` / `subnets` 表里（`PostgresAgentRepository` / `PostgresSubnetRepository`）。迁移方向：把 `Analytics` 改成注入 `IAgentRepository` / `ISubnetRepository` 而不是裸 Redis，统计用 SQL `GROUP BY status / subnet_id / tags`，Redis 降级仅在"无 PG"配置下兜底。
  影响文件：`[acn/monitoring/analytics.py](../acn/monitoring/analytics.py)` 构造函数 + `get_agent_stats()` / `get_subnet_stats()`、`[acn/api.py](../acn/api.py)` 构造 Analytics 时传入 repo。

### Redis tag 索引（P2-4 延伸）

- **`find_open_tasks(tags=...)` 在 Redis 分支仍是 Python-side filter**
  P2-4 把 `TaskPool.find_tasks_for_agent` 的重复过滤层消掉了，PG 分支立刻享受原生 `required_tags @> ARRAY[...]` 的 SQL 过滤，但 Redis 分支里 `find_open_tasks` 还是 `ZREVRANGE(acn:tasks:open)` 一页 + `task.matches_tags(tags)` 在 Python 端逐条过滤。
  真正的 scale 修法：
  - `save(task)` / 状态变更时维护每个 tag 一个 `acn:tasks:by_tag:{tag}`（zset，score=created_at，member=task_id）
  - `find_open_tasks(tags=[t1, t2])` 用 `ZINTERSTORE` 或按需 `ZREVRANGEBYSCORE` 每个 tag 的 zset 后交集
  - 子网可见性维度独立，交集后再做子网过滤
  目前没赶着做的原因：Redis 分支本就是 "no-PG fallback"，生产部署会用 PG 分支（已经天然零成本过滤）；tag-index 方案比修 pattern 复杂，值得等一个真实 scale 信号再做。
  影响文件：`[acn/infrastructure/persistence/redis/task_repository.py](../acn/infrastructure/persistence/redis/task_repository.py)` `save` / `_update_status` / `find_open_tasks`。

### broadcast-by-tag 的 `total` 字段语义不准确（P3）

- **位置**：`acn/acn/routes/communication.py` — `broadcast_by_tag` 路由，第 216-238 行
- **问题**：`body.limit` 截断 `responses` 发生在 `success_count` 和 `total` 计算之前，导致响应里的 `total` 反映的是截断后的数量，而不是实际广播触达的 agent 数量。客户端无法区分"只有 N 个 agent"和"广播了更多但被截断到 N"。
- **修法**：先记录 `total_sent = len(responses)`，再截断，用 `total_sent` 作为响应里的 `total`，另加 `returned` 字段表示本次返回条数，或在文档里明确说明 `total` 是截断后计数。
- **优先级**：P3（语义问题，不影响消息实际投递）

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