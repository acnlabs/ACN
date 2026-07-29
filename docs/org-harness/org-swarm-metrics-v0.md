# Org 编排质量指标（wave metrics）v0

**Status:** Accepted · **审核修订 2026-07-29**（B1–B3 / W1–W7）· **M0 fixtures+smoke 已落地**  
**Date:** 2026-07-29  
**Audience:** 产品 / Org 编排器维护者  
**Depends on:** [design-v0.md](./design-v0.md) §0 · [org-orchestrator-v0.md](./org-orchestrator-v0.md) · [org-work-handoff-contract-v0.md](./org-work-handoff-contract-v0.md) · 现网 `OrgWorkItem`（无 `metadata`）  
**Inspiration:** Kimi Agent Swarm（PARL / 关键路径 / Context Sharding）— **只借度量与反模式，不复制闭源 Swarm 产品**  
**Related（正交）：** [sparse-collab-contract-v0.md](../sparse-collab-contract-v0.md) — L0→L2 **进场与结算**；本文只度量 L2 **内部**并行质量  
**Code：** [`swarm_metrics.py`](../../examples/org-orchestrator/swarm_metrics.py) · [`smoke_org_swarm_metrics.sh`](../../scripts/smoke_org_swarm_metrics.sh)

> **一句话：** 给 **Org 编排器（外部 Pattern）** 增加「协作是否真并行、是否偷懒、墙钟是否缩短」的可观测指标；可选再做「一票拆多子票并行唤醒」。  
> **对外名：** 编排质量 / **wave**（波次）。文中 `swarm` 仅作可选 metadata 键名与灵感标注，**≠** L1 会话内 fan-out。  
> **不是：** Kernel 新模块、`plugins.loop=swarm`、沙箱 Port、单 agent 内部 fan-out（那是 L1）。

---

## 0. 为什么现在写

| 已有 | 缺口 |
|---|---|
| 编排器 P2：有 assignee → `work_wake` → 治理关单 | 只知道「叫醒了 / 关了」，不知道协作质量 |
| handoff：成员交班挂 work | 串行交班可见，**并行扇出**无约定 |
| Kimi Swarm 启示 | 防串行崩溃、防虚假并行、用**关键路径**逼墙钟优化 |

目标：把 Swarm 的**编排语义**落到 ACN 开放组织上，而不是做成会话内同模型子代理集群。

### 0.1 现网约束（审核锁定）

今日 `OrgWorkItem` 字段仅为：

`work_id · org_id · title · status · assignee_agent_id · created_at · updated_at`

- **无** `metadata`、**无** 状态转移历史、**无** 验收态（仅有 `todo|in_progress|done|cancelled`）。  
- `GET .../work?open_only=false` 可列出含终态票；编排器 poll 只能看见**当前快照**。  
→ M0 必须用**侧车本地观测日志**补时间轴；M1 依赖 **`work.metadata` 先落地**（或显式侧车关系图），见 §3.2 / D4。

---

## 1. 架构位置（硬边界）

```text
ACN 内：Kernel + builtin_work + heartbeat + events     ← SoT 不变
              │
              │ work list / PATCH / org.* 
              ▼
外部：Org 编排器（已有侧车）
              │  + 本方案：质量账本（metrics）
              │  + 可选：平行拆票策略（fan-out）
              ▼
成员 agent（L1 自带 harness / 可选自带沙箱）— 不进 Kernel
```

| 项 | 决定 |
|---|---|
| 落点 | **Org 编排器**增强；**不依赖 P3 催办/超时完成**，可与 P3 并行 |
| SoT | 仍是 ACN Org work；指标是**派生观测**，可丢可重建 |
| `plugins.*` | **不新增** Builtin；不写 `plugins.loop=swarm` |
| 沙箱 | **正交**；属成员 / 待办执行器 / AM·Ranch 执行层，**不**作为 Org Harness 模块或本文范围 |
| 与 handoff | handoff = 成员→成员串行交班；本方案 fan-out = **编排器**拆平行子票 |

`design-v0` §0.4：LangGraph / Swarm / CrewAI 多挂 **Work 策略或外部 Pattern**——本文即此路径的**度量层**（M0）与可选扇出（M1）。

