# Org 运转模式（Runtime Modes）v0

**Status:** Accepted（叙事收口）· **Date:** 2026-08-15  
**Audience:** 产品 / 编排器与外部 Pattern 维护者  
**Depends on:** [design-v0.md](./design-v0.md) §0 · [plugin-catalog-v0.md](./plugin-catalog-v0.md) · [pattern-shelf-v0.md](./pattern-shelf-v0.md) · [org-swarm-metrics-v0.md](./org-swarm-metrics-v0.md)

> **一句话（对齐 DeepSeek Harness 的用法，不是抄 L1）：** 官方给 **几套开箱文档预设**；用户 **自由组合** Pattern。  
> **v0 尚无** `mode=` / 一键打包——预设是说明 + 狗粮链接。  
> 预设 ≠ 组织形式穷尽表；**公司式只是示例之一**，组织形式不只有公司。  
> **扩展正统 = 外部 Pattern / 社区适配**；进程内 `plugins.*` 只是默认电池。  
> Wave 指标是 **旁路观测**；**真节拍在外部 Pattern**（`heartbeat` 只是薄 tick，见 §6）。  
> 能跑的米 → **[pattern-shelf-v0.md](./pattern-shelf-v0.md)**。

---

## 1. 不变 vs 可变

| 稳住（Kernel / SoT） | 可变（Pattern / 预设） |
|---|---|
| Org / 成员 / 围栏 / 事件 | 怎么编队、谁拆票、谁叫醒、用什么看板/记忆 |
| Work 有权威状态（今日 `builtin_work`） | 中心编排 / 交班 / 自组织 / graph / 自研侧车… |
| 可选 `work.metadata`（只存不解析） | 关系与策略字段的**用法**（如 `metadata.wave`） |

想换运转方式 → **停/起外部 Pattern**；**不要**改未接线的 `plugins.loop=…` / `plugins.work=…` 指望生效。  
Port / Knowledge·Memory 切分是**建议坐标**，部署方可整包侧车；ACN 只守 Kernel 契约与 work/事件 SoT。

---

## 2. 预设 + 自由组合

和 DeepSeek 的 Standard / Minimal 等一样：**预设 = 常用组合的快捷入口**，不是宪法，也不是互斥枚举。

```text
用户冷启动
    │ 可选：选一个官方文档预设
    ▼
官方预设（纸面套餐：挂钩 + 狗粮链接）
    │ 增删、叠加、自研
    ▼
Kernel（薄）+ 任意外部 Pattern
```

**v0 诚实口径：** 下表是 **文档预设**（说明 + 链接）；**尚无** `mode=` API、一键 init 或可执行打包。可执行打包另议；id **illustrative**，可改名增减。

### 2.1 官方示例预设（可改名、可增减；非穷尽）

每种 = 推荐挂钩 + 狗粮链接；**零 Kernel 新实体**；**无** `plugins.mode=…`。

| 预设 id（示例） | 人话 | 典型挂钩（本预设主轴） | 勿默认塞进本包 |
|---|---|---|---|
| **ledger** | 先有组织与工单真相，暂不挂主动节拍 | Kernel + `builtin_work` + `heartbeat` | 编排器 / 看板 |
| **corp-board** | 偏公司科层 + **人看板**（公司式举例） | [Paperclip quickstart](./quickstart-org-paperclip.md) | 编排器 wake（要叫醒 → 另挂 **dispatch**） |
| **dispatch** | **中心叫醒**成员干活 | [org-orchestrator](./org-orchestrator-v0.md) · [wake 契约](./org-orchestrator-wake-contract-v0.md) | 人看板（要看板 → 另挂 **corp-board**） |
| **peer-handoff** | 成员之间交班 / 弱中心小队 | [work_handoff](./org-work-handoff-contract-v0.md) | — |

需要「看板 + 叫醒」→ **自由组合** `corp-board` + `dispatch`（见 §2.2），不要把两者写进同一预设主轴。

**不是清单：** 流水线 DAG、开源维护者+贡献者、课题组、临时联盟、市场招人、一体记忆 Hub……都可自组或社区提供。  
**Market（对外 Task / 钱包）** 见 [task-bridge](./org-task-bridge-v0.md) · [wallet](./org-wallet-v0.md)——旁路能力，需要时挂，不必塞进「对内预设」互斥表。
### 2.2 自由组合

- 同一 Org **可叠加**多个 Pattern（例如看板 + 编排器 wake）。  
- 混合时以 work SoT 为准；**同一动作轴只留一个写者**（见 §6）。  
- 换预设 = 换侧车组合，**不迁** Org / 成员 / 已有 work。  
- 选型归部署方；官方预设只降低冷启动成本。

### 2.3 常见手法（挂在预设里，不是第二套模式）

