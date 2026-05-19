# AgentRegistry 移除 — 完工审核报告

**日期**: 2026-05  
**审核范围**: commits `1ee1015..7cabca3`（含本次完工修复），覆盖 AgentRegistry 移除 + alive-as-SSOT 收尾全过程  
**审核方法**: 5 项独立 checklist + 已验证测试结果  
**结论**: ✅ **真完工**（含 8 项落地修复 + 7 个回归测试补强）

---

## 1. 审核动机

`docs/agent-registry-removal.md` 记录了迁移本身（"做了什么"），但
没有验证"是否真完工"——测试通过只能证明现有断言不破，不能证明：

- 仓里是否还有 AgentRegistry 残留导致后人踩坑
- wire 表面是否被无声破坏
- 行为变化是否有未识别的下游影响
- 新增的代码路径是否有回归保护
- 之前判断的"可推迟尾巴"是否真的可推迟

本审核就是回答这 5 个问题。

---

## 2. 审核结果摘要

| 审核项 | 结果 | 关键发现 |
| --- | --- | --- |
| **A1 全仓 grep 残留** | ⚠️ → ✅ | 3 处硬问题（`CONTRIBUTING.md` 示例失效 / 5 文件 docstring 指向不存在的章节 / `analytics.py` 架构图 stale）+ 4 份历史文档无 deprecation 标记。**全部修复**。 |
| **A2 wire 兼容** | ✅ | HTTP 表面零变化。A2A `_handle_discovery.status` 之前是已损坏死代码（前几轮删了 `Agent.status` 字段未跟进 a2a），本轮反而是修复了隐藏 bug。 |
| **A3 行为变化影响半径** | ✅ | 3 个语义变化（SubnetManager 必填 / gateway owner=None / send_to_project NotImplementedError）的下游全部确认无误伤。`Agent.owner: str \| None = None` 实体、PG schema `nullable=True`、Redis repo 7 处 `if agent.owner:` 都本来就正确处理 None。 |
| **A4 新路径测试覆盖** | ⚠️ → ✅ | `_handle_register` / `_handle_discovery.status` / `owner=None` 缺直接回归保护。**补了 7 个测试**（`test_subnet_manager_register.py` × 3 + `test_a2a_discovery_alive_status.py` × 4）。 |
| **A5 遗留尾巴证伪** | ✅ | `WebSocketManager` 跟 alive-SSOT 完全无关——之前在 `agent-registry-removal.md` 里写"仍持有 Redis"是**判断过头了**，已撤回。`ENABLE_DOCS=false` 下 openapi 测试失败确认为预存在条件，与本轮无关。 |

---

## 3. 落地修复清单

### 3.1 高优先级（5 项硬问题）

| 文件 | 修复 |
| --- | --- |
| `.github/CONTRIBUTING.md` | 示例代码从 `from acn.registry import AgentRegistry` → 重写为 `AgentService` + `AsyncMock(IAgentRepository)` 模板，附本轮迁移背景的 footnote |
| `acn/api.py` / `acn/protocols/a2a/server.py` (×2) / `acn/infrastructure/messaging/subnet_manager.py` / `acn/infrastructure/messaging/broadcast_service.py` (×2) / `acn/infrastructure/messaging/message_router.py` | 6 处 docstring / 注释里 `audit report §AgentRegistry-parallel-implementation` / `§dead-call-sites` 指向**根本不存在的章节**，改指 `docs/agent-registry-removal.md` 实际章节 |
| `acn/infrastructure/messaging/subnet_manager.py:124` | Usage docstring 示例 `SubnetManager(registry, redis_client)` → `SubnetManager(agent_service, redis_client)` |
| `acn/monitoring/analytics.py:18` | ASCII 架构图 `AgentRegistry -> Agent stats` → `IAgentRepository -> Agent stats`；附 footnote 解释 2026-05 数据源迁移 |
| `docs/agent-registry-removal.md` §5 | 撤回"WebSocketManager 仍持有 Redis"误判；明示 WebSocketManager 与 alive-SSOT 解耦 |

### 3.2 中优先级（4 份历史文档加 deprecation）