---

## 2. 从 Kimi 映射到 ACN 词汇

| Kimi Swarm | 我们的对应 | 是否照搬 |
|---|---|---|
| 指挥官（Orchestrator） | **Org 编排器**侧车（可兼治理 key） | 角色对齐，实现已有 |
| 冻结子 agent | **Org 成员 agent**（能力不由编排器改写） | 天然成立 |
| 串行崩溃 | 一票活长期只派给同一人、从不拆 | **要防**（真 wave 上） |
| 虚假并行 | 滥建子票、多数立刻 cancelled / 无产出 | **要防**（真 wave 上） |
| 关键路径 | 同一波次内墙钟代理（见 §4） | **核心指标**（M0 为粗代理） |
| Context Sharding | 子票只回 **摘要 / kb_refs**，禁止 transcript 灌编排器 | **约定**（M1+ playbook） |
| PARL 三维奖励训模型 | **不做模型 RL**；改为编排器策略分 + 运维看板 | 只借形状 |

---

## 3. 概念模型

### 3.1 波次（Wave）

一次「可并行」的派工单元。

```text
root_work_id     # 人类/治理可见的母票（可仍是 builtin_work 单票）
wave_id          # 编排器生成：wv_{uuid}
child_work_ids[] # 本波次平行子票
```

| 模式 | 定义 | 用途 |
|---|---|---|
| **真 wave** | 侧车关系图或（M1）`metadata.wave` 显式绑定 root+children | 告警、P/C/K 规范定义 |
| **窗聚合（window bundle）** | 同一 Org、时间窗 T 内终态/活跃票的粗打包 | **仅趋势狗粮**；**不作** `SERIAL_*` / `FAKE_*` 告警依据 |

**M0：** 真 wave 来自 **fixtures 或侧车本地图**；线上默认也可跑 window bundle 报表（标注 `kind=window`）。  
**M1：** 治理/编排器拆 N 个子票并写入关系后，才对生产流量打反模式告警。

### 3.2 子票关系存放（B1 收口）

现网 **无** `OrgWorkItem.metadata`。关系存放按阶段：

| 阶段 | 存放 | 说明 |
|---|---|---|
| **M0** | 侧车本地（JSON/SQLite）`wave_id → {root, children[]}`；smoke 用合成 fixtures | **不改** Kernel |
| **M1 前置（阻塞）** | ACN 为 `OrgWorkItem` 增加可选 **`metadata: object`**（JSON），编排器写入 `metadata.wave` | Kernel **只存不解析** |
| **备选（不推荐作主路径）** | 侧车关系图永不进 ACN | 多编排器实例无法共享图；仅单机 POC |

M1 建议写入形状（键名 `wave`，避免与 L1「swarm」混淆；若已有草稿用 `swarm` 亦可，但文档与实现须统一一种）：

```json
{
  "wave": {
    "role": "root",
    "root_work_id": "work_…",
    "wave_id": "wv_…",
    "shard_hint": "optional topic slice"
  }
}
```

- 非法/缺失 → 退化为今日单票行为。  
- **D4：** M1 **以 `work.metadata` 落地为前置**；在此之前不宣称生产扇出。

### 3.3 观测日志（B3 收口）

侧车每次 poll `list_work` 时，对每票差分写入 append-only 事件（本地）：

```text
{ ts, work_id, status, assignee_agent_id, observed_at }
```

- **峰值并行度 / 历时** 只从该日志推导，不假设 ACN 提供转移历史。  
- 关掉 metrics 模块 = 不写日志、不算分；唤醒/关单路径零改动（M0-S3）。

### 3.4 Context Sharding（成员回写约定）

| 允许回写到 work / 编排器可见处 | 禁止 |
|---|---|
| 结论摘要（短） | 完整 tool transcript |
| 结构化结果指针（URL / artifact id） | 把其他子票上下文粘贴进本票 |
| `kb_refs`（走 Org 知识库） | 要求编排器重放全量子推理 |

与 handoff 契约一致：大段内容进 KB，信封/work 只挂引用。M0 仅在 playbook 预留一节；强制检查属 M1+。

