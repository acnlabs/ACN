# Org Harness

**Status:** Design v0 + [ADR-0014](../adr/0014-org-harness-module.md) Accepted；**Phase 1 Kernel + Phase 2a/P2c 已落地**（`/api/v1/orgs*` · Work Port `builtin_work` · Paperclip Org path）  
**Last updated:** 2026-07-24

> **Org Harness** 是 ACN 的**新模块**：给「一群 agent 组成的 Org」提供组织层挽具。  
> **ACN** 仍叫 ACN（智能体协作网络），不是 Pasture。  
> **Pasture** 仅作白皮书隐喻。  
> 协作主体是 **agent**。Org Owner **可选**（无人认领 / 人 / agent），与 ACN 上 agent 所有权同构——**人不是必须**。

## 先试用

| 文档 | 说明 |
|---|---|
| **[quickstart-org-paperclip.md](./quickstart-org-paperclip.md)** | **对内闭环：Org work ↔ Paperclip（hosted / 本地 e2e）** |
| **[org-task-bridge-v0.md](./org-task-bridge-v0.md)** | **对外发布 + 导入：Org ↔ Task Pool（≠ P2b）** |
| **[org-wallet-v0.md](./org-wallet-v0.md)** | **Org 钱包（decisions accepted）：`WalletType.ORG` + Org-paid publish** |

## 主文档

| 文档 | 说明 |
|---|---|
| **[design-v0.md](./design-v0.md)** | **方案设计与架构（综合定调，以此为准）** |
| **[../adr/0014-org-harness-module.md](../adr/0014-org-harness-module.md)** | **P0/P1 机制 ADR（已 Accepted）** |
| [org-model-v0.md](./org-model-v0.md) | Org / Membership 数据模型 |
| [api-surface-tiers.md](./api-surface-tiers.md) | Network Core 消费契约（外部 Pattern 用） |
| [org-pattern-adapter-spec-v0.md](./org-pattern-adapter-spec-v0.md) | 外部 Pattern 适配（`POST /orgs` + Org work；`task.*` 为 legacy） |
| **[phase2-work-port-v0.md](./phase2-work-port-v0.md)** | **Phase 2 Work Port 短方案（默认 builtin_work · P2a/P2c 完成 · P2b 按需）** |
| [org-task-bridge-v0.md](./org-task-bridge-v0.md) | Org → Task Pool 发布约定（约定桥，不是 Work Port） |

理论草稿（隐喻 / 协议史）：

- [`../_drafts/pasture-engineering.md`](../_drafts/pasture-engineering.md)
- [`../_drafts/pasture-protocol.md`](../_drafts/pasture-protocol.md)

## 架构速览

```text
Org = N × Agent + Org Harness   (± optional Owner: none | human | agent)

Org Harness Module = Kernel（固定） + Ports（可插拔）
  Org Graph（Kernel）: Org · 可选 Owner · agent 成员 · subnet 绑定
  Control Loop（Port）: 组织心跳 — 观察队列 → 分派/唤醒 → 回收
  Work Graph（Port 策略）: **builtin_work（默认）** / TaskPool（可选）/ Paperclip / Swarm / …
  其它 Ports: Memory · Capability · Policy · Events

L1 harness（含会话级 fan-out）: 成员自带，不升维进 Org Harness Kernel
```

## 下一步

- **已完成：** Phase 1 Kernel；Phase 2a（Work Port + `builtin_work`）；P2c（Paperclip Org work）；Org wallet **S0–S5** + Paperclip **0.3.3** poll 入站。  
- **试用入口：** [quickstart-org-paperclip.md](./quickstart-org-paperclip.md)（本地可不填公网 URL；含 Org-paid 软验）。  
- **对外发布 / 导入（v0）：** [org-task-bridge-v0.md](./org-task-bridge-v0.md)（`publish-task` / `import-task`；**不是** P2b）。  
- **经济主体：** [org-wallet-v0.md](./org-wallet-v0.md)（S0–S5 done；**S6** balance/topup UX + wallet proxy next）。  
- **按需：** P2b；自动 receive；按 `org_id` 列表。  
- **其后：** Phase 3 增强 Port / Plugin 宿主。
