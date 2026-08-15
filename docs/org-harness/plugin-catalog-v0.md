# Org Harness — Plugin catalog v0（官方推荐短名单）

**Status:** Draft for operators · not a marketplace  
**Last updated:** 2026-08-15  
**Audience:** Org 创建者、Pattern 作者、ACN 维护者

> **本文是冷启动短名单，不是扩展正统。**  
> 扩展正统 = 外部 Pattern / 侧车（见 [design-v0](./design-v0.md) §0.1–0.3）；`plugins.*` Builtin = 默认电池。  
> 每个 Port 给默认 + ≤2 个推荐方向，状态：`shipped` / `adapter-planned` / `community-welcome` / `deferred`。  
> 完整 Pattern/插件宿主（发现、按 Org 启用、版本、失败隔离）→ 有多 Pattern 实需再开；**不**把第三方热加载进 ACN 进程。

相关：[design-v0.md](./design-v0.md) **§0 架构导读** · §5.3 · [org-knowledge-base-v0.md](./org-knowledge-base-v0.md) ·
[phase2-work-port-v0.md](./phase2-work-port-v0.md) · [ADR-0014](../adr/0014-org-harness-module.md)

---

## 创建 Org 时怎么选

**默认就开工（推荐大多数人）：**

```json
{
  "work": "builtin_work",
  "loop": "heartbeat",
  "memory": "noop"
}
```

`POST /api/v1/orgs` 不传 `plugins` 时也会归一成上面这组。

