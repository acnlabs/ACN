# ACN Org 编排器 — 产品定义 v0

**Status:** P0–P1 Accepted · **P2 最小侧车已落地（examples）**  
**Date:** 2026-07-27  
**Audience:** 产品 / Org Harness 维护者  
**Depends on:** [design-v0.md](./design-v0.md) §0 · [plugin-catalog-v0.md](./plugin-catalog-v0.md) · [org-pattern-adapter-spec-v0.md](./org-pattern-adapter-spec-v0.md)  
**P1：** [org-orchestrator-wake-contract-v0.md](./org-orchestrator-wake-contract-v0.md)  
**P2 code：** [`examples/org-orchestrator/`](../../examples/org-orchestrator/)

> **一句话：** **Org 编排器**是跑在 ACN **外面**的组织节拍器——读 Org 待办与成员，**叫醒该干活的 agent 成员**，回收结果并回写 work。  
> 它填的是 **Control Loop 问题轴**，实现形态是 **外部 Pattern**；**不是**新 Kernel，**不是** `plugins.loop=*` Builtin。

### 产品前提（Accepted）

> **大多数 Org / agent 没有 Paperclip。**  
> 因此编排器的**默认路径必须是 ACN-native**（只依赖 Org API + 成员信道），不能把「装 Paperclip」当开工前提。  
> Paperclip 仍是**可选**人侧驾驶舱（Issues ↔ work + 其自带 wakeup）；有则锦上添花，无则编排器独立成立。

市场上没有现成的「ACN Org 编排器」——要自研的是**薄侧车**（选人 + 唤醒 + 回写），不是再造 ClawTeam / Paperclip 级引擎。

---

## 1. 要解决什么

成员互派（A 用 A2A/CLI 叫 B）在「人少、自觉看列表」时够用；一旦出现：

- 待办积压，没人主动 `work list`；  
- 要按角色 / 技能把活派给**对的成员**；  
- 要周期性巡检「卡住 / 超时」并再唤醒；  

就需要一个**站在 Org 视角打节拍**的组件。那就是 Org 编排器。

**不做：** 单 agent 内部 tools（L1）；**不依赖**人看板（Paperclip 可选，非默认）；本机「有活就跑一条命令」走 [待办执行器](./org-loop-spawn-sidecar-poc-v0.md)，与编排器分开。

---

## 2. 在架构里的位置

```text
ACN 内：Kernel + builtin_work + heartbeat + events   ← SoT 仍在这里
              │
              │ work list / org.* / loop tick
              ▼
外部：Org 编排器（本产品，未实现）
              │
              ├── 选成员（Membership + 角色 / skills）
              ├── 唤醒：A2A / Mode B / 成员侧约定信道
              └── 治理 key 回写 work（v0 PATCH 约束）
```

| 项 | 决定 |
|---|---|
| SoT | **ACN** Org + Membership + `builtin_work` |
| 插法 | **外部 Pattern**（与 Paperclip 同级） |
| `plugins.loop` | **保持 `heartbeat`**；编排器消费 tick/事件，不进白名单 |
| 干活主体 | **Org 成员 agent**（有 ACN 身份），不是匿名本机进程优先 |
| 关单 | v0：**治理 key** PATCH（`todo`\|`in_progress`\|`done`\|`cancelled`） |

成员之间要把活交给同事时，走 [交班契约](./org-work-handoff-contract-v0.md)（`acn.org.work_handoff`），**不是**编排器广播。v0 为治理改派后的通知，不是成员自助转派。

---

## 3. 和相邻产品的边界

| | **Org 编排器（默认路径）** | [待办执行器](./org-loop-spawn-sidecar-poc-v0.md) | Paperclip（可选） | 成员互派 / [交班](./org-work-handoff-contract-v0.md) |
|---|---|---|---|---|
| 前提 | **不需要 Paperclip** | 一台 runner 机器 | 人要看板时才装 | 无 |
| 给谁 | 纯 agent Org 自动派活 / 唤醒 | 固定机器跑命令 | 人 + 被唤醒的 agent | 成员自觉协作 |
| 谁执行 | **成员 agent** | 本机 `spawnCommand` | Paperclip 拉起的 agent | 成员自己 |
| 核心动作 | 选人 → ACN 信道唤醒 → 收结果 | poll → 跑命令 → 关单 | Issues ↔ work + 自带 wakeup | A2A；**派活须挂 work + handoff** |
| ClawTeam | **可选**执行适配（[选型](./clawteam-org-loop-adapter-v0.md)） | 仅配方 | 无关 | 成员自用 |

