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
| B | 成员侧在线：Mode B `acn listen --runtime command --wake-exec '…/handle_wake.py'`，或 Mode A / `acn inbox` |
| C | `acn org work create … --assignee <成员>`（治理） |
| D | 跑 `run_orchestrator.py --once` |
| E | 成员日志出现 `acn.org.work_wake`；`handle_wake.py` 打印 `work show` 摘要 |
| F | 治理：`acn org work update … --status done` |

零 Paperclip。编排器与成员可同机；生产上编排器常跟 Owner，成员各自 `listen`。

---

## 2. 收到 wake 后做什么

```text
收到消息/事件
  → 解析 type == acn.org.work_wake（见契约）
  → list work 找到 work_id，确认仍 open 且 **API assignee = 自己**（空 assignee 不干）
  → 同一 `idempotency_key` 只处理一次（`handle_wake.py` 写本地 idem 文件）
  → L1 执行 title/描述要求的活
  → 完成：通知治理方 PATCH done（或 Owner/编排器代关）
  → 放弃：治理 PATCH cancelled
```

**不要：**

- 把 `work_id` 当成 Task Pool `task_id` 去 `tasks accept`  
- 假设自己的 agent key 能 `PATCH` work（v0 仅 governance）  
- 盲信信封里的 `title`/`status` 快照而不再拉 API  

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
