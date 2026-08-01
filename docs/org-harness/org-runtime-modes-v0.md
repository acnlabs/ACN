# Org 运转模式（Runtime Modes）v0

**Status:** Accepted（叙事收口）· **Date:** 2026-08-02  
**Audience:** 产品 / 编排器与外部 Pattern 维护者  
**Depends on:** [design-v0.md](./design-v0.md) §0 · [plugin-catalog-v0.md](./plugin-catalog-v0.md) · [org-swarm-metrics-v0.md](./org-swarm-metrics-v0.md)

> **一句话：** Org 怎么转（中心派工、去中心自组织、graph、混合）是 **可插拔 Pattern**，按 Org **按需、可动态切换**；不是 Harness 宪法里的单一正统。  
> Wave 指标是 **旁路观测**，不绑定某一种运转模式。  
> **真节拍在外部 Pattern**；进程内 `heartbeat` 只是薄 tick（见 §6）。

---

## 1. 不变 vs 可变

| 稳住（Kernel / SoT） | 可变（Pattern） |
|---|---|
| Org / 成员 / 围栏 / 事件 | 谁拆票、谁叫醒、是否并行 |
| Work 有权威状态（今日 `builtin_work`） | 中心编排 / 成员自组织 / LangGraph 等 |
| 可选 `work.metadata`（只存不解析） | 关系与策略字段的**用法**（如 `metadata.wave`） |

想换运转方式 → **停/起外部 Pattern**（或将来白名单 Builtin）；**不要**改未接线的 `plugins.loop=…` / `plugins.work=…` 指望生效。
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

---

## 5. Org 出问题修哪一层

分层借自 [Harness–Loop–Graph（L1 Agent）](https://mp.weixin.qq.com/s/qcItld5OnQbGb5Ib3altNg) 的诊断习惯，**映射到 Org**（勿把文中 Graph 当成 Kernel Org Graph）。  
原则：**先看证据再改图**；不要一卡住就加深 Pattern。

| 现象（先看这个） | 先修哪一层 | 典型动作 |
|---|---|---|
| 成员连不上 / 鉴权失败 / 围栏拒写 / work API 4xx | **环境 / 围栏** | 查成员、token、subnet、路由；别先改派工策略 |
| 票在、人不动；wake 了无进展；状态乱跳 | **Control Loop** | 查轮询/wake、认领、完成回写；看 observe JSONL / work 事件 |
| 拆票滥、假并行、墙钟不短、交接死循环 | **Workflow / Pattern** | 换或关掉 Pattern；看 wave 粗表；**不要**先改 Kernel |
| 「感觉慢」但 work 已完成、KB 有结论 | **可能不是 Org 坏了** | 用 work/KB 当进度；区分「人感」与 SoT |

**止损：** 同一故障连修两轮 Pattern 仍复现 → 停，回到上表上一层取证（环境或 Loop），再决定是否换模式。

---

## 6. Pattern 生命周期（挂上 / 切换 / 卸下）

### 6.1 `heartbeat` vs 外部编排器

| | ACN 内 `plugins.loop=heartbeat` | 外部 Pattern（编排器 / 待办执行器 / Paperclip 驱动…） |
|---|---|---|
| 角色 | 薄 Control Loop **插座**（tick） | **产品意义上的组织节拍**（谁叫醒、何时巡检） |
| 换模式时 | 通常 **保持** `heartbeat` | **换侧车或改配置**，不是改 `plugins.loop` 字符串 |
| 勿做 | `plugins.loop=orchestrator` / `clawteam` | 指望 Builtin 白名单里「已有」这些 id |

更细的分层表见 [design-v0.md](./design-v0.md) §0.3。

### 6.2 切换 runbook（v0）

**原则：Work SoT 不搬家；只换「谁在推票」。**

1. **停旧 Pattern** — 停编排器/侧车进程；关掉会写同一批 work 的第二套驱动（避免双投 wake）。  
2. **核对 SoT** — `list_work`：进行中票的 `assignee` / `status` 仍以 ACN 为准；不必迁库。  
3. **可选清侧车状态** — 本地 observe JSONL、wave 关系图、Pattern 私有游标可归档或丢弃；**不要**为了换模式改 Kernel。  
4. **起新 Pattern** — 指向同一 `org_id`；从 open work 重新 poll / 订阅 `org.*`。  
5. **混合时** — 同一 Org 可并存（如 Paperclip 看板 + 编排器 wake），但 **同一动作轴只留一个写者**（例如不要两个编排器同时对同一票发 wake）。

| 动作 | 会影响 | 不会丢 |
|---|---|---|
| 停编排器侧车 | 新的 wake / 自动巡检 | Org、成员、围栏、已有 work |
| 卸 Paperclip | 人看板同步 | ACN 上的 work 行 |
| 改 `plugins.work` / `plugins.loop`（白名单外） | 创建/更新 Org 失败或无效 | —；自定义走外部，别改这两项撞墙 |

**卸干净的标志：** 无侧车进程、无第二套 webhook 消费者在改同一 org 的 work；`list_work` 状态与预期一致。