| 手法 | 说明 |
|---|---|
| 中心派工 / wake | 扇出 = **可选** M1，非必经 |
| 成员交班 | [work_handoff](./org-work-handoff-contract-v0.md) |
| 去中心自建/认领 | 仍写 Org work；约定自定 |
| Graph / DAG | 外部 Pattern；不替代 Control Loop（design §0.4） |
| 人看板同步 | Paperclip 等 |

---

## 3. 与 wave 指标的关系

[org-swarm-metrics-v0.md](./org-swarm-metrics-v0.md) 度量的是：**平行派工是否真并行、是否滥拆、墙钟是否缩短**。

- 子票可以来自编排器扇出、成员自建、或 graph 写出——只要关系能挂上（`metadata.wave` 或侧车图）。  
- **M1「自动扇出」** = 某一预设下的可选策略，**不是**启用指标的前提。  
- 无真 wave 关系时，只跑 window 粗表，不打 `SERIAL_*` / `FAKE_*`。

---

## 4. 工程含义（避免走偏）

| 做 | 不做 |
|---|---|
| 用预设降低冷启动；鼓励自由组合 | 把预设写成 Kernel 枚举或组织形式穷尽表 |
| 保持 Kernel 薄；策略在外 | `plugins.mode=corp` / 热加载第三方进 ACN 进程 |
| 指标与 Escrow/XP 解耦 | 用指标自动扣费/改派（M0/M1 默认） |

**下一步：** 有真实协作场景时，选或改一个预设再插包；文档与狗粮跟着那一行走。

---

## 5. Org 出问题修哪一层

分层借自 [Harness–Loop–Graph（L1 Agent）](https://mp.weixin.qq.com/s/qcItld5OnQbGb5Ib3altNg) 的诊断习惯，**映射到 Org**（勿把文中 Graph 当成 Kernel Org Graph）。  
原则：**先看证据再改图**；不要一卡住就换预设或加深 Pattern。

| 现象（先看这个） | 先修哪一层 | 典型动作 |
|---|---|---|
| 成员连不上 / 鉴权失败 / 围栏拒写 / work API 4xx | **环境 / 围栏** | 查成员、token、subnet、路由；别先改派工策略 |
| 票在、人不动；wake 了无进展；状态乱跳 | **Control Loop** | 查轮询/wake、认领、完成回写；看 observe JSONL / work 事件 |
| 拆票滥、假并行、墙钟不短、交接死循环 | **Workflow / Pattern** | 换或关掉 Pattern；看 wave 粗表；**不要**先改 Kernel |
| 「感觉慢」但 work 已完成、KB 有结论 | **可能不是 Org 坏了** | 用 work/KB 当进度；区分「人感」与 SoT |

**止损：** 同一故障连修两轮 Pattern 仍复现 → 停，回到上表上一层取证（环境或 Loop），再决定是否换预设。

---

## 6. Pattern 生命周期（挂上 / 切换 / 卸下）

### 6.1 `heartbeat` vs 外部编排器

| | ACN 内 `plugins.loop=heartbeat` | 外部 Pattern（编排器 / 待办执行器 / Paperclip 驱动…） |
|---|---|---|
| 角色 | 薄 Control Loop **插座**（tick） | **产品意义上的组织节拍**（谁叫醒、何时巡检） |
| 换预设时 | 通常 **保持** `heartbeat` | **换侧车或改配置**，不是改 `plugins.loop` 字符串 |
| 勿做 | `plugins.loop=orchestrator` / `clawteam` / `plugins.mode=…` | 指望 Builtin 白名单里「已有」这些 id |

更细的分层表见 [design-v0.md](./design-v0.md) §0.3。

### 6.2 切换 runbook（v0）

**原则：Work SoT 不搬家；只换「谁在推票 / 挂哪组 Pattern」。**

1. **停旧 Pattern** — 停编排器/侧车进程；关掉会写同一批 work 的第二套驱动（避免双投 wake）。  
2. **核对 SoT** — `list_work`：进行中票的 `assignee` / `status` 仍以 ACN 为准；不必迁库。  
3. **可选清侧车状态** — 本地 observe JSONL、wave 关系图、Pattern 私有游标可归档或丢弃；**不要**为了换预设改 Kernel。  
4. **起新 Pattern** — 指向同一 `org_id`；从 open work 重新 poll / 订阅 `org.*`。  
5. **混合时** — 同一 Org 可并存，但 **同一动作轴只留一个写者**。

| 动作 | 会影响 | 不会丢 |
|---|---|---|
| 停编排器侧车 | 新的 wake / 自动巡检 | Org、成员、围栏、已有 work |
| 卸 Paperclip | 人看板同步 | ACN 上的 work 行 |
| 改 `plugins.work` / `plugins.loop`（白名单外） | 创建/更新 Org 失败或无效 | —；自定义走外部，别改这两项撞墙 |

**卸干净的标志：** 无侧车进程、无第二套 webhook 消费者在改同一 org 的 work；`list_work` 状态与预期一致。
