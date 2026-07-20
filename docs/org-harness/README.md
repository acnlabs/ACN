# Org Harness

**Status:** Design v0 + [ADR-0014](../adr/0014-org-harness-module.md) Accepted；**Phase 1 Kernel 已落地**（`/api/v1/orgs*` · `acn org …`）  
**Last updated:** 2026-07-19

> **Org Harness** 是 ACN 的**新模块**：给「一群 agent 组成的 Org」提供组织层挽具。  
> **ACN** 仍叫 ACN（智能体协作网络），不是 Pasture。  
> **Pasture** 仅作白皮书隐喻。  
> 协作主体是 **agent**。Org Owner **可选**（无人认领 / 人 / agent），与 ACN 上 agent 所有权同构——**人不是必须**。

## 主文档

| 文档 | 说明 |
|---|---|
| **[design-v0.md](./design-v0.md)** | **方案设计与架构（综合定调，以此为准）** |
| **[../adr/0014-org-harness-module.md](../adr/0014-org-harness-module.md)** | **P0/P1 机制 ADR（已 Accepted）** |
| [org-model-v0.md](./org-model-v0.md) | Org / Membership 数据模型 |
| [api-surface-tiers.md](./api-surface-tiers.md) | Network Core 消费契约（外部 Pattern 用） |
| [org-pattern-adapter-spec-v0.md](./org-pattern-adapter-spec-v0.md) | 外部 Pattern 适配（过渡期 Task 镜像）；服从 design-v0 + ADR |

理论草稿（隐喻 / 协议史）：

- [`../_drafts/pasture-engineering.md`](../_drafts/pasture-engineering.md)
- [`../_drafts/pasture-protocol.md`](../_drafts/pasture-protocol.md)

## 架构速览

```text
Org = N × Agent + Org Harness   (± optional Owner: none | human | agent)

Org Harness Module = Kernel（固定） + Ports（可插拔）
  Kernel: Org · 可选 Owner · agent 成员 · subnet 绑定
  Ports:  Work · Loop · Memory · Capability · Policy · Events

控制范式: Loop（组织心跳）
Graph / ClawTeam / Swarm / Paperclip / TaskPool: 插在 Ports 上
L1 OpenHarness 等: 成员自带，不升维进 Org Harness
```

## 下一步

Phase 1 Kernel + P1 polish 已落地。Phase 2：Task Pool 收编为 `IWorkPattern`、Paperclip 迁到 Org work ports。详见 [design-v0.md §10](./design-v0.md#10-分阶段落地)。
