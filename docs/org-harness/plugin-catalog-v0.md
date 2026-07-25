# Org Harness — Plugin catalog v0（官方推荐短名单）

**Status:** Draft for operators · not a marketplace  
**Last updated:** 2026-07-25  
**Audience:** Org 创建者、Pattern 作者、ACN 维护者

> **官方先筛一波，不自研全家桶。**  
> 每个 Port 给默认 + ≤2 个推荐方向，状态写清楚：  
> `shipped` / `adapter-planned` / `community-welcome` / `deferred`。  
> 完整插件宿主（发现、版本、热加载）→ Phase 3；本文只解决冷启动。

相关：[design-v0.md](./design-v0.md) §5.3 · [phase2-work-port-v0.md](./phase2-work-port-v0.md) ·
[ADR-0014](../adr/0014-org-harness-module.md)

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

| 你想要… | 现在怎么做 |
|---|---|
| 组织内派活 + Paperclip Issues | **默认** `builtin_work` + 装 [`paperclip-acn-plugin`](https://github.com/acnlabs/paperclip-acn-plugin)（外部 Pattern，**不是** `plugins.work=paperclip`） |
| 面向网络发赏金 | **旁路** [org-task-bridge](./org-task-bridge-v0.md) / Org wallet；**不要**改 `plugins.work` |
| 组织共享记忆 / SOP | 先 `noop`；需要时按下方 Memory 短名单自建侧车，等 `adapter-planned` |
| Task Pool 当组织工单后端 | **`deferred`**（`plugins.work=task_pool` 会 `plugin_unavailable`） |

非法或未接线的 id → API 明确报错（`unknown_plugin` / `plugin_unavailable`），不会静默忽略。

---

## 自定义规则（硬约定）

> **自定义优先走外部适配；`plugins.*` 仅官方 / 进程内实现。**

| 路径 | 谁用 | v0 事实 |
|---|---|---|
| **`org.plugins.*`（进程内 registry）** | ACN 内置 Builtin | 白名单 id only（今日：`builtin_work` / `heartbeat` / `noop`）。任意第三方字符串 → 拒绝。完整第三方进程内插件 → **Phase 3**（宿主 + 信任模型）之后才谈。 |
| **外部 Pattern / 侧车** | 用户与社区的主自定义路径 | 消费 Org / work / harness 事件（见 [adapter spec](./org-pattern-adapter-spec-v0.md)），或按 `org_id` 挂 Memory/MCP 侧车。**不**要求改 `plugins.work=…`。Paperclip 即此路。 |

因此：

- 「我想换一套组织编排」→ 写/装 **外部 Pattern**，Kernel 仍用默认 `builtin_work`。  
- 「我想接 Mem0」→ **侧车** + 文档契约；等标成 `adapter-planned` 落地前，不必也不该伪造 `plugins.memory=mem0`（今日会失败）。  
- 「我想把自研代码热加载进 ACN」→ **非目标**（见文末）；请走外部进程。

Port 划分（Work / Loop / Memory / …）是**问题轴**，充分用于架构对话；**不是**「每个轴今天都有可选货架」。v0 必要落地仍是 Kernel + Work + 薄 Loop + Events；其余槽位先占位、再官方筛选适配。

---

## 状态图例

| 状态 | 含义 |
|---|---|
| **shipped** | ACN 进程内已接线，或官方外部适配已可用 |
| **adapter-planned** | 官方打算写薄适配 / 文档契约；组件本身用现成 OSS |
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
| ClawTeam / 自定义巡检 | **adapter-planned**（选型） | 外部侧车，非 `plugins.loop=*`；见 [clawteam-loop-adapter-poc-v0.md](./clawteam-loop-adapter-poc-v0.md)。 |

### 3. Memory — `IOrgMemory`（`plugins.memory`）

| id / 方向 | 状态 | 说明 |
|---|---|---|
| **`noop`** | **shipped** | 默认占位：无组织共享记忆，成员靠各自 L1。 |
| **Mem0**（或兼容「事实/画像」记忆服务） | **adapter-planned** | 适合组织级「长期事实 / 偏好」侧车；官方后续薄适配，**不自研记忆引擎**。 |
| **Zep / Graphiti** 一类（时序 + 图谱） | **adapter-planned** | 适合多会话、多成员共享叙事与实体关系。 |
| SOP / Skills 包（文件或 git） | **community-welcome** | 流程文档当只读输入给 Work/Loop；可与向量记忆分离（见 design-v0）。 |

选型提示：先问「要记住的是事实、对话轨迹，还是 SOP」——三类可以不同组件，不必一个 OSS 打满。

### 4. Capability — `ICapabilityPool`

| 方向 | 状态 | 说明 |
|---|---|---|
| **聚合成员 ACN skills / tags** | **adapter-planned**（Builtin） | v0 产品路径：读成员 profile，不另造目录服务。 |
| MCP catalog（外挂） | **community-welcome** | 组织级工具目录；契约稳定后再考虑官方适配。 |

今日**无** `plugins.capability` 字段强制项；能力发现仍靠成员 agent 自身。

### 5. Policy / Budget — `IPolicyBudget`

| 方向 | 状态 | 说明 |
|---|---|---|
| **Kernel 角色**（owner / manager / worker…） | **shipped** | 治理与 treasury 规则在 Kernel；见 ADR-0014。 |
| 审批流 / 月度硬停 / manager mandate | **deferred** | Org wallet 亦将 mandate 后置；有真实需求再开 Port。 |

### 6. Events — `IEventSink`

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
