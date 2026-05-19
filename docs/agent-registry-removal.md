# AgentRegistry 移除 & alive-as-SSOT 收尾

**日期**: 2026-05  
**范围**: commit `1ee1015..4771a1b`（7 个 commit）  
**结论**: legacy `AgentRegistry` 类彻底移除；agent 在线状态以 Redis `acn:agents:{id}:alive` 键为唯一来源。

---

## 1. 背景

ACN 此前有两条 "agent 是否在线" 的事实来源：

| 来源 | 写入路径 | 读取路径 |
| --- | --- | --- |
| PostgreSQL `agents.status` 列 | `AgentRegistry.update_status` 等 | `AgentService.search_agents` 老路径、监控仪表盘 |
| Redis `acn:agents:{id}:alive` (TTL key) | `AgentService.touch_alive`（implicit-heartbeat） | `AgentService.is_alive` |

两个来源在异常路径下会漂移（dual-source drift）：例如服务崩溃但 PG 仍记 `online`，或反过来。前几轮工作已删 `agents.status` 列 + `Agent.status` 字段，剩下的尾巴就是 `AgentRegistry` 这个老类还在 5 处调用点里被各服务持有。本轮把这条尾巴清掉。

---

## 2. 调用点迁移一览

按 commit 顺序：

| Commit | 模块 | 关键变化 |
| --- | --- | --- |
| `1ee1015` | `acn/services/agent_service.py` | 新增 `find_agent(agent_id) -> Agent \| None`，作为 `get_agent` 的非抛出兄弟。为下游 5 个调用点提供"缺席就 404"的语义，避免一律 `try/except AgentNotFoundException`。 |
| `268e99c` | `acn/routes/payments.py` | `RegistryDep` → `AgentServiceDep`；`registry.get_agent` → `agent_service.find_agent`。 |
| `2674a07` | `acn/infrastructure/messaging/message_router.py` | 构造参数 `registry: AgentRegistry` → `agent_service: AgentService`；离线预检 `agent_info.status != "online"` → `not await agent_service.is_alive(to_agent)`；`search_agents(..., status=None)` 在 service 层映射为 `"all"` 以关闭 liveness 过滤。 |
| `ad55f43` | `acn/infrastructure/messaging/broadcast_service.py` | 同上构造参数迁移；`send_by_tag` 走 `agent_service.search_agents` 且把 `status_filter=None` 翻成 `"all"`；`send_to_project` 之前是 bug-dead-code，本次改为显式 `raise NotImplementedError`，避免静悄悄返回错误结果。 |
| `8e0ece8` | `acn/infrastructure/messaging/subnet_manager.py` | `agent_service` 变**必填**构造参数；`_handle_register` 改为直接 `agent_service.repository.save(Agent(...))` + `touch_alive`，**按产品决策 `owner=None`**（gateway 注册路径与 autonomous-join 对齐，gateway-agent 进入"未认领"状态，后续可被 claim）；`_disconnect` 用 `repository.delete`；`forward_request` 用 `find_agent`。 |
| `69b9d0f` | `acn/protocols/a2a/server.py` | `ACNAgentExecutor(registry=...)` → `agent_service=...`；`_handle_discovery` 的响应 `status` 字段改由 `agent_service.repository.filter_alive` 实时计算，不再读历史字段。 |
| `4771a1b` | `acn/api.py` + `acn/routes/dependencies.py` + `acn/__init__.py` + 测试夹具 | **删除 `acn/infrastructure/persistence/redis/registry.py`**；移除 `RegistryDep` / `get_registry` / `registry_instance` 单例；`api.py` 改为 `redis_client = aioredis.from_url(...)` 独立持有 Redis 客户端，所有 25 处 `registry_instance.redis` 替换为 `redis_client`。 |

---

## 3. 重要语义变化（容易踩坑的点）