**命名约束：** 对外说「Org 编排器」；不要说「装 ClawTeam / Paperclip 进 ACN」。二者都是可选周边，不是默认依赖。

---

## 4. v0 范围（Accepted 方向）

**做：**

1. **输入：** 某 `org_id` 的 open work + 成员表。  
2. **策略：** 有 `assignee` → 唤醒该成员；**无 assignee → 跳过 + 日志**（不广播）。  
3. **唤醒：** ACN `communication/send`；投递跟成员 delivery_mode；信封见 [唤醒契约](./org-orchestrator-wake-contract-v0.md)。  
4. **回收：** 治理面关单；v0 不自动超时 `cancelled`（仅日志）。  
5. **运维形态：** 单进程侧车；`ACN_BASE` + 发送方 agent key +（可选同）治理 key + `org_id`。

**不做（v0）：**

- 进程内 `plugins.loop=orchestrator`  
- DAG / 多步 Work Graph（那是 Work Port；默认仍 `builtin_work` 单票）  
- 替换 Membership、自建第二套待办 SoT  
- **依赖 Paperclip**（有则并行；无则必须可用）  
- 必须绑定 ClawTeam / LangGraph  
- 人审批 UI、Memory、预算硬停  

---

## 5. 成功标准（何时算产品成立）

| # | 标准 |
|---|---|
| S0 | **零 Paperclip** 环境：仅 ACN Org + 成员 agent + 编排器侧车即可跑通 S1–S2 |
| S1 | 新建 **带 assignee** 的 open work 后，编排器在约定 SLA 内向该成员发出 `acn.org.work_wake`（成员能凭 `work_id` 开工）；无 assignee 仅日志跳过 |
| S2 | 成员完成后，work 经治理路径变为 `done`；编排器不持有第二份权威状态 |
| S3 | 文档与 skill 能说清：编排器 ≠ 待办执行器 ≠ Paperclip；且无「必须装 Paperclip / ClawTeam」表述 |
| S4 | 关掉编排器后，Org / work / 成员数据仍完整（外部 Pattern 可卸） |

---

## 6. 落地顺序

| 步 | 内容 | 状态 |
|---|---|---|
| P0 | 本文产品定义 | **Accepted** |
| P1 | 唤醒契约（payload / 信道 / 幂等） | **Accepted** · [wake-contract](./org-orchestrator-wake-contract-v0.md) |
| P2 | 最小侧车：poll → 校验 assignee → `communication/send` → 幂等日志 | **done** · [`examples/org-orchestrator/`](../../examples/org-orchestrator/) · `scripts/smoke_org_orchestrator.sh` |
| P2.1 | 成员 playbook + `handle_wake.py` | **done** · [playbook](./org-orchestrator-member-playbook-v0.md) |
| P3 | `org.*` / tick 驱动、催办、超时策略 | 按需 |
| P3.5 | 编排质量账本（wave R/P/C/K；不依赖 P3） | **Accepted · M0 done** · [org-swarm-metrics-v0.md](./org-swarm-metrics-v0.md) |
| P4 | 可选：ClawTeam 等执行适配（[选型](./clawteam-org-loop-adapter-v0.md)） | 有需求再开 |
| P5 | 平行拆票 + 并行 wake（metrics M1） | 前置 `work.metadata` + 真可拆场景；见 wave-metrics |

---

## 7. P0 决策（Accepted）

| # | 决定 | 理由 |
|---|---|---|
| 1 | 无 assignee → **跳过 + 日志** | 避免误叫醒 |
| 2 | 唤醒 → **ACN 消息**（跟成员 delivery_mode） | 不新开 webhook 注册 |
| 3 | 治理 key → **Owner agent 或运维侧车** | 纯 agent Org 常见无运维人 |
| 4 | v0 主验收 → **仅有 assignee 的路径** | 减少策略扯皮 |
