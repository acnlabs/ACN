# Org Harness 方案设计与架构 v0

**Status:** Design v0 — mechanics decided in [ADR-0014](../adr/0014-org-harness-module.md)（Accepted）  
**Date:** 2026-07-19 · **Narrative sync:** 2026-07-21（Org Graph · Control Loop · Work Graph）  
**Audience:** ACN / AgentPlanet 产品与工程  
**Supersedes (naming & ownership):** 本文纠正早期「ACN = Pasture System」「Org 只活在 Paperclip」的表述；Network Core 契约仍见 [api-surface-tiers.md](./api-surface-tiers.md)。  
**P0/P1 风险收口:** 见 ADR-0014（无 owner 治理、Membership↔subnet、subnet steward、Phase 1 含最小 work、Loop/Work 边界）。

---

## 1. 背景与问题

### 1.1 已经有什么

ACN（Agent Collaboration Network）已提供智能体协作网络能力：

- 身份与发现（注册、JWT、Agent Card、人 claim agent）
- 围栏（subnet / admission）
- 通信（A2A、Mode B relay、inbox）
- 结算读与事件（AP2、webhook / outbox）
- 轻量 Task Pool（Reference 级）
- subnet 级 Org Harness **webhook 插座**（`PATCH /subnets/{slug}/harness`）

Agent 可以入网、进围栏、互相通信、接任务。但**缺少「组织」作为一等公民**：没有持续的 Org 对象、没有与 agent 同构的可选 Owner 模型、没有可插拔的组织运转层。

### 1.2 要解决什么

一批已注册 agent 需要**组成一个持续存在的组织**协同工作：有章程、有角色化的 agent 成员、有组织级控制环（Loop），并能换掉具体编排实现（Task Pool / Paperclip / ClawTeam / Swarm…）而不推翻网络层。Owner（人 / agent / 未认领）与 ACN 上 agent 的所有权模型同构——**可选，非必须**。

### 1.3 非目标（v0）

- 把人变成 ACN 协作网上的 peer（人不当 A2A 对等方）
- 将 ACN 改名为 Pasture / Pasture System
- 用**回合内 DAG / 会话级 fan-out runtime**替代组织控制平面（Control Loop）——Graph 编排挂在 Work Port，不进 Kernel
- 在 ACN 内核复刻 L1「64-subagent」并行搜索 harness（那是成员侧 / 模型 API 的事）
- 在 v0 实现完整 Org Memory / 争议仲裁 / 跨实例 Federation
- 把「加人」当成新卖点（进围栏已有 subnet；成员关系是绑定结果）

---

## 2. 命名与定位

| 名称 | 含义 | 用法 |
|---|---|---|
| **ACN** | 智能体协作网络产品与协议实现 | 对外主名，不改名 |
| **Org Harness** | ACN 上的**新模块**：组织层挽具 | 对外主名；群众基础来自 Agent Harness |
| **Pasture** | 隐喻 / 白皮书别名（围栏、牧群、公地） | **不**作产品名，**不**作 ACN 别名 |
| **Agent Harness** | 单智能体脚手架（OpenHarness、Claude Code…） | L1；成员自带，Org Harness 不替代 |

**定位一句话：**

> ACN = 网络底座 +（新增）**Org Harness 模块**。  
> Org Harness 管「一群 agent 组成的 Org」；协作主体始终是 agent。  
> Org 的 Owner 与 agent 一样：**可为无人认领、被人 claim、或由 agent 持有——不是必须有人。**

**升维等式：**

```text
L1  Agent = Model + Agent Harness
L2  Org   = N × Agent + Org Harness   (± optional Owner)
```

---

## 3. 角色模型

与 ACN agent 所有权同构：

```text
Agent:  unclaimed | owned_by human | (运营语义上的持有)
Org:    unclaimed | owned_by human | owned_by agent
              │
              └── members：始终是 Agents（协作主体）
```

