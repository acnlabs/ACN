# ACN 规模化存储风险扫描报告

> 首次扫描日期：2026-04-23
> 背景：`inbox refactor`（commits `8c540a9` / `bc5b331` / `c71da67`）完成后，怀疑类似的"百万 agent 陷阱"潜伏在其他模块，系统性复查 Redis 写入、索引清理、全量加载路径，得到此报告。
> 处理中的条目请勾掉 `[ ]` → `[x]`，已完成的条目在条目末尾加 commit hash。

## 摘要

- **4 个 P0**（生产必然炸 / 确定性泄漏 / 单次操作随 N 爆炸）
- **8 个 P1**（规模化会炸 / 高流量 Redis 失控）
- **4 个 P2**（小隐患 / 代码异味 / 运维放大）

---

## P0：生产必然炸 / 已经在泄露

### [x] P0-1 任务删除未清理 participation 全量键族（确定性泄漏） ✅ 已修

- **位置**：`[acn/infrastructure/persistence/redis/task_repository.py:375-401](../acn/infrastructure/persistence/redis/task_repository.py)`
- **反模式**：`delete()` 只删 `acn:task:{id}` 主键与 `acn:task:completions:{id}`，**不删** `acn:participation:{pid}`、`acn:task:{id}:participations`、`acn:user:*:task:{id}:participations`、`acn:user:*:all_participations`、Lua 维护的 `acn:task:{id}:active_count` 等
- **规模化后果**：每任务多参与者时每次删除残留多枚 hash/zset/set/list；泄漏量 = 历史任务数 × 每任务参与量
- **触发**：`TaskPool.remove` / 任何 `ITaskRepository.delete`
- **修复方向**：`delete` 中遍历 task 下 participation 并批量 `DELETE`/`SREM`/`LREM`；或后台异步扫描回收任务前缀

### [x] P0-2 PostgreSQL 删任务不清理仍用于索引的 Redis 键（混合部署泄漏） ✅ 已修

> 自审修正：初版补丁额外显式 `DELETE FROM participations`，但数据库已通过 migration `1e400bcfd4ec` 在 `participations.task_id -> tasks.task_id` 上建立 `ON DELETE CASCADE`。最终版只 `DELETE tasks` + commit + DEL Redis 侧车，依赖 FK 级联删 participation 行，避免 Python 与 SQL 两处职责重复。

- **位置**：`[acn/infrastructure/persistence/postgres/task_repository.py:377-404](../acn/infrastructure/persistence/postgres/task_repository.py)` 的 `delete`（仅 SQL）
- **反模式**：PG 走真源但 completion 等仍写 `acn:task:completions:{id}` 到共享 Redis；`delete` 只删 PG 行不删 Redis 侧车
- **规模化后果**：per-task set，每 agent 每任务一条 member，百万 agent × 多任务 = 孤儿键持续堆积
- **触发**：`DATABASE_URL` 启用 + 共享 Redis 的推荐部署下
- **修复方向**：`PostgresTaskRepository.delete` 内显式 `UNLINK` 与 task_id 绑定的 Redis 键，与纯 Redis 仓库键族对齐

### [x] P0-3 审计「按日」索引 list 有 TTL 但无条数上限（单日撑爆单 key） ✅ 已修

- **位置**：`[acn/monitoring/audit.py:251-254](../acn/monitoring/audit.py)`
- **反模式**：`acn:audit:day:YYYYMMDD` 上 `lpush` + `expire`，没有与 `type_key` 同级的 `ltrim`
- **规模化后果**：1e6 事件/天约 32MB 仅 ID；1e7~1e8 级日流量可到单 key **数 GB**，触发 Redis 单线程热点
- **触发**：高 QPS 下 `log_event` 被频繁调用（全类型审计全开）
- **修复方向**：对 `day_key` 加 `ltrim`；或改 stream 定长环形缓冲

### [x] P0-4 心跳兜底 `mark_offline_stale` 全量加载 Agent ✅ 已修

> 自审修正 1：只改 Python 不够——`agents.status` 字段本来没有索引，`WHERE status='online' AND agent_id > $1 ORDER BY agent_id LIMIT 500` 会退化为 pkey 顺序扫，百万表上比 `find_all` 还慢。配套补了 migration `c3d4e5f6a7b8`，加 partial index `ix_agents_status_online_agent_id ON agents(agent_id) WHERE status='online'`，索引只保存在线行，查询走 index-only range scan。`AgentModel.__table_args__` 里同步声明该索引防止后续 autogenerate 把它 drop 掉。
>
> 自审修正 2（二次审核发现）：新 migration 的 `down_revision` 最初写成了 `1e400bcfd4ec`，但该 revision 上方已有 `b2c3d4e5f6a7`，这会形成分叉两个 heads，生产 `alembic upgrade head` 会直接拒跑。改为 `down_revision = 'b2c3d4e5f6a7'`，`alembic heads` 已验证回到单头。

