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