# Org Harness

**Status:** Design v0 + [ADR-0014](../adr/0014-org-harness-module.md) Accepted；**Phase 1 Kernel + Phase 2a/P2c 已落地**（`/api/v1/orgs*` · Work Port `builtin_work` · Paperclip Org path）  
**Last updated:** 2026-08-01

> **Org Harness** 是 ACN 的**新模块**：给「一群 agent 组成的 Org」提供组织层挽具。  
> **ACN** 仍叫 ACN（智能体协作网络），不是 Pasture。  
> **Pasture** 仅作白皮书隐喻。  
> 协作主体是 **agent**。Org Owner **可选**（无人认领 / 人 / agent），与 ACN 上 agent 所有权同构——**人不是必须**。  
> **分层导读：** [design-v0.md §0](./design-v0.md#0-架构导读先读这个)（Kernel / Loop / Work / 外部 Pattern）。

## 先试用

| 文档 | 说明 |
|---|---|
| **[quickstart-org-paperclip.md](./quickstart-org-paperclip.md)** | **对内闭环：Org work ↔ Paperclip（hosted / 本地 e2e）** |
| **[org-task-bridge-v0.md](./org-task-bridge-v0.md)** | **对外发布 + 导入：Org ↔ Task Pool（≠ P2b）** |
| **[org-wallet-v0.md](./org-wallet-v0.md)** | **Org 钱包（decisions accepted）：`WalletType.ORG` + Org-paid publish** |

## 主文档

| 文档 | 说明 |
|---|---|
| **[design-v0.md](./design-v0.md)** | **方案设计与架构（综合定调，以此为准；先读 §0）** |
| **[../adr/0014-org-harness-module.md](../adr/0014-org-harness-module.md)** | **P0/P1 机制 ADR（已 Accepted）** |
| [org-model-v0.md](./org-model-v0.md) | Org / Membership 数据模型 |
| [api-surface-tiers.md](./api-surface-tiers.md) | Network Core 消费契约（外部 Pattern 用） |
| [org-pattern-adapter-spec-v0.md](./org-pattern-adapter-spec-v0.md) | 外部 Pattern 适配（`POST /orgs` + Org work；`task.*` 为 legacy） |
| **[phase2-work-port-v0.md](./phase2-work-port-v0.md)** | **Phase 2 Work Port 短方案（默认 builtin_work · P2a/P2c 完成 · P2b 按需）** |
| **[pattern-shelf-v0.md](./pattern-shelf-v0.md)** | **外部 Pattern 货架（目录不是商店；首批能跑货 + 社区自荐）** |
| **[plugin-catalog-v0.md](./plugin-catalog-v0.md)** | **官方 Port 推荐短名单（冷启动：默认 + ≤2 候选 / 状态）** |
| **[org-loop-spawn-sidecar-poc-v0.md](./org-loop-spawn-sidecar-poc-v0.md)** | **Org 待办执行器（外部）— agent 自动跑命令 POC（C1–C2）** |
| **[org-orchestrator-v0.md](./org-orchestrator-v0.md)** | **ACN Org 编排器产品定义（P0 Accepted；P2 侧车已落地）** |
| [org-orchestrator-wake-contract-v0.md](./org-orchestrator-wake-contract-v0.md) | Org 编排器唤醒契约 P1（`acn.org.work_wake`） |
| [org-orchestrator-member-playbook-v0.md](./org-orchestrator-member-playbook-v0.md) | 成员侧：收到 wake → 干活 → 治理关单 |
| **[org-work-handoff-contract-v0.md](./org-work-handoff-contract-v0.md)** | **成员交班契约（`acn.org.work_handoff`；v0=治理改派后通知；示例+狗粮已落地）** |
| **[org-runtime-modes-v0.md](./org-runtime-modes-v0.md)** | **文档预设 + 自由组合**（v0 无 `mode=` API；公司式仅为示例）；外部 Pattern 一等公民；wave 旁路观测 |
| **[org-swarm-metrics-v0.md](./org-swarm-metrics-v0.md)** | **编排质量 / wave 指标（Accepted；M0 + observe；`work.metadata` 已落地；自动扇出 = 可选 Pattern）** |
| **[org-knowledge-base-v0.md](./org-knowledge-base-v0.md)** | **Org 知识库 Port（`IOrgKnowledge`；与 Memory 分界；侧车路径）** |
| [`examples/org-knowledge/`](../../examples/org-knowledge/) | 知识库 K1：`read_kb.py` + `org_demo` 目录树 |
| [`examples/org-orchestrator/`](../../examples/org-orchestrator/) | 编排器侧车 + `handle_wake.py` + smoke |
| [clawteam-org-loop-adapter-v0.md](./clawteam-org-loop-adapter-v0.md) | ClawTeam ↔ Org Loop 适配器选型（编排器的可选实现；≠ 待办执行器） |
| [org-task-bridge-v0.md](./org-task-bridge-v0.md) | Org → Task Pool 发布约定（约定桥，不是 Work Port） |
| [../sparse-collab-contract-v0.md](../sparse-collab-contract-v0.md) | **全网稀疏协作契约（Accepted 2026-07-30；Candidate→Active→Settle；与 wave 正交）** |
| [../auto-collab-pull-mvp-v0.md](../auto-collab-pull-mvp-v0.md) | **自动拉人最小版（MVP-1 示例已落地；MVP-2 技能检索未做）** |

理论草稿（隐喻 / 协议史）：

- [`../_drafts/pasture-engineering.md`](../_drafts/pasture-engineering.md)
- [`../_drafts/pasture-protocol.md`](../_drafts/pasture-protocol.md)

## 架构速览

```text
Org = N × Agent + Org Harness   (± optional Owner: none | human | agent)

Org Harness Module = Kernel（固定） + Ports（可插拔）
  Org Graph（Kernel）: Org · 可选 Owner · agent 成员 · subnet 绑定
  Control Loop（Port）: 今日 heartbeat — 观察队列 → 分派/唤醒 → 回收
  Work Graph（Port）: 今日 builtin_work（默认）；TaskPool deferred
  其它 Ports: Knowledge · Memory · Capability · Policy · Events

自定义主路径 = 外部 Pattern（非 plugins.*）
  Paperclip · Org 待办执行器 · 知识库侧车 ·（将来）ClawTeam Loop 适配器等

L1 harness（含会话级 fan-out）: 成员自带，不进 Org Harness Kernel
```

## 下一步

- **已完成：** Phase 1 Kernel；Phase 2a（Work Port + `builtin_work`）；P2c（Paperclip Org work）；Org wallet **S0–S6**；Paperclip **`@acnlabs/paperclip-plugin-acn@0.3.5`**（Org-paid · 余额显示 · poll 入站 · 充值指引）。  
- **试用入口：** [quickstart-org-paperclip.md](./quickstart-org-paperclip.md)（本地可不填公网 URL；含 Org-paid 软验与 topup curl）。  
- **对外发布 / 导入（v0）：** [org-task-bridge-v0.md](./org-task-bridge-v0.md)（`publish-task` / `import-task`；**不是** P2b）。  
- **经济主体：** [org-wallet-v0.md](./org-wallet-v0.md) — **v0 收线**（S6b 插件内 topup **deferred**；外置 Backend 充值即可）。  
- **插件冷启动：** [plugin-catalog-v0.md](./plugin-catalog-v0.md)（官方短名单；Knowledge / Memory 等 adapter-planned）。  
- **Org 知识库：** [org-knowledge-base-v0.md](./org-knowledge-base-v0.md)（**agent 主贡献**；`plugins.knowledge` K3 + 读 K1/K2 + **写 K4**）· [侧车](../../examples/org-knowledge/) · [`smoke_org_knowledge.sh`](../../scripts/smoke_org_knowledge.sh)。  
- **按需（有真实卡住再开）：** P2b；自动 receive；按 `org_id` 列表 Tasks；Memory/Capability 薄适配；知识库 K5（`llm_wiki`）。  
- **实验（C1–C2）：** [Org 待办执行器（外部）](./org-loop-spawn-sidecar-poc-v0.md) + [`examples/org-loop-spawn-sidecar/`](../../examples/org-loop-spawn-sidecar/)（C3 webhook 按需）。  
- **Org 编排器：** [产品定义](./org-orchestrator-v0.md) P0 + [唤醒契约](./org-orchestrator-wake-contract-v0.md) P1 + [P2 侧车](../../examples/org-orchestrator/)；P3 催办/超时按需。  
- **成员交班：** [契约](./org-work-handoff-contract-v0.md) + [`send_handoff`/`handle_handoff`](../../examples/org-orchestrator/) + [`smoke_org_work_handoff.sh`](../../scripts/smoke_org_work_handoff.sh)。  
- **Pattern 货架：** [pattern-shelf-v0.md](./pattern-shelf-v0.md) — 已能跑的外部 Pattern 目录 + 社区自荐；不是进程内市场。  
- **运转模式：** [org-runtime-modes-v0.md](./org-runtime-modes-v0.md) — **文档预设 + 自由组合**（v0 无 `mode=`；公司式仅为示例）；外部 Pattern 一等扩展；§6 挂卸；自动扇出非必经。  
- **编排质量（M0 Accepted）：** [org-swarm-metrics-v0.md](./org-swarm-metrics-v0.md) + [`swarm_metrics.py`](../../examples/org-orchestrator/swarm_metrics.py) + [`work_observe.py`](../../examples/org-orchestrator/work_observe.py) + [`smoke_org_swarm_metrics.sh`](../../scripts/smoke_org_swarm_metrics.sh) + [`smoke_org_work_metadata_wave.sh`](../../scripts/smoke_org_work_metadata_wave.sh)（`metadata.wave` 狗粮）。  
- **选型（未实现）：** [ClawTeam ↔ Org Loop 适配器](./clawteam-org-loop-adapter-v0.md)（编排器可选后端，有需求再开）。  
- **其后：** Phase 3 增强 Port / Plugin 宿主。
