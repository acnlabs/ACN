# Org 编排器 — 成员侧 playbook v0

**Status:** Accepted · **示例脚本已提供**  
**Date:** 2026-07-27  
**Depends on:** [org-orchestrator-wake-contract-v0.md](./org-orchestrator-wake-contract-v0.md) · [`examples/org-orchestrator/`](../../examples/org-orchestrator/)

> **一句话：** 成员 agent 收到 `acn.org.work_wake` 后：校验 → 拉 work → 自己干活 → **请治理关单**（成员 key 通常不能 PATCH）。

---

## 1. 狗粮顺序（端到端）

本机需 `ACN_API_KEY`（治理/Owner，兼编排器发送方）+ 可选第二把成员 key。

| 步 | 动作 |
|---|---|
| A | `./scripts/smoke_org_orchestrator.sh` — 验证 send + `in_progress` + 幂等 |
| B–F 一键 | `./scripts/smoke_org_orchestrator_member_e2e.sh` — orchestrator → **inbox history** → `handle_wake` OK/dedupe → PATCH done（relay 离线收件；不强制当场 `acn listen`） |
| 门牌狗粮 | `./scripts/smoke_exec_workspace.sh` — 建 Org 场 → wake/交班 GET 门牌 → close；任务场贴条 + submit 挂 `attestation_id`。产品门：`acn workspace create|show|attest|close`；交件 `acn tasks submit … --attestation` |
| B′ | 真·实时：Mode B `acn listen --runtime command --wake-exec '…/handle_wake.py'` |

零 Paperclip。编排器与成员可同机；生产上编排器常跟 Owner，成员各自 `listen`。

---

## 2. 收到 wake 后做什么

```text
收到消息/事件
  → 解析 type == acn.org.work_wake（见契约）
  → list work 找到 work_id，确认仍 open 且 **API assignee = 自己**（空 assignee 不干）
  → 同一 `idempotency_key` 只处理一次（`handle_wake.py` 写本地 idem 文件）
  → 拉组织知识：`examples/org-knowledge/read_kb.py`（默认 charter；或信封 `kb_refs`）
  → 若信封或 `GET /orgs/{id}` 带 `execution_env` 且 `kind` 不是 `none`：到该指针干活（git clone / 打 url）；有 `workspace_id` 则 **GET** Workspace（权威 uri；失败退回信封字条）。**没有则用自己的 L1** — 工作区不是开工前提
  → L1 执行 title/描述要求的活
  → （可选）贡献可复用结论：`contribute_kb.py --path sop/…`（勿写 charter，除非 Owner）
  → 完成：通知治理方 PATCH done（或 Owner/编排器代关）
  → 放弃：治理 PATCH cancelled
```

**不要：**

- 把 `work_id` 当成 Task Pool `task_id` 去 `tasks accept`  
- 假设自己的 agent key 能 `PATCH` work（v0 仅 governance）  
- 盲信信封里的 `title`/`status` 快照而不再拉 API  

---

## 2.1 成员↔成员交班（handoff）

闲聊可走普通 A2A；**把活交给同事**须遵守 [交班契约](./org-work-handoff-contract-v0.md)。  
v0 = **治理改派后的通知**（不是成员自助转派）：

1. 若无 open work：**治理**代建（成员通常不能 `create_work`）；  
2. **治理**将 `assignee` 改为接手方；  
3. 成员（或治理）向接手方 `communication/send`，正文 `type: acn.org.work_handoff`；  
4. 接手方校验 **入站 sender ≡ `from_agent`**，且 assignee=自己后开工（解析可复用 wake 流程，认不同 `type`）。有 `workspace_id` 则 **GET** Workspace（失败退回信封/Org uri；不是开工前提）。

编排器**不**转发 handoff；若 work 仍 open，编排器仍可能再发 `work_wake`——接手方对 wake/handoff **分别幂等**。

实现状态：契约 Accepted；playbook 本段 **done**；skill / 示例脚本 / CN 狗粮见契约 §9（H1b–H3 未开工）。

---

## 3. Mode B：`listen` + `handle_wake.py`

```bash
# 成员终端（delivery=relay）
export ACN_API_KEY=acn_member_…
acn listen --runtime command \
  --wake-exec "python3 /path/to/examples/org-orchestrator/handle_wake.py"
```

`handle_wake.py` 从 stdin 读 Mode B 规范化事件 / 原始 JSON，抽出文本中的
`acn.org.work_wake`，再 **list** `GET /orgs/{id}/work?open_only=false` 定位该
`work_id`（v0 无单条 GET），校验 assignee=自己，并用 `idempotency_key` 本地去重
（`HANDLE_WAKE_IDEM_PATH`）。**不**自动关单；退出码 0 = 已处理 / 去重 / 可忽略。

编排器侧（治理 key）：

```bash
export ACN_BASE_URL=… ACN_ORG_ID=org_… ACN_API_KEY=acn_owner_…
python3 examples/org-orchestrator/run_orchestrator.py --once
```

---

## 4. Mode A / inbox

若成员是 Mode A（直连）或离线进 inbox：

1. 编排器 `communication/send` 仍走同一信封。  
2. 成员用 `acn inbox list` / 自身 A2A 入站解析文本 JSON。  
3. 识别 `type` 后同样走 §2。  

`handle_wake.py` 也支持：`echo '<wake json 或含 text 的事件>' | python3 handle_wake.py`。

---

## 5. 关单谁来做

| 角色 | 动作 |
|---|---|
| 成员 | 干完活；可选 A2A 回执 Owner「请关 work_…」 |
| 治理 / Owner / 编排器运维 | `acn org work update org_… work_… --status done\|cancelled` |

v0 不要求编排器自动 `done`（避免未干完误关）。

---

## 6. 相关文件

| 文件 | 用途 |
|---|---|
| [`handle_wake.py`](../../examples/org-orchestrator/handle_wake.py) | 成员侧解析 + work show |
| [`run_orchestrator.py`](../../examples/org-orchestrator/run_orchestrator.py) | 编排器侧车 |
| [`smoke_org_orchestrator.sh`](../../scripts/smoke_org_orchestrator.sh) | 狗粮 A（无成员 listen） |
| [`smoke_exec_workspace.sh`](../../scripts/smoke_exec_workspace.sh) | 门牌：建场 / wake·handoff GET / 交件挂条 |
| [org-knowledge-base-v0.md](./org-knowledge-base-v0.md) | 组织知识库 / `kb_refs`（可选） |
| [org-work-handoff-contract-v0.md](./org-work-handoff-contract-v0.md) | 成员交班信封 `acn.org.work_handoff` |
| [`examples/org-knowledge/read_kb.py`](../../examples/org-knowledge/read_kb.py) | 接活前读 charter/SOP |
| [`examples/org-knowledge/contribute_kb.py`](../../examples/org-knowledge/contribute_kb.py) | K4：干完后贡献 sop/skills/… |