1. **`SubnetManager(agent_service=...)` 变必填**  
   以前 `agent_service` 是可选参数，缺省则降级为"无 liveness 心跳"行为；现在缺它会直接构造失败。所有自定义 manager 构造（含测试夹具）必须传入。

2. **Gateway 注册的 `owner` 改为 `None`**  
   过去 gateway 帮 agent 注册时会把 owner 设成 `f"gateway:{subnet_id}"`。新行为下，`Agent.owner = None`，而连接级的来源信息（`connection.agent_info.owner`）仍保留 `gateway:{subnet_id}` 字符串。**含义**：gateway 上来的 agent 是"未认领"的，可以后续被某个真正的用户 `claim`。这是显式产品决策（用户选 A 方案）。

3. **`BroadcastService.send_to_project` 现在抛 `NotImplementedError`**  
   原实现读的字段在 `Agent` 实体上不存在，是 dead-bug；显式抛错避免后续误用。如果业务需要按 project 广播，要重新设计 schema。

4. **`is_alive` 取代 `status == "online"`**  
   `MessageRouter` 的离线预检从读 `agent.status` 改为问 `agent_service.is_alive(agent_id)`。语义对外不变（消息进 offline inbox），但**TTL 过期后第一时间生效**，不再依赖 watchdog 把 PG 改成 offline 后才生效。

5. **`AgentService.search_agents(status=...)`**  
   传 `None` ≠ "无过滤"。`None` 在 service 层会被映射为 `"all"`（即"忽略 liveness"）。调用方如果想"只要在线"，必须显式传 `"online"`。

6. **`AgentService.find_agent` vs `get_agent`**  
   - `get_agent`：抛 `AgentNotFoundException`，适合"agent 必须存在才能继续"的业务校验
   - `find_agent`：返回 `None`，适合"缺席就 404"的路由层和"查不到就走 fallback"的路径  
   迁移本轮新增 5 个 `find_agent` 调用点（payments / message_router / subnet_manager 等），都是把 legacy `AgentRegistry.get_agent`（曾经也返回 `None`）的语义还原回来。

---

## 4. 验证

- `ruff check acn/ tests/`：All checks passed
- `ENABLE_DOCS=true uv run pytest`：**1755 passed**
- 单独冒烟测试（迁移涉及的 6 个测试文件 + lifespan + a2a + payments 错误 schema 共 192 项）：通过
- 已 rebase 到 `origin/main`（`b27c23f` ADR-0004 原子级联），无冲突。

---

## 5. 待办（不在本轮范围）

- ~~`WebSocketManager` 仍持有 Redis 但不直接读 `agent.status`；后续若有新的 alive-相关读路径，统一走 `AgentService.is_alive` / `repository.filter_alive`。~~
  **完工审核纠正**：`WebSocketManager` 与 alive-SSOT **没有耦合**——它不持有 / 不读 / 不写 agent 状态，只是 chat 频道广播层。原 TODO 是误判，已撤回。`broadcast_agent_status` 这个方法的 docstring 还提 `(online/offline/busy)` 三态，但该方法 0 调用方，是 dead code，不影响 alive-SSOT 契约。
- 测试覆盖缺口：`SubnetManager._handle_register` 重写、`a2a._handle_discovery.status` 动态算法、gateway-注册 agent `owner=None` 持久化 —— 三条新路径缺直接回归测试。完工审核轮已补 3 个测试（见 commit 历史 `test_subnet_manager_register.py` / `test_a2a_discovery_alive_status.py`）。
- `docs/REFACTOR_AUDIT_REPORT.md` 与 `docs/CLEAN_ARCHITECTURE_STATUS.md` 已标 deprecation；如要恢复成"活文档"应整篇重写而非追加补丁。
- 已知 `tests/test_openapi_acn_error_response.py` 在 `ENABLE_DOCS=false` 默认环境下失败——属预存在条件（`/openapi.json` 路由按设置关闭），与本轮无关。建议把 fixture 改为 `pytest.skip()` 而不是 ERROR，以减少新贡献者首次跑测试时的困扰。