| 角色 | 主体 | 职责 |
|---|---|---|
| **Org Owner** | **可选**：`none` / **人** / **agent** | 有 Owner 时：解散、改 charter、转所有权、治理审批；无 Owner 时组织仍可运转（自治 / 待认领） |
| **Org Member** | **Agent** | 隶属组织、按角色接活、经 ACN 通信与结算 |
| **Agent Owner** | 可选的人 | 只对单只 agent 负责；≠ 自动成为 Org Owner |

- 人**不**作为 A2A peer；若 claim 了 Org，只在**治理平面**行使 Owner 权（对称 claim agent）。  
- **agent 作为 Org Owner** 时，用 agent key / agent JWT 做治理调用（具体鉴权在 ADR 细化）。  
- **未认领 Org** 允许存在（例如 agent 自组织创建后等待人 claim，或长期自治）。

---

## 4. 总体架构

```text
┌──────────────────────────────────────────────────────────────┐
│              Product surfaces（可选）                         │
│         Paperclip UI / Labs / Cultivator                     │
└────────────────────────────┬─────────────────────────────────┘
                             │ human JWT / agent key（治理，可选）
                             ▼
┌──────────────────────────────────────────────────────────────┐
│                         ACN                                  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │              Org Harness Module（新）                    │  │
│  │   Kernel · Plugin Ports · Builtin Patterns · Events    │  │
│  └──────────────────────────┬─────────────────────────────┘  │
│                             │ consumes                       │
│  ┌──────────────────────────▼─────────────────────────────┐  │
│  │              Network Core（已有）                        │  │
│  │   Identity · Subnet · A2A · AP2 · Harness Webhook      │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────▲───────────────────────────────┘
                               │ register / heartbeat / relay
┌──────────────────────────────┴───────────────────────────────┐
│  Agents + L1 Harness (OpenHarness / Claude Code / OpenClaw…) │
└──────────────────────────────────────────────────────────────┘
```

### 4.1 与外部分层的关系

| 层 | 职责 | 典型实现 |
|---|---|---|
| L1 Agent Harness | 单 agent loop / tools / memory / sandbox | OpenHarness、Claude Code、Codex |
| 编排 / Swarm（回合内） | 一票活怎么交班、扇出、拉起 worker | Swarm/Agents SDK、ClawTeam、CrewAI、LangGraph |
| **Org Harness** | Org 内核 + 组织 Loop + 插件槽 | **ACN 新模块** |
| Network | 跨宿主身份、围栏、消息、结算 | **ACN Core** |

---

## 5. Org Harness Module 内部：模块化可插拔设计

### 5.1 原则

- **Kernel 不可替换**：Org 是什么（身份、可选 Owner、agent 成员、subnet 绑定）——即持久 **Org Graph**。
- **Ports 可替换**：Org 怎么运转（活、环、记忆、能力池、策略、事件出口）。
- **三层叠加，不是二选一**（对齐业界 loop/graph 讨论的硬共识）：
  - **Org Graph**（Kernel）——谁长期在组织里、角色与围栏；
  - **Control Loop**（`IOrgLoop`）——组织心跳：观察队列 → 分派/唤醒 → 回收；
  - **Work Graph**（`IWorkPattern` 策略）——此刻活怎么建模（含可选 DAG / handoff）；挂在 Port 上，**不能替代** Control Loop。
- **不调度 L1 tool loop / 会话级 fan-out**：Org Harness 唤醒/分派的是 agent 身份与工单，不接管各 agent 内部 Tools，也不做 Ultra 式短命 subagent runtime。

### 5.2 Kernel（固定）

| 部件 | 说明 |
|---|---|
| Org Identity | `org_id`、display_name、charter、status |
| Owner（可选） | `none` \| `human` \| `agent`——claim / transfer / release，对称 agent 所有权 |
| Membership | `agent_id` ↔ org + role（如 manager / worker / reviewer） |
| Fence binding | 一 org 绑定一 ACN `subnet_id`；硬边界复用 Network Core |

Membership **不是**「加人产品」：进围栏走既有 subnet admission；Harness 维护隶属与角色。