---

## 4. 指标体系

对每个 **真 wave**（规范）；window bundle 只输出粗表并标 `kind=window`。

### 4.0 规范定义（一种公式）

| 指标 | **规范定义** | 防什么 |
|---|---|---|
| **R — Result** | 母票（或波次代表票）终态为 `done` → 1，否则 0。聚合：窗口内 `done` 占比 | 空转 |
| **P — Parallelism** | 观测日志上，波次内 **同时** `status=in_progress` 的子票数的**峰值**（报表字段 `P`；`P_norm=P/n` 仅评分用） | 串行崩溃 |
| **C — Completion** | `子票 done 数 / 子票创建数`（`cancelled` 计入分母） | 虚假并行 |
| **K — Critical path（代理）** | 单波次、**无阶段划分**时：`K = max_i (t_terminal_i - t_first_in_progress_i)`（缺 in_progress 观测则用 `updated_at - created_at`）。多阶段 DAG 留 M2 | 假并行不省墙钟 |

**备选（非规范）：** `P' = 1 - (串行等价步 / 子票步)` — 仅分析用，不进告警阈值。

**删除（W2）：** 「治理验收通过率」——现网无独立验收态；若未来增加，再开 M2。

### 4.1 M0 粗代理（无真 wave 子图时）

线上仅有 flat work 列表时，可算（**不得**触发 §4.2 告警）：

| 粗指标 | 定义 |
|---|---|
| R_window | 时间窗 T 内终态票中 `done` 占比 |
| P_proxy | 当前 open 票中 **不同** `assignee_agent_id` 数 |
| K_proxy | 窗内 `max(updated_at - created_at)`（终态票） |

### 4.2 反模式告警（仅真 wave）

| 代码 | 条件（默认阈值，可配） | 含义 |
|---|---|---|
| `SERIAL_COLLAPSE` | 子票数 ≥ 2、峰值 P = 1、且墙钟总历时 ≥ 0.85 × Σ 各子票历时；**须有绝对时间轴**（`started_at`/`ended_at`）。仅有 `duration_sec` 时不告警（否则 wall:=Σ 会假阳性） | 名拆实串 |
| `FAKE_PARALLEL` | 子票数 ≥ 3 且 C &lt; 0.3，或 cancelled 占比 &gt; 0.5 | 滥拆 |
| `CRITICAL_PATH_REGRESSION` | 同 root 重跑时 K 显著变差（需基线） | **M2** |

### 4.3 评分（可选；M0 可不算）

```text
score = 0.5 * R + 0.25 * P_norm + 0.25 * C
P_norm = min(1, P / max(1, child_count))
```

- 报表输出峰值计数为 `P`；`P_norm` 仅用于评分。  
- **只展示与告警**，不自动改派、不扣 Org wallet。  
- 与 Escrow / XP **解耦**。

---

## 5. 与编排器路线图的衔接

| 步 | 内容 | 相对现网 |
|---|---|---|
| **P3**（已规划） | tick / 催办 / 超时 | 与本文 **无硬依赖** |
| **P3.5（本文 M0）** | fixtures 真 wave → R/P/C/K + 告警 + smoke | **done**（live 观测日志 poll 后续） |
| **P5（本文 M1）** | **前置：** `OrgWorkItem.metadata`；再拆平行子票 + 并行 wake | 有真实可拆任务且 metadata 已合并 |
| **P6** | 看板（CLI / `org wave report`）或可选 Paperclip | 有狗粮再做 |

不插入 P4（ClawTeam）关键路径；P4 仍按需。

---

## 6. M0 落地范围

**已做：**

1. [`swarm_metrics.py`](../../examples/org-orchestrator/swarm_metrics.py)：对 **fixture 真 wave** 算 R/P/C/K；`kind=window` 不算 §4.2 告警。  
2. [`smoke_org_swarm_metrics.sh`](../../scripts/smoke_org_swarm_metrics.sh) + demo fixtures（不连活 Org）。  
3. README / orchestrator §6 已链。

**未做（不挡 Accepted）：**