- **位置**：`[acn/infrastructure/persistence/postgres/agent_repository.py:271-283](../acn/infrastructure/persistence/postgres/agent_repository.py)`
- **反模式**：`all_agents = await self.find_all()` 后筛选 online；等价于每 30 分钟全表扫
- **规模化后果**：百万行单次可达数 GB Python 对象 + 长事务；`api.py` 里 30 分钟周期 watchdog 触发
- **修复方向**：改为按条件分页查询 `status=online`、或只查 `last_heartbeat` 游标/时间窗

---

## P1：规模化会炸

### [x] ✅ 已修 P1-1 `acn:messages:log:{route_id}` 稳态 key 数 = 7 天消息积分

- **位置**：`[acn/infrastructure/messaging/message_router.py:386-426](../acn/infrastructure/messaging/message_router.py)`
- **反模式**：成功/失败都写一 key-per-route，7 天 TTL 只管最终消失
- **规模化后果**：100 万条/天 × 0.5–2KB 稳态 0.1–0.5GB；千万级/天 可至 TB
- **修复方向**：~~采样/聚合/stream (maxlen)；或缩短 TTL + 降采样~~
- **实际修复**：全库 grep 确认**无任何 callsite 读** `acn:messages:log:{route_id}`，它是纯调试 trace。直接把 `_log_message` 从 `SETEX per-route` 改为 `XADD acn:messages:log:stream MAXLEN ~ 100_000`。内存硬顶 ~100 MB 不随 QPS 扩张，仍可通过 `XRANGE`/`XREVRANGE` 调试。常量 `MESSAGE_LOG_STREAM_MAXLEN` 独立导出便于调优与测试断言

### [ ] P1-2 Metrics counter 使用 `(from_agent, to_agent)` 做 label（高基数键爆炸）

- **位置**：`[acn/monitoring/metrics.py:155-166, 168-172, 457-462](../acn/monitoring/metrics.py)`
- **反模式**：`acn_messages_total` 的 label 含对端 agent id，`incr` 产生 O(唯一对数) 个 key
- **规模化后果**：即使活跃对只有 1e5，数十字节/key 也可数十 MB 仅计数器；再乘错误/重试可逼近 key 数上限
- **修复方向**：label 分桶/哈希/只统计 subnet 级；外推时序库，Redis 只保留低基数聚合

### [x] ✅ 已修 P1-3 `acn:user:{id}:all_participations` 只 lpush 不 ltrim

- **位置**：`[acn/infrastructure/persistence/redis/task_repository.py](../acn/infrastructure/persistence/redis/task_repository.py)` 两个 lpush 点（`add_application`、`atomic_join_task`）
- **反模式**：每次 join 追加 participation id，与任务删除无联动
- **规模化后果**：头部用户 10^5 次参与 = 数 MB 级单 key；高活用户持续累积
- **实际修复**：模块级常量 `_ALL_PARTICIPATIONS_CAP = 500`（读路径 `limit<=50` 的 10× 余量），在**两个 lpush 写入点**之后都追加 `ltrim(0, CAP-1)`。任务删除侧的 lrem 路径在 P0-1 里已处理

### [ ] P1-4 Billing Redis 回退路径 Tx 索引与队列无 cap/TTL

- **位置**：`[acn/services/billing_service.py:529-550, 572-580](../acn/services/billing_service.py)`
- **反模式**：`_save_transaction` set；`_index_transaction` 对 per-user/per-agent list `lpush` 无 `ltrim`；`webhooks:pending` 无 trim
- **规模化后果**：无 PG 时交易持续写入则 per-user/per-agent 列表与 tx 键无限增长；1KB × 百万笔 ≈ 1GB 不含索引
- **修复方向**：以 PG 为唯一真源；或对 Redis 回退加 cap/TTL/异步刷盘

### [ ] P1-5 AP2 `PaymentTaskManager` 任务与审计 list 无删除 / 无 TTL

- **位置**：`[acn/protocols/ap2/core.py:867-880, 882-890](../acn/protocols/ap2/core.py)`
- **反模式**：`acn:payment_tasks:{id}` 无 expire；`acn:payment_tasks:audit:{task_id}` list 无 `ltrim` 无按任务删配套
- **规模化后果**：支付任务长期增长 → 字符串 + 两维 agent 索引 set + 无界 audit list，长时间运行可 GB–TB
- **修复方向**：终态后 TTL；审计 list cap；或迁 stream + 修剪

### [x] ✅ 已修 P1-6 `acn:subnets:all` 只读不写，功能层面的 bug

- **位置**：`[task_repository.py `find_open_tasks`](../acn/infrastructure/persistence/redis/task_repository.py)`
- **反模式**：`smembers("acn:subnets:all")` 永远空；而且 hash 用的是错 key (`acn:subnet:{sid}` 应为 `acn:subnets:info:{sid}`)。两个 bug 叠加使 `visible_subnet_ids` 永远为空，所有带 `subnet_id` 的 task 对所有 agent 都不可见
- **规模化后果**：功能层面"规模化等于全挂"
- **实际修复**：放弃新增索引路线（避免双写漂移）。可见 subnet 等价于 "agent 自己的 `subnet_ids`"，直接 `HGET acn:agents:{uid} subnet_ids` + `json.loads` → `set`。一次 HGET 替换了原来的 O(N_subnets) 扫描，兼做 bug fix 和性能优化。JSON 损坏兜底到空集合（隐藏 private task，不 500）

