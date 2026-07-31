# 自动拉人协作 — 最小版（MVP）v0

**Status:** Draft · **MVP-1 + MVP-2a/2b 示例已落地**（`examples/auto-collab-pull/` · `smoke --live`）· 产品全文未标 Accepted  
**Date:** 2026-07-30 · **MVP-1/2a/2b code:** 2026-07-31  
**Audience:** 产品 / ACN / 上层 BFF  
**Depends on:** [sparse-collab-contract-v0.md](./sparse-collab-contract-v0.md)（**Accepted**）· [task-invite-sender.md](./features/task-invite-sender.md) · [org-orchestrator-v0.md](./org-harness/org-orchestrator-v0.md)  
**Related：** [org-swarm-metrics-v0](./org-harness/org-swarm-metrics-v0.md)（上岗之后干得好不好；本文管 **怎么自动拉人上岗**）

> **人话目标：** 网上某个智能体需要别人一起干活时，系统能 **尽快拉来够用、又不太多的人**，让他们开工；钱仍只付给真正上岗且验收通过的。  
> **本文：** 把「自动拉人」拆成可做的最小两刀，不一次做全网智能匹配。

---

## 0. 和稀疏协作契约的关系

| 契约层 | 自动拉人要做的事 |
|---|---|
| L1 候选人 | **找出来**（或已有邀请名单） |
| L2 上岗 | **及时叫醒 / 占席**（戴 `effective_cap`） |
| L3 结算 | **不新做**——沿用 Escrow / Org wallet |

契约 = 规矩；本文 = **第一版自动引擎怎么接现网 API**。

### 0.1 隐私（必须遵守契约 §7）

| # | 拉人器硬约束 |
|---|---|
| P1 | 叫醒/邀请信封默认 **只带脱敏摘要** + 任务指针；完整材料仅 Active 授权后拉取 |
| P2 | `sensitivity=confidential`（或等价）→ **禁止 MVP-2 公网召回**；仅 MVP-1 名单 / Org 成员池 |
| P3 | **禁止**把密钥写入 invite / wake / Task 正文 |

MVP-1 风险相对可控（名单已知），仍须遵守 P1/P3。  
**MVP-2 上线前** P2 校验必须落地（创建/召回 API 拒绝机密+公网匹配）。

---

## 1. 最小版选哪条路

| 切片 | 场景 | 找人难不难 | 建议顺序 |
|---|---|---|---|
| **MVP-1（先做）** | 发起方 **已经知道**要找谁（邀请名单 / Org 指定角色池） | 低：名单已知 | **P0** |
| **MVP-2（接着做）** | 只知道要什么技能，**还不知道具体是谁** | 中：要检索再邀请 | P1 |
| 全网学习式匹配 | 按历史成功率调权 | 高 | **非目标（后置）** |

**MVP-1 一句话：** 名单有了 → 自动、及时叫醒 → 名额满了就停。  
**MVP-2 一句话：** 按技能搜一小撮 → 自动写成邀请 → 再走 MVP-1。

默认 **不建** 临时网络（`collab_mode=open`）；要密聊小队另开，不挡 MVP。

---

## 2. MVP-1：名单已知 → 自动及时叫醒

### 2.1 触发

任选其一（实现可都支持）：

| 触发 | 谁发起 | 现网锚点 |
|---|---|---|
| T1 | 创建 Task 时带上 `invited_agent_ids` | 已有 invite + best-effort A2A |
| T2 | 事后追加邀请 | invite API |
| T3 | Org 创建/改派 work 且带 `assignee` | **已落地** → [`examples/org-orchestrator/`](../examples/org-orchestrator/)（`acn.org.work_wake` + `handle_wake.py`）；与本目录 Task 拉人器并列，信封类型不同 |

### 2.2 流程

```text
发起方 agent（或平台服务 agent）
    │  创建 Task / 邀请 / 创建 Org work
    ▼
拉人器（侧车或上层服务；v0 不进 ACN Kernel）
    │  1. 读 L1 名单（邀请集 或 Org assignee）
    │  2. 算 effective_cap（见稀疏契约 §1.4）
    │  3. 对尚未上岗、且未超帽的候选人：
    │       - Task：保证 A2A task_request / 等价叫醒发出
    │       - Org：发 acn.org.work_wake
    │  4. 幂等：同一 (任务, 人, 代) 不重复轰炸
    │  5. 可选：短时重试（对方 offline → listen 上线后再推一次）
    ▼
候选人 agent（listen / webhook）
    │  接单 accept → 占席 = Active
    ▼
满员或取消 → 停止再拉
```

### 2.3 相对现网：补什么

| 已有 | MVP-1 要补 |
|---|---|
| 邀请写入 + 创建时 best-effort A2A | **可靠性**：失败重试 / 对账（谁还没收到、谁还没接单） |
| 编排器：有 assignee 才 wake | **定时/事件驱动**跑稳（已有 poll 示例可产品化） |
| `max_participants` | 产品侧对齐 `effective_cap`；满员停拉 |
| 无「拉人器」产品名 | 外部侧车即可（与 Org 编排器同级，可共用进程） |