- poll `list_work` → 本地差分事件日志（§3.3）  
- 成员 playbook「并行子票只回摘要」专节（M1 前补）

**不做（M0）：**

- 改 ACN Kernel / 加 `metadata`（那是 **M1 前置**）  
- 自动拆票、自动改 assignee  
- 沙箱、模型 RL、`plugins.*`  
- 指标绑 XP / Escrow  
- 对 window bundle 打 `SERIAL_*` / `FAKE_*`

**成功标准（B2 收口）：**

| # | 标准 | 状态 |
|---|---|---|
| M0-S1 | 合成 wave 稳定算出 R/P/C/K | **done**（单测） |
| M0-S2 | fixture 真 wave 触发 `SERIAL_COLLAPSE` / `FAKE_PARALLEL` | **done** |
| M0-S3 | 关掉 metrics 后编排器路径不变 | **done**（独立脚本） |
| M0-S4 | `kind=window` 不产生 §4.2 告警 | **done**（单测） |

---

## 7. M1 扇出（仅草案；阻塞前置写清）

**前置（全部满足再开工）：**

1. 真实可拆场景（多源调研 / 多角色评审等）。  
2. ACN `OrgWorkItem` **可选 `metadata`** 已合并并有 list/PATCH 透传。  
3. M0 smoke 绿。

**步骤：**

1. 治理创建 root + N child，写入 `metadata.wave`。  
2. 编排器对 child 并行 `work_wake`（现有唤醒契约）。  
3. 全部 child `done`（或法定多数）→ 治理关 root；K 按波次结算。  
4. 单 child 超时 → 告警，默认不拖死整波（可配）。

成员互派仍走 handoff；**不要**用 handoff 冒充并行扇出。

---

## 8. 决策清单

| # | 提案 | 状态 |
|---|---|---|
| D1 | 指标落在编排器侧车，不进 Kernel | **Ack（审核）** |
| D2 | M0 只观测、不自动拆票 | **Ack（审核）** |
| D3 | 指标不进 Escrow/XP | **Ack（审核）** |
| D4 | M1 子票关系：**先**落地 `work.metadata`，再写 `metadata.wave`；M0 用侧车/fixtures | **条件 Ack（审核）** |
| D5 | 下一步工程 = M0 账本 + fixture smoke（不依赖 P3） | **Ack · M0 done** |

---

## 9. 非目标

- 复制 Kimi「300 子代理 / 4000 步」产品形态  
- 在 ACN 进程内跑 Firecracker / AgentENV  
- 训练 orchestrator 模型（PARL）  
- 替代 TaskBoard / Task Pool（网络市场另层）  
- 把沙箱做成 Org Harness Builtin / Port

---

## 10. 审核修订记录

| 原问题 | 处理 |
|---|---|
| B1 `metadata` 不存在 | §0.1 / §3.2 / D4：M0 侧车图；M1 阻塞于 metadata |
| B2 M0-S2 与无子票矛盾 | S2 明确为 fixture 真 wave；window 不告警 |
| B3 无状态历史 | §3.3 侧车观测日志；K 为无阶段粗代理 |
| W1 P 双公式 | §4.0 只留峰值 P；P' 为备选 |
| W2 验收通过率 | 删除 |
| W3 对话结论 | 改为稳定表述（§1 沙箱） |
| W4 score 权重 | 默认 0.5 / 0.25 / 0.25；M0 可选不算 |
| W5 伪波次 | 改名 window bundle；禁告警 |
| W6 命名 | 对外 wave；键 `metadata.wave` |
| W7 P3 依赖 | §5 写明不依赖 P3 |

---

## 11. 参考

- 内部：[org-orchestrator-v0.md](./org-orchestrator-v0.md) · [org-work-handoff-contract-v0.md](./org-work-handoff-contract-v0.md) · [design-v0.md](./design-v0.md) §0 · [sparse-collab-contract-v0.md](../sparse-collab-contract-v0.md) · `acn/core/entities/org.py` (`OrgWorkItem`)  
- 外部：Kimi Agent Swarm（指挥官冻结队员、三维奖励、关键路径、Context Sharding）