想少选一点、先跑通某种协作样子：看 **[org-runtime-modes-v0.md §2](./org-runtime-modes-v0.md#2-预设--自由组合)** 的 **文档预设**（ledger / corp-board / dispatch / peer-handoff）；再按需叠加外部 Pattern。预设 **不是** `plugins.*` 字段。

| 你想要… | 现在怎么做 |
|---|---|
| 组织内派活 + Paperclip Issues | **默认** `builtin_work` + 装 [`paperclip-acn-plugin`](https://github.com/acnlabs/paperclip-acn-plugin)（外部 Pattern，**不是** `plugins.work=paperclip`） |
| 面向网络发赏金 | **旁路** [org-task-bridge](./org-task-bridge-v0.md) / Org wallet；**不要**改 `plugins.work` |
| 组织知识库（**agent 主贡献**） | `plugins.knowledge=noop|git`（K3）；侧车 [`examples/org-knowledge/`](../../examples/org-knowledge/)。`llm_wiki` → K5。见 [org-knowledge-base-v0.md](./org-knowledge-base-v0.md) |
| 组织共享记忆（事实 / 叙事） | 先 `memory=noop`；需要时按下方 Memory 短名单自建侧车 |
| Task Pool 当组织工单后端 | **`deferred`**（`plugins.work=task_pool` 会 `plugin_unavailable`） |

非法或未接线的 id → API 明确报错（`unknown_plugin` / `plugin_unavailable`），不会静默忽略。

---

## 自定义规则（硬约定）

> **自定义优先走外部适配；`plugins.*` 仅官方 / 进程内实现。**

| 路径 | 谁用 | v0 事实 |
|---|---|---|
| **`org.plugins.*`（进程内 registry）** | ACN 内置 Builtin | 白名单 id only（今日：`builtin_work` / `heartbeat` / `memory=noop` / `knowledge=noop|git`）。任意第三方字符串 → 拒绝。完整第三方进程内插件 → **Phase 3**（宿主 + 信任模型）之后才谈。 |
| **外部 Pattern / 侧车** | 用户与社区的主自定义路径 | 消费 Org / work / harness 事件（见 [adapter spec](./org-pattern-adapter-spec-v0.md)），或按 `org_id` 挂 Memory/MCP 侧车。**不**要求改 `plugins.work=…`。Paperclip 即此路。 |

因此：

- 「我想换一套组织编排」→ 写/装 **外部 Pattern**，Kernel 仍用默认 `builtin_work`。  
- 「我想接 Mem0」→ **侧车** + 文档契约；等标成 `adapter-planned` 落地前，不必也不该伪造 `plugins.memory=mem0`（今日会失败）。  
- 「我想建组织知识库」→ **外部侧车**（git/SOP）；见 [org-knowledge-base-v0.md](./org-knowledge-base-v0.md)。勿伪造 `plugins.knowledge=*`。  
- 「我想把自研代码热加载进 ACN」→ **非目标**（见文末）；请走外部进程。

Port 划分（Work / Loop / Knowledge / Memory / …）是**问题轴**，充分用于架构对话；**不是**「每个轴今天都有可选货架」。v0 必要落地仍是 Kernel + Work + 薄 Loop + Events；其余槽位先占位、再官方筛选适配。

---

## 状态图例

| 状态 | 含义 |
|---|---|
| **shipped** | ACN 进程内已接线，或官方外部适配已可用 |
| **examples-shipped** | 官方 examples / smoke 已可用；进程内 `plugins.*` 未必接线 |
| **adapter-planned** | 官方打算写薄适配 / 文档契约；组件本身用现成 OSS |
| **plugin-planned** | `plugins.*` id 预留，尚未进白名单 |
| **community-welcome** | 欢迎按 Port 契约接；暂不承诺官方维护 |
| **deferred** | 已知 id 或方向，当前刻意不做 |

---

## Port 短名单

### 1. Work — `IWorkPattern`（`plugins.work`）

| id / 方向 | 状态 | 说明 |
|---|---|---|
| **`builtin_work`** | **shipped** | 默认。Org work API + `org.work_*` / tick。 |
| **Paperclip**（外部 Pattern） | **shipped** | Issues ↔ Org work；[`@acnlabs/paperclip-plugin-acn`](https://www.npmjs.com/package/@acnlabs/paperclip-plugin-acn) ≥ 0.3.5。配置 `acnOrgId`，**勿**设 `plugins.work=paperclip`。 |
| `task_pool` | **deferred** | P2b；选了会 `plugin_unavailable`。网络招人用 [publish-task 桥](./org-task-bridge-v0.md)。 |
| CrewAI / LangGraph / 自研 DAG | **community-welcome** | 按 [org-pattern-adapter-spec](./org-pattern-adapter-spec-v0.md) 消费 Core，不必进进程内 registry。 |

### 2. Loop — `IOrgLoop`（`plugins.loop`）

| id / 方向 | 状态 | 说明 |
|---|---|---|
| **`heartbeat`** | **shipped** | 默认薄 tick（`POST …/loop/tick`）。别名 `thin` → `heartbeat`。 |
| Paperclip / harness 唤醒 | **shipped**（外部） | Pattern 或 subnet harness 收 `org.*` 后驱动成员 L1；不必换 loop id。 |
| **Org 待办执行器（外部）**（任意 `spawnCommand`；ClawTeam 等为配方） | **adapter-planned**（C1–C2 in examples） | 外部 Pattern，非 `plugins.loop=*`；见 [org-loop-spawn-sidecar-poc-v0.md](./org-loop-spawn-sidecar-poc-v0.md)。 |
| **ACN Org 编排器**（叫醒成员 agent） | **adapter-planned**（P2 examples） | Loop 轴外部 Pattern；[产品定义](./org-orchestrator-v0.md) · [唤醒契约](./org-orchestrator-wake-contract-v0.md) · [`examples/org-orchestrator/`](../../examples/org-orchestrator/)。无 `plugins.loop=orchestrator`。 |
| **ClawTeam ↔ Org Loop 适配器** | **adapter-planned**（选型 only） | 编排器的**可选**执行后端：Org work ↔ CT task；见 [clawteam-org-loop-adapter-v0.md](./clawteam-org-loop-adapter-v0.md)。 |

### 3. Knowledge — `IOrgKnowledge`（`plugins.knowledge` · K3）

| id / 方向 | 状态 | 说明 |
|---|---|---|
| **`noop`** | **wired**（默认） | 不要组织知识库。 |
| **`git` / 文件侧车**（按 `org_id`） | **wired** + examples（读 K1/K2 · 写 K4） | 启用侧车契约。`read_kb` + `contribute_kb`；wake `kb_refs`。见 [org-knowledge-base-v0.md](./org-knowledge-base-v0.md)。 |
| **`llm_wiki`**（Karpathy 编译层 + 可选 Obsidian） | **plugin-unavailable**（K5） | 可选第二档；agent 维护 wiki/；须治理，不替代 charter。 |
| 外挂 KB / RAG | **community-welcome** | 成熟栈接入；ACN **不自研**引擎。 |

组织刚需；**主贡献者是 agent**；与 Memory **分开选型**。

### 4. Memory — `IOrgMemory`（`plugins.memory`）

| id / 方向 | 状态 | 说明 |
|---|---|---|
| **`noop`** | **shipped** | 默认占位：无组织共享记忆，成员靠各自 L1。 |
| **Mem0**（或兼容「事实/画像」记忆服务） | **adapter-planned** | 适合组织级「长期事实 / 偏好」侧车；官方后续薄适配，**不自研记忆引擎**。 |
| **Zep / Graphiti** 一类（时序 + 图谱） | **adapter-planned** | 适合多会话、多成员共享叙事与实体关系。 |

选型提示：先问「要的是 **SOP（→ Knowledge）**、事实，还是对话轨迹」——三类可以不同组件，不必一个 OSS 打满。

### 5. Capability — `ICapabilityPool`

| 方向 | 状态 | 说明 |
|---|---|---|
| **聚合成员 ACN skills / tags** | **adapter-planned**（Builtin） | v0 产品路径：读成员 profile，不另造目录服务。 |
| MCP catalog（外挂） | **community-welcome** | 组织级工具目录；契约稳定后再考虑官方适配。 |

今日**无** `plugins.capability` 字段强制项；能力发现仍靠成员 agent 自身。

### 6. Policy / Budget — `IPolicyBudget`

| 方向 | 状态 | 说明 |
|---|---|---|
| **Kernel 角色**（owner / manager / worker…） | **shipped** | 治理与 treasury 规则在 Kernel；见 ADR-0014。 |
| 审批流 / 月度硬停 / manager mandate | **deferred** | Org wallet 亦将 mandate 后置；有真实需求再开 Port。 |

### 7. Events — `IEventSink`

| 方向 | 状态 | 说明 |
|---|---|---|
| **subnet harness webhook** | **shipped** | `org.*` → Pattern（Paperclip 等）。 |
| **platform webhook** | **shipped** | Backend（如 `org.owner_changed` / Org wallet S5）。 |
| 多 webhook / OTel | **community-welcome** | |

---

## 旁路能力（不是 `plugins.*`，但创建 Org 常问）

写进清单以免漏掉——它们**不是** Port 插件 id。

| 能力 | 状态 | 入口 |
|---|---|---|
| Org 金库 / Org-paid 赏金 | **shipped** | [org-wallet-v0.md](./org-wallet-v0.md) · Backend topup（插件 ≥ 0.3.5 显示指引） |
| 对外 Task 发布 / 导入 | **shipped** | [org-task-bridge-v0.md](./org-task-bridge-v0.md) |
| 围栏 / harness URL | **shipped** | Kernel + subnet；本地可用 poll（Paperclip 0.3.3+） |

---

## 官方筛选标准（短）

1. **许可证与可自托管**（或有清晰托管 API）。  
2. **组织多租户**：能按 `org_id`（或等价）隔离，而不是只能单用户会话。  
3. **不强迫进 Kernel**：侧车或外部 Pattern 优先于塞进 ACN 进程。  
4. **有一条最小试用路径**（文档或脚本），否则只标 `community-welcome`。  
5. **ACN 不复刻其核心**——只做适配与推荐。

---

## 非目标

- npm/pypi「插件市场」UI  
- 热加载任意第三方代码进 ACN 进程（自定义走外部 Pattern / 侧车）  
- 把 Paperclip / Mem0 重新实现一遍  
- 在 `plugins.work` 里塞「对外赏金」（那是 Task bridge）  
- 开放任意 `plugins.*` 字符串当作「已安装插件」

---

## 修订

| 日期 | 变更 |
|---|---|
| 2026-07-25 | 初稿：冷启动短名单 + 创建 Org 选择表 |
| 2026-07-25 | 硬约定：自定义优先外部适配；`plugins.*` 仅官方/进程内 |
| 2026-07-27 | 升格 **Knowledge** Port；SOP/Skills 从 Memory 拆出；链 [org-knowledge-base-v0.md](./org-knowledge-base-v0.md) |
| 2026-07-27 | Knowledge：**examples-shipped**（K1+K2）；图例补 examples-shipped / plugin-planned |
| 2026-07-27 | Knowledge：修订为 **agent 主贡献**；货架 `git` / `llm_wiki` / `noop` |