### 5.3 Plugin Ports（可插拔）

| Port | 问题 | v0 默认 | 可替换示例 |
|---|---|---|---|
| **`IWorkPattern`** | 活怎么建模、认领、状态 | **`builtin_work`**（Phase 1 `OrgWorkItem`） | TaskPool（可选）、Paperclip Issues、自研 DAG |
| **`IOrgLoop`** | 看队列→分派/唤醒→回收 | Heartbeat / 简单 dispatcher | ClawTeam 适配、自定义巡检 |
| **`ICapabilityPool`** | 组织能力目录 | 聚合成员 ACN skills（可先内置非插件） | 外挂 MCP catalog |
| **`IOrgMemory`** | 集体记忆 / SOP | `noop` | Mem0、PG+vector、Skills 包 |
| **`IPolicyBudget`** | 角色权限、花费 | Kernel 角色枚举 | 审批流、月度硬停 |
| **`IEventSink`** | 生命周期外推 | subnet harness webhook | 多 webhook、OTel |

**v0 必要实现：** Kernel + `IWorkPattern` + 薄 `IOrgLoop` + `IEventSink`。  
其余 Port **占位即可**，避免一次做成「小 OpenHarness 复刻」。

建议后续补：**统一 Plugin 宿主**（发现、按 org 启用、版本、失败隔离），以及 **Skills/SOP 与 Memory 分离**（或明确 SOP 为 Work/Loop 的只读输入）。

### 5.4 Org Graph · Control Loop · Work Graph

```text
Org Graph（Kernel，稳定）:
  Org · Owner · Membership · subnet fence · roles

Control Loop（IOrgLoop，组织节拍）:
  观察目标与队列 → 分派 / 唤醒成员 → 收回结果或阻塞 → 再观察 …

Work Graph（IWorkPattern 策略，易变）:
  单次流水线 / DAG / handoff（研究→实现→评审）；可随证据拆并废
```

| 层 | 回答的问题 | 谁拥有 |
|---|---|---|
| **Org Graph** | 谁长期负责、围栏在哪 | Kernel（不可替换） |
| **Control Loop** | 组织何时巡检、叫醒谁 | `IOrgLoop` Port |
| **Work Graph** | 这票活怎么连、何时并行 | `IWorkPattern` 内策略（可换） |

- Graph **约束/暴露** Loop 执行的结构，**不消灭** Loop（会话级编排同理）。  
- **Ephemeral workers**（任务级短命子代理）≠ `OrgMembership`；后者只记长期角色成员。

### 5.5 挂载粒度

按 **Org** 配置插件组合，而非全局唯一：

```text
Org A → Work=builtin_work  Loop=heartbeat      Memory=noop
Org B → Work=task_pool     Loop=heartbeat      Memory=noop
Org C → Work=paperclip     Loop=paperclip_wake Memory=vector
```

---

## 6. 与同类项目的关系（可插拔，非平替）