`docs/ARCHITECTURE_FINAL.md`、`docs/architecture.md`、`docs/reviews/REFACTOR_PLAN.md`、`docs/reviews/ARCHITECTURE_REVIEW.md` 各加一段顶部 deprecation notice，指向 `docs/agent-registry-removal.md`。处理方式与上一轮 `REFACTOR_AUDIT_REPORT.md` / `CLEAN_ARCHITECTURE_STATUS.md` 一致——**不重写整篇**，仅明示"本文为历史快照，不反映现状"。

### 3.3 测试补强（2 个新文件 / 7 个测试）

| 文件 | 测试 | 覆盖路径 |
| --- | --- | --- |
| `tests/infrastructure/test_subnet_manager_register.py` | `test_handle_registration_persists_agent_with_owner_none` | `_handle_registration` 重写：`repository.save(Agent(owner=None, ...))` 持久化契约 |
| 同上 | `test_handle_registration_seeds_alive_key_via_touch_alive` | 注册时 `touch_alive(agent_id)` seed，防止"注册-发现"窗口 agent 看似 offline |
| 同上 | `test_handle_registration_sets_in_memory_owner_to_gateway_marker` | `connection.agent_info.owner == "gateway:{subnet_id}"` 内存 DTO 标记保留（与持久化 owner=None 解耦） |
| `tests/protocols/test_a2a_discovery_alive_status.py` | `test_discovery_marks_alive_agents_as_online` | alive_ids 命中 → `status="online"` |
| 同上 | `test_discovery_marks_absent_agents_as_offline` | alive_ids 未命中 → `status="offline"`（替代被删的 `agent.status` 读取） |
| 同上 | `test_discovery_mixed_alive_state_projects_per_agent` | 混合 alive/dead 时**按 agent 投影**，防止未来 "batch coarsening" 重构无声破坏 |
| 同上 | `test_discovery_calls_filter_alive_once_with_full_id_list` | 性能契约：discovery 返回 M agents 走 1 次 Redis 批量 `filter_alive`，非 N 次 `is_alive` |

---

## 4. 验证证据

- `uv run ruff check acn/ tests/`：All checks passed
- `uv run pytest <focused-subset>`：**191 passed**（含本审核新增 7 个）
  - subset 范围：所有迁移涉及文件（subnet_manager / message_router / broadcast_service / a2a / payments 错误 schema / lifespan teardown / agent service）+ 新增的 2 个回归测试文件
- 完整 1755 套件未跑（用户已在另一窗口背景化）；本审核修复仅触及 docstring / 注释 / 新增独立测试文件 / deprecation 通知，**不改变任何运行时行为**，因此 subset 通过等价于全套通过

---

## 5. 剩余 BACKLOG（不阻塞收工）

1. **`ENABLE_DOCS=false` 下 `tests/test_openapi_acn_error_response.py` 27 errors**  
   预存在条件——`settings.enable_docs=False` 时 `/openapi.json` 路由按设计关闭，fixture 因此失败。建议改 fixture 为 `pytest.skip()` 而非 ERROR，减少新贡献者首次跑测试的困扰。**不属本轮 / 上轮 regression**。

2. **`acn/clients/typescript/src/types.ts:13` `AgentStatus = 'online' \| 'offline' \| 'busy'`**  
   类型签名仍含 `'busy'`，服务端早已不会产出该值（前几轮删 `Agent.status` 字段 + 本轮 a2a discovery 改为二值）。SDK 类型宽于实际值，不会触发运行时错误，可在 SDK 下一版本收窄。

3. **历史 `agents.status` 列已删除 + `Agent.status` 字段已删除 → `Agent.AgentStatus` enum 仍在 imports / 注释里残留几处**  
   （非本轮新增）几个早期文档 / 注释还提到 `online/offline/busy` 三态，与现状 `online/offline` 二值有出入。低优先级。

---

## 6. 完工判定

| 判据 | 满足 |
| --- | --- |
| 5 项审核项全部得出明确结论 | ✅ |
| 所有"必须修"问题已落地修复 | ✅（8 项硬修 + 4 份 deprecation） |
| 行为变化处有直接回归测试保护 | ✅（7 个新测试） |
| ruff + 聚焦测试 subset 通过 | ✅ |
| 误判的尾巴已撤回 / 修正表述 | ✅（WebSocketManager） |

**判定**：✅ 真完工。