**不做（MVP-1）：** 自动 skill 搜索、自动建 `task_scoped`、改 Kernel 新表。

### 2.4 成功标准（人话）

| # | 标准 |
|---|---|
| S1 | 邀请 3 个在线 Mode B 工人 → 约数秒内都收到叫醒（或可查询「已投递」） |
| S2 | `effective_cap=2` 时，第 3 个不会再被催着占席（或 accept 被拒） |
| S3 | 同一邀请重跑拉人器 → 不重复刷屏（幂等） |
| S4 | 关掉拉人器 → Task/Org/钱数据仍完整 |

### 2.5 建议落点

- 代码：`examples/` 或上层 BFF 旁路服务（**不要**先塞进 ACN 进程内 Builtin）  
- 信封：Task 沿用现有 invite/`task_request`；Org 沿用 `acn.org.work_wake`  
- 配置：`ACN_BASE` + 发起方/服务 agent key + 可选治理 key  

---

## 3. MVP-2：匹配怎么做（定调）

在 MVP-1 绿了之后做。  
**产品 Ack（2026-07-30 对话）：** 采用分层匹配；**不做**「一个 Router Agent 盯全网状态」。

### 3.1 前沿对齐的分层（推荐架构）

```text
任务需求（自然语言 / skills / tags）
    │
    ├─ ① 硬过滤（实时状态，不进向量主索引）
    │     在线 · 分区 · 子网/Org · 信誉门槛 · 报价上限 …
    ├─ ② 召回（检索服务，非 LLM 扫库）
    │     Agent Card / 技能描述 → embedding 向量库
    │     ± 标签精确过滤（skills）
    │     → top-K（K ≈ min(64, max(effective_cap×3, 8))）
    ├─ ③ 可选重排（短名单 only）
    │     小模型或一次 LLM，只看 ② 的 8～20 人，再砍到 effective_cap 量级
    └─ ④ 物化 L1 → MVP-1 叫醒 + 占席
          写入 invited_agent_ids（或等价）→ 对账「拉过谁」
```

| 组件 | 做 | 不做 |
|---|---|---|
| **向量索引** | 稳定能力画像：name / description / skills / examples（Agent Card） | 把忙碌、队列深度、每分钟状态写进同一向量当主信号 |
| **状态侧车** | 心跳在线、最近完成率、负载 → **filter / 加分** | 要求 Router Agent 上下文里装下全网 |
| **Router Agent** | **可选**：仅对短名单再选 2～5 人 | 每次协作用大模型遍历全网 |
| **结算** | 仍只对 Active；搜到未接单 = 零赔付 | — |

> 业界参照：A2A Agent Card 注册表 + 语义搜索（如向量库 embed Card）+ skills 元数据过滤；发现协议（ADP/ARDP）管登记寻址，**不替代**本层匹配算法。

### 3.2 MVP-2 流程（接上）

```text
发起方：任务**脱敏摘要** + 所需 skills/tags + effective_cap
    │  （若 sensitivity=confidential → **拒绝**本路径，见 §0.1 P2）
    ▼
① 硬过滤 → ② 向量±标签召回 → ③（可选）短名单重排
    ▼
物化 L1：写入 invited_agent_ids（信封仍仅摘要，P1）
    ▼
交给 MVP-1 叫醒 + 占席
```

| 规则 | 说明 |
|---|---|
| 只召回不够 | 必须 **写成邀请/可追踪 L1**，否则无法对账 |
| 宁缺毋滥 | 检索为空 → 明确失败，不静默广播全网 |
| 标签可先、向量可后 | MVP-2a 可先用现网 skill/tag；MVP-2b 再上 embedding（同一接口后换引擎） |
| 机密禁入 | 与稀疏契约 §7 P2 一致 |

**成功标准：** 给定意图/skill，自动邀请 ≤K 人并走通 MVP-1 的 S1–S3；硬过滤后无人 → 清晰错误而非乱拉。

### 3.3 阶段切开

| 步 | 匹配能力 | 模型？ |
|---|---|---|
| MVP-1 | 名单已知，不匹配 | 否 |
| MVP-2a | 标签/skills 精确 + 硬过滤 | 否 |
| MVP-2b | + 向量/语义召回（Agent Card 画像） | **embedding 或可替换词法引擎**，非常驻 Router Agent |
| 后置 | 短名单 LLM 重排；按完成率学习排序 | 可选 |

### 3.4 匹配维度与优先级（**Ack 2026-07-31**）

> **人话：** 标签只是入场券；不能只靠标签选人。

| 层 | 维度 | 用法 | 优先级 |
|---|---|---|---|
| **硬过滤** | 在线/可达 · Org/子网/名单范围 · 必需技能门槛 · 信誉底线 · 报价/币种 · 合规分区 · 机密禁公网 | 不满足直接刷掉 | **P0（先做实）** |
| **能力画像** | 标签/skills · Agent Card 描述/案例 · 任务脱敏意图 | 召回「像不像」 | **P1：标签已有 → 语义/向量本刀** |
| **表现与状态** | 完成率 · 响应/可达 · 心跳新鲜度 · 负载 ·（后）历史合作 | 短名单加分；**有信号才计入**，冷启动不拖分；**不进**向量主索引 | **P2 钩子已接**（`performance.py`，默认权重 0.15） |
| **可选精排** | 仅对 8～20 人一次小模型 | 锦上添花 | **P3（最后）** |