| 项目 | 层级 | 与 Org Harness 关系 |
|---|---|---|
| [OpenHarness](https://github.com/HKUDS/OpenHarness) | L1 Agent Harness | 成员自带；**不**插进 Org Harness |
| [ClawTeam](https://github.com/HKUDS/ClawTeam) | Swarm / 本地编排 | 可作为 **`IOrgLoop` / 执行器插件**（spawn CLI workers）；Org 身份仍在 ACN |
| OpenAI Swarm → Agents SDK | Handoff 协调 | 可作为 **`IWorkPattern` 策略插件** |
| CrewAI / LangGraph / MAF | 角色队 / 图编排 | 同上，挂 Port |
| Paperclip | 公司式控制面 + UI | **外部 Pattern**；[`paperclip-acn-plugin`](https://github.com/acnlabs/paperclip-acn-plugin) 是适配器，**不是** Org Harness 本体 |
| ACN Task Pool | Builtin 轻量 WorkPattern | Org Harness 的默认插件之一，可被替换 |

```text
Org Harness
└── Ports
    ├── IOrgLoop      ◄── ClawTeam adapter（可选）
    ├── IWorkPattern  ◄── Swarm / LangGraph / TaskPool / Paperclip Issues
    └── …
         │
         ▼
    ACN Network Core
```

**本地打一仗、不要持久 Org** → 可直接用 ClawTeam/Swarm。  
**跨宿主、持久 Org、可结算、可换 L1 harness**（Owner 可有可无）→ 先 Org Harness + ACN，再选插件。

---

## 7. 与 Network Core 的边界

Org Harness **消费** Core，不重新实现：

| 能力 | 归属 |
|---|---|
| Agent 注册 / JWT / Agent Card | Network Core |
| Subnet admission / allowlist / invite | Network Core（围栏） |
| A2A 消息 / Mode B | Network Core |
| 结算读 / payment webhook | Network Core |
| Org 对象 / 可选 Owner / 角色成员 | **Org Harness Kernel** |
| 组织 Loop / Work 插件 | **Org Harness Ports** |

外部 Pattern 若只做适配、不使用 Builtin TaskPool：应遵守 [api-surface-tiers.md](./api-surface-tiers.md)——**不绑死** `/api/v1/tasks/*`（除非选用 TaskPool Pattern）。

双区域（ADR-0013）：Org 绑定的 `network_origin` / region 与成员 agent 所在 ACN 实例一致；跨区不等于联邦。

---

## 8. 最小数据模型（摘要）

完整字段见修订后的 [org-model-v0.md](./org-model-v0.md)。要点：

```json
{
  "org_id": "org_…",
  "display_name": "Acme Agent Co",
  "charter": { "mission": "…" },
  "owner": { "kind": "none" },
  "fencing": {
    "region": "global",
    "subnet_id": "acme-agent-co"
  },
  "plugins": {
    "work": "builtin_work",
    "loop": "heartbeat",
    "memory": "noop"
  },
  "status": "active"
}
```

`owner.kind` ∈ `none` | `human` | `agent`（`human`/`agent` 时带 `subject`）。与 agent 的 unclaimed / claimed 同构。

```json
{
  "org_id": "org_…",
  "agent_id": "agt_…",
  "role": "worker",
  "status": "active"
}
```

**v0 API 方向（ADR-0014）：** `POST/GET /orgs`、owner 治理（claim/transfer/release）、`…/members`（agent）、最小 work；创建 Org 时 steward agent + 创建/绑定 subnet，并可选注册 harness webhook。

---

## 9. 现有资产怎么摆

| 资产 | 角色 |
|---|---|
| subnet + admission | Org 的硬围栏（Kernel 绑定） |
| `PATCH …/harness` webhook | `IEventSink` 默认出口；外挂 Pattern 接收端 |
| Task Pool | Builtin `IWorkPattern` 默认实现之一 |
| `paperclip-acn-plugin` | Paperclip ↔ ACN 适配器；演进目标对齐本设计的 Port，而非替代模块 |
| Mode B `acn listen` | L1 成员入网/接活方式之一 |

---

## 10. 分阶段落地

### Phase 0 — 文档与定调（本文）

- 命名：ACN / Org Harness / Pasture 隐喻
- 架构：Kernel + Ports；Loop 优先
- 关系：ClawTeam/Swarm/OpenHarness/Paperclip

### Phase 1 — Kernel + 最小 Work + 薄 Loop（实现优先）

> 细则： [ADR-0014](../adr/0014-org-harness-module.md) D1–D7。

1. ~~ADR~~ → **ADR-0014 Accepted**
2. ~~持久化 Org / Membership / minimal work~~（Redis + PG + alembic `d3e4f5a6b7c8`）
3. ~~创建 Org ⇒ steward + subnet~~（`POST /api/v1/orgs`）
4. ~~Membership 先 join 后 upsert + 补偿 leave~~
5. ~~薄 Loop tick + `org.*` webhook~~（`POST …/loop/tick`）
6. ~~CLI：`acn org create | show | members | claim | transfer | release | work | tick`~~
7. 验收：`scripts/smoke_org_kernel.sh` + `tests/services/test_org_service.py`

### Phase 2 — Work Port

> **短方案（实施准绳）：** [phase2-work-port-v0.md](./phase2-work-port-v0.md)

1. **P2a（必做）** 最小 Port 插座 + 默认 `builtin_work`（现有 `OrgWorkItem`）；`smoke_org_kernel.sh` 行为不变  
2. **P2b（按需）** `plugins.work=task_pool` 进程内可选适配（非默认；外部 Pattern 仍禁绑 `/tasks/*`）  
3. **P2c（可并行）** `paperclip-acn-plugin`：Issues ↔ Org work + `org.*`，弱化 Task 镜像  

### Phase 3 — 增强 Port

- Policy/Budget、Memory、Capability 真插件化  
- ClawTeam / Swarm 适配器实验  
- 统一 Plugin 宿主（发现 / 版本 / 热加载；Phase 2 仅最小 resolve）  

### 明确后置

Org Memory 深度、跨 org 信誉、Dispute、Federation、agentic 支付轨（见 [org-pattern-adapter-spec-v0.md § Deferred](./org-pattern-adapter-spec-v0.md#7-deferred-enhancements) / BACKLOG）。

---

## 11. 设计决策记录（讨论收敛）

| # | 决策 |
|---|---|
| D1 | 对外主名 **Org Harness**；Pasture 仅隐喻 |
| D2 | **ACN 就是 ACN**，不叫 Pasture System |
| D3 | Org Harness 是 **ACN 新模块**，不是纯外部 Paperclip 概念 |
| D4 | 协作主体是 **agent**；Org Owner **可选**（`none` / human / agent），对称 ACN agent 所有权，**人不是必须** |
| D5 | 模块内 = **Kernel + Ports**；可插拔的是运转方式 |
| D6 | 三层：**Org Graph**（Kernel）+ **Control Loop**（节拍）+ **Work Graph**（Port 策略）；禁止用回合内 DAG 替代组织控制平面 |
| D7 | ClawTeam / Swarm = **Port 插件候选**，不是 Org Harness 平替 |
| D8 | OpenHarness 等 = **L1**，成员自带 |
| D9 | v0 不做「加人」叙事；复用 subnet 围栏 |
| D10 | v0 只做满 Kernel + Work + 薄 Loop + Events，避免过度对称 |

---

## 12. 相关文档

| 文档 | 关系 |
|---|---|
| [README.md](./README.md) | 本目录索引 |
| [../adr/0014-org-harness-module.md](../adr/0014-org-harness-module.md) | **P0/P1 决策 ADR（Accepted）** |
| [api-surface-tiers.md](./api-surface-tiers.md) | Network Core / Pattern 消费契约 |
| [org-model-v0.md](./org-model-v0.md) | 数据模型（随本文修订 ownership） |
| [org-pattern-adapter-spec-v0.md](./org-pattern-adapter-spec-v0.md) | 外部 Pattern 适配（过渡期 Task 镜像）；以本文 + ADR-0014 为准 |
| [`../_drafts/pasture-engineering.md`](../_drafts/pasture-engineering.md) | 学科隐喻与升维理论（Draft） |
| [`../_drafts/pasture-protocol.md`](../_drafts/pasture-protocol.md) | 协议理论 Draft；命名以本文为准 |

---

## 13. 一句话总结

> **Org Harness = ACN 内建的 Org Graph 内核（Org + 可选 Owner + agent 成员 + subnet 绑定）+ Control Loop 节拍 + 可插拔 Ports（Work Graph 策略 / Memory / Policy / Events）；Owner 与 agent 一样可无人认领 / 被人 claim / 由 agent 持有；L1 harness（含会话级 fan-out）由成员自带，ClawTeam/Swarm/Paperclip/LangGraph 挂 Port，ACN Network Core 是底座——不是把 ACN 改名叫 Pasture，也不是在内核复刻 Ultra。**
