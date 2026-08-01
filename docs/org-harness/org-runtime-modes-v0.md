# Org 运转模式（Runtime Modes）v0

**Status:** Accepted（叙事收口）· **Date:** 2026-08-01  
**Audience:** 产品 / 编排器与外部 Pattern 维护者  
**Depends on:** [design-v0.md](./design-v0.md) §0 · [plugin-catalog-v0.md](./plugin-catalog-v0.md) · [org-swarm-metrics-v0.md](./org-swarm-metrics-v0.md)

> **一句话：** Org 怎么转（中心派工、去中心自组织、graph、混合）是 **可插拔 Pattern**，按 Org **按需、可动态切换**；不是 Harness 宪法里的单一正统。  
> Wave 指标是 **旁路观测**，不绑定某一种运转模式。

---

## 1. 不变 vs 可变

| 稳住（Kernel / SoT） | 可变（Pattern） |
|---|---|
| Org / 成员 / 围栏 / 事件 | 谁拆票、谁叫醒、是否并行 |
| Work 有权威状态（今日 `builtin_work`） | 中心编排 / 成员自组织 / LangGraph 等 |
| 可选 `work.metadata`（只存不解析） | 关系与策略字段的**用法**（如 `metadata.wave`） |

想换运转方式 → **外部 Pattern**（或将来白名单 Builtin）；**不要**伪造未接线的 `plugins.loop=…` / `plugins.work=…`。

---

## 2. 模式菜单（非穷尽、可混合）

| 模式 | 典型行为 | 已有挂钩 |
|---|---|---|
| **中心派工** | 编排器 wake；可选再拆平行子票 | [org-orchestrator](./org-orchestrator-v0.md)；扇出 = **可选** M1，非必经 |
| **成员交班** | 串行改派 + 通知 | [work_handoff](./org-work-handoff-contract-v0.md) |
| **去中心自组织** | 成员自建/认领子票、互派 | 仍写 Org work（+ 可选 metadata）；约定按 Org 自定 |
| **Graph 编排** | LangGraph / CrewAI / DAG | 挂 **Work 策略或外部 Pattern**；不替代 Control Loop（design §0.4） |
| **混合** | 同一 Org 并存多种 | 允许；动态切换不要求迁 Kernel |

同一 Org **可以今天偏中心、明天偏自组织**；混合时以 work SoT 为准，Pattern 可叠加可卸（关掉侧车，Org/work/成员仍在）。

---

## 3. 与 wave 指标的关系

[org-swarm-metrics-v0.md](./org-swarm-metrics-v0.md) 度量的是：**平行派工是否真并行、是否滥拆、墙钟是否缩短**。

- 子票可以来自编排器扇出、成员自建、或 graph 写出——只要关系能挂上（`metadata.wave` 或侧车图）。  
- **M1「自动扇出」** = 中心派工 Pattern 下的一种策略实现，**不是**启用指标的前提，也**不是**所有 Org 的必经关卡。  
- 无真 wave 关系时，只跑 window 粗表，不打 `SERIAL_*` / `FAKE_*`。

---

## 4. 工程含义（避免走偏）

| 做 | 不做 |
|---|---|
| 按真实 Org 场景再插具体 Pattern | 先造「万能自动扇出引擎」当主干 |
| 保持 Kernel 薄；策略在外 | 把会话级 L1 fan-out 塞进 Org Kernel |
| 指标与 Escrow/XP 解耦 | 用指标自动扣费/改派（M0/M1 默认） |

**下一步：** 有具体协作卡住时，再选上表某一行（或混合）落地；文档与狗粮跟着那一行走。