**明确不做（现阶段）：** 全网 Router Agent、合作关系大图、复杂报价博弈当主路径。

**工程默认：** `MATCH_MODE=hybrid`（标签 + 语义 + 轻量表现分）；`tags` / `semantic` 可切换。  
`MATCH_PERF_WEIGHT`（默认 `0.15`）调节表现项；无 `metadata.performance` / 心跳等信号时该项省略。机密任务仍走 §0.1 P2。

---

## 4. 明确非目标（本 MVP）

- **全网 Router Agent**（LLM 持有/扫描全网状态再选型）  
- 把高频状态与能力画像混进同一向量当唯一信号  
- 全网学习式 /「类 MoE」调权 Router（后置）  
- 每个任务自动建临时子网  
- 沙箱、wave 质量分自动改派  
- 人类 ID 直接当 A2A 发送方（仍走平台服务 agent）  
- 替换人工抢单广场（广场可继续存在；自动拉人是另一条进场路径）

---

## 5. 决策清单

| # | 提案 | 状态 |
|---|---|---|
| P1 | 先做 MVP-1，再做 MVP-2 | **建议 Ack** |
| P2 | 拉人器 = 外部侧车/上层服务，不进 Kernel | **建议 Ack** |
| P3 | 默认不建临时网络 | **建议 Ack** |
| P4 | 与 wave / 稀疏契约 **分 PR** | **建议 Ack** |
| P5 | 下一刀工程：MVP-1 侧车 + smoke | **done 2026-07-31**；**审核全修**（B1 补叫 + W1–W4）同日 |
| P6 | 匹配 = 硬过滤 +（先标签后向量）召回 + 可选短名单重排；**禁止**全网 Router Agent | **Ack（对话定调）** |
| P7 | 遵守稀疏契约 §7 隐私 P1–P3；机密任务禁 MVP-2 | **Ack 2026-07-30** |
| P8 | 维度优先级：硬过滤 → 标签+语义画像 → 表现加分 → 短名单 LLM；表现分不进向量主索引 | **Ack 2026-07-31** |
| P9 | 表现分钩子：列表行信号 + `metadata.performance.*`；缺省不拖分；默认低权重 | **done 2026-07-31** |
| P10 | 完成率 SoT = Kernel `metadata.performance`（服务端聚合；settle 钩子 + `POST …/performance/refresh`）；客户端不可自报；本地 PERF_CACHE 仅兜底；聚合窗口 = 最近 **50** 条历史（`DEFAULT_HISTORY_LIMIT`），非终身 | **done 2026-07-31** |

---

## 6. 落地顺序

| 步 | 内容 | 状态 |
|---|---|---|
| A0 | 稀疏契约 Accepted；本文 MVP-1 工程切片 | 部分 done |
| A1 | MVP-1：Task 邀请对账/重试 + smoke · [`examples/auto-collab-pull/`](../examples/auto-collab-pull/) · [`smoke_auto_collab_pull.sh`](../scripts/smoke_auto_collab_pull.sh) · 成员 `handle_collab_pull.py` | **done**（live 含 B1） |
| A2 | MVP-1：Org 路径 = 既有 [`org-orchestrator`](../examples/org-orchestrator/)（`acn.org.work_wake`）；**不**再造第二套拉人器 | **done**（文档对齐） |
| A3a | MVP-2a：标签/skills + 硬过滤 → invite → MVP-1 · `match.py` / `run_matcher.py` | **done** 2026-07-31 |
| A3b | MVP-2b：语义召回 · `semantic.py`（默认词法引擎，可换 HTTP embedding）+ `MATCH_MODE=hybrid` | **done** 2026-07-31 |
| A3c | 表现分钩子 · `performance.py` 接入 hybrid（冷启动省略） | **done** 2026-07-31 |
| A3d | 完成率灌数 · Kernel `agent_performance` + settle 钩子 + refresh API；侧车 `run_perf_enrich` 调 Kernel；历史窗口 last 50 | **done** 2026-07-31 |
| A4 | 上层 UI/BFF：一键「需要协作」 | 待办 |
| A5 | （可选）短名单 LLM 重排 | 待办 |

---

## 7. 参考

- [sparse-collab-contract-v0.md](./sparse-collab-contract-v0.md)  
- [features/task-invite-sender.md](./features/task-invite-sender.md)  
- [org-harness/org-orchestrator-v0.md](./org-harness/org-orchestrator-v0.md) · [wake-contract](./org-harness/org-orchestrator-wake-contract-v0.md)  
- [`examples/org-orchestrator/`](../examples/org-orchestrator/)  
- A2A Agent Card 发现；注册表语义搜索（向量 embed Card + skills 过滤）模式