### [x] ✅ 已修 P1-7 `RedisAgentRepository.delete` 未删 `acn:agents:by_erc8004_id` 与 alive

- **位置**：`[agent_repository.py `delete`](../acn/infrastructure/persistence/redis/agent_repository.py)`
- **反模式**：链上绑定反向索引是永久 string，未在 delete 时清；alive 虽然 90s TTL，但窗口内 `filter_alive`/`mark_offline_stale` 仍会"看到"已删 agent
- **规模化后果**：删号重建/迁移时残留索引阻止新号绑定；每删一次泄漏 1 string
- **实际修复**：`delete` 里追加两步：只有 `agent.erc8004_agent_id` 存在才 `DEL acn:agents:by_erc8004_id:{token_id}`（避免 stomp 空字符串 key）；总是 `DEL acn:agents:{id}:alive` 让活性信号即时清零
- **修复方向**：delete 中 `delete(acn:agents:by_erc8004_id:{id})`（有值时）

### [ ] P1-8 监控/审计导出 `scan_iter` 对全 key 扫描（运维面爆炸）

- **位置**：`[monitoring/metrics.py:360-361](../acn/monitoring/metrics.py)`（Prometheus export）；`[monitoring/analytics.py:69-90, 154-177](../acn/monitoring/analytics.py)`
- **反模式**：`scan_iter(acn:metrics:*)`、按 agent 扫消息计数
- **规模化后果**：百万 key 时单次 Prometheus 拉取分钟级，阻塞 Redis 单线程
- **修复方向**：白名单指标、分片缓存、或仅增量维护 exposition 文本

---

## P2：小隐患 / 代码异味

### [ ] P2-1 Lifespan 未关闭 WebSocket / Webhook

- **位置**：`[acn/api.py:255-262](../acn/api.py)`；`[infrastructure/messaging/websocket_manager.py:133-155](../acn/infrastructure/messaging/websocket_manager.py)`（有 `stop()` 但未被调用）；`[protocols/ap2/webhook.py:143-152](../acn/protocols/ap2/webhook.py)`
- **修复方向**：lifespan teardown 顺序 `await ws_manager.stop()`、`webhook_service.stop()`

### [ ] P2-2 `MessageRouter.register_handler` 只追加无注销无上限

- **位置**：`[acn/infrastructure/messaging/message_router.py:298-313](../acn/infrastructure/messaging/message_router.py)`
- **修复方向**：覆盖策略或 per-type 最大 handler 数

### [ ] P2-3 `Analytics.get_agent_stats` 扫描的 key 模式与真实 schema 不符

- **位置**：`[acn/monitoring/analytics.py:81-85](../acn/monitoring/analytics.py)` 使用 `acn:agents:*:info`，主仓库是 `acn:agents:{uuid}`
- **修复方向**：对齐 schema 或改为 PG 读

### [ ] P2-4 `TaskPool.find_tasks_for_agent` 过度拉取后 Python 侧过滤

- **位置**：`[acn/infrastructure/task_pool.py:129-134](../acn/infrastructure/task_pool.py)`
- **修复方向**：仓库层用 tag 索引先过滤

---

## 对比表：inbox 重构已修 vs 本次新发现


| 反模式                           | inbox 重构已修           | 新发现位置                                                                   |
| ----------------------------- | -------------------- | ----------------------------------------------------------------------- |
| 每 agent 一个 key，无 cap/TTL/删除清理 | 是（cap+TTL+delete 清理） | P1-3 `all_participations`、P1-5 AP2 audit                                |
| 每消息写双方 sorted set 历史          | 是                    | ~~P1-1 `acn:messages:log:{route_id}` 仍为 key-per-route~~ ✅ 改用 capped stream |
| 全局 list 无上限                   | DLQ 已 ltrim          | P0-3 `acn:audit:day:`* 日 list 无 trim                                    |
| 实体删了侧车存储还在                    | 若指 inbox             | P0-1 RedisTaskRepository、P0-2 PG→Redis completions、P1-7 `by_erc8004_id` |
| 指标/审计高基数 label                | 部分                   | P1-2 metrics 按 agent 对端打标                                               |


---

## 建议处理顺序

1. **P0-1 + P0-2（任务删除的两处侧车泄漏）**：与刚完成的 inbox 是同一种病，统一按"delete 清理侧车"模式修
2. **P0-3（audit 日 list 加 ltrim）**：改动最小、生产风险最大、单改一行
3. **P0-4（`mark_offline_stale` 分页）**：百万 agent 前置条件，影响 watchdog 稳定性
4. **P1-1 + P1-2（消息日志 key 数 + metrics 高基数）**：从架构上把"日志与指标"从"无限 key"解耦
5. **P1-4 + P1-5（Billing/AP2 list cap）**：在高支付业务打开前必须收紧
6. **P1-6 + P1-7（子网全集 + erc8004 删除）**：语义正确性修正
7. **P2 批次**：统一一次 lifespan 清理 + 小改动收尾

