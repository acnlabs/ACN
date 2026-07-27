# Org 编排器 — 唤醒契约 v0（P1）

**Status:** Accepted · **P2 示例已实现**（[`examples/org-orchestrator/`](../../examples/org-orchestrator/)）  
**Date:** 2026-07-27  
**Audience:** 编排器实现者 / 成员 agent 作者  
**Depends on:** [org-orchestrator-v0.md](./org-orchestrator-v0.md) · ACN `POST /communication/send` · Mode A/B delivery

> **一句话：** 编排器用 **ACN 既有消息信道**叫醒成员；payload 里带 Org work 指针。不新开 webhook 注册表。

---

## 1. Accepted 决策（摘自产品定义 §7）

| # | 决定 |
|---|---|
| 无 assignee | **跳过 + 日志**（不广播） |
| 唤醒信道 | **ACN 消息**；投递跟成员 `delivery`（Mode A direct / Mode B relay→inbox/listen） |
| 治理 key | Owner agent **或**运维侧车持有；持 key = 能 PATCH work |
| v0 主路径 | **仅处理有 `assignee` 的 open work** |

---

## 2. 谁发给谁

```text
编排器进程（持：编排器用 agent key，或治理/Owner key 兼用）
    │
    │  POST /api/v1/communication/send
    │  to = work.assignee（必须是 Org 成员）
    ▼
成员 agent（Mode A HTTP 或 Mode B listen/inbox）
    │
    │  解析 payload → acn org work show / 开工
    ▼
（可选）治理面 PATCH work → done | cancelled
```

| 角色 | 身份 |
|---|---|
| **发送方** | 编排器配置的 ACN agent（推荐：Org Owner agent，或专用 `orchestrator` 服务 agent） |
| **接收方** | `work.assignee`；发送前校验仍为该 Org `active` 成员 |
| **关单方** | 另持**治理**权限的 key（可为同一 Owner agent；v0 PATCH 仅 governance） |

发送方 key **不必**是治理 key；唤醒与关单权限可分离。v0 为省事允许同一 Owner key 兼用，文档须提示风险。

---

## 3. 消息载荷（Content）

经 `POST /communication/send` 的文本/结构化正文须让成员**不依赖编排器进程**也能开工。

### 3.1 规范信封（Accepted）

消息正文（text part 或等价 JSON 字符串）为：

```json
{
  "type": "acn.org.work_wake",
  "schema_version": 1,
  "idempotency_key": "org_…:work_…:wake:1:agt_…",
  "org_id": "org_…",
  "work_id": "work_…",
  "title": "…",
  "status": "todo",
  "assignee": "agt_…",
  "hint": "Fetch work with acn org work show; complete then ask governance to mark done.",
  "kb_refs": [
    { "uri": "orgkb://org_…/charter.md", "title": "charter.md" }
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `type` | 是 | 固定 `acn.org.work_wake` |
| `schema_version` | 是 | 现为 `1` |
| `idempotency_key` | 是 | 见 §4 |
| `org_id` / `work_id` | 是 | SoT 指针 |
| `title` | 是 | 便于展示；权威仍以 API 为准 |
| `status` / `assignee` | 建议 | 发送时快照 |
| `hint` | 否 | 给人/agent 的操作提示 |
| `kb_refs` | 否 | 组织知识指针（**不塞全文**）；见 [org-knowledge-base-v0.md](./org-knowledge-base-v0.md)。成员用 sidecar 解析 `orgkb://` |

成员侧：**以 `work_id` 再拉一次 Org API 为准**，勿盲信快照字段。  
`kb_refs` 的 org_id 须与信封 `org_id` 一致；全文由 [`examples/org-knowledge/`](../../examples/org-knowledge/) 拉取（`handle_wake.py` 已接）。

### 3.2 与 Mode B runtime 的关系

- Mode B：`acn listen --runtime …` 收到的是 A2A/`a2a_message` 事件；runtime 应从消息文本中识别 `acn.org.work_wake`（或透传 raw 由上层解析）。  
- **不**把 Org `work_id` 伪装成 Task Pool `task_id`（避免与 task invite 语义混淆）。  
- 若需在 metadata 带指针，可选：`metadata.acn_org_id` / `metadata.acn_work_id`（与正文双写；正文仍必填）。

---

## 4. 幂等与重试

| 项 | 规则 |
|---|---|
| **幂等键** | `{org_id}:{work_id}:wake:{wake_generation}:{assignee_agent_id}` |
| **为何含 assignee** | 改派后新成员必须能收到新 wake；旧 assignee 的键仍保留，避免对其重复投递 |
| **wake_generation** | v0 固定从 `1` 起；同一 `(work, assignee)` 未成功投递前重试复用同一 generation |
| **何时 +1** | 仅当产品允许「再次唤醒同一 assignee」（例如超时催办）时递增；v0 默认可不做催办 |
| **编排器本地** | `try_claim`（flock）→ `send` → `confirm`；`send` 失败则 `release` 以便重试；落盘失败不更新成功语义 |
| **成员侧** | 同一 `idempotency_key` 只开工一次；重复消息忽略或仅打日志 |

投递失败（网络/对方 `closed`）：释放 claim 后可重试同一 key；**不要**因此 PATCH work。

---

## 5. 编排器 tick 算法（v0 最小）

每轮（poll 或消费 `org.work_*` / `org.loop_tick`）：

1. `GET` open work（`todo` / `in_progress`）。  
2. 无 `assignee` → **log skip**，继续。  
3. `assignee` 非本 Org active 成员 → **log skip**（不发送）。  
4. 已有成功 `idempotency_key` 记录 → skip。  
5. `communication/send` → 成功则记幂等；可选将 `todo` → `in_progress`（需治理 key；v0 **建议做**，便于区分「已叫醒」）。  
6. **不**在 v0 因超时自动 `cancelled`（仅日志；P3 再开）。

---

## 6. 成员收到后怎么做（契约期望）

1. 解析 `type == acn.org.work_wake`。  
1b. （可选）按 `kb_refs` 或默认 `charter.md` 只读拉组织知识，再开工。  
2. `acn org work list` / 等价 list API 找到该 `work_id`，确认仍 open 且自己是 assignee（v0 **无** `GET /work/{id}`）。  
3. 用自身 L1 执行。  
4. 完成：经治理路径 `PATCH` → `done`（成员 key 若无治理权，则回报 Owner/编排器代关，或人工/脚本）。  
5. 放弃：治理 `cancelled`。

v0 **不**要求成员能自己 PATCH（API 约束）；编排器文档须写明关单路径，避免「叫醒了却无法关单」。

---

## 7. 非目标（P1）

- 成员预注册自定义 webhook  
- 无 assignee 的 skills 匹配 / 广播  
- ClawTeam / 本机 spawn  
- 替换 `plugins.loop`  
- 与 Task Pool `task_request` 混用同一信封  

---

## 8. 下一步

| 步 | 内容 | 状态 |
|---|---|---|
| P2 | `examples/org-orchestrator/` 最小侧车 | **done** |
| P3 | `org.*` webhook 驱动、催办 generation+1、超时策略 | 按需 |
