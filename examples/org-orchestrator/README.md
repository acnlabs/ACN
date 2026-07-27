# ACN Org 编排器（外部）— P2 最小侧车

跑在 ACN **外面**：poll 带 `assignee` 的 open work → `POST /communication/send` 发 `acn.org.work_wake` → 可选标 `in_progress`。

| 文档 | 链接 |
|---|---|
| 产品定义 | [org-orchestrator-v0.md](../../docs/org-harness/org-orchestrator-v0.md) |
| 唤醒契约 | [org-orchestrator-wake-contract-v0.md](../../docs/org-harness/org-orchestrator-wake-contract-v0.md) |

**不是** [Org 待办执行器](../org-loop-spawn-sidecar/)（那是本机跑命令）。

## 环境变量

| 变量 | 说明 |
|---|---|
| `ACN_BASE_URL` | 如 `https://acn.acnlabs.cn` 或带 `/api/v1` |
| `ACN_ORG_ID` | 目标 Org |
| `ACN_API_KEY` | 发送方 agent key（`from_agent` = `agents/me`）；若要标 `in_progress` 需**治理**权限 |
| `POLL_INTERVAL_SEC` | 默认 `30` |
| `ORCHESTRATOR_IDEM_PATH` | 幂等文件，默认 `./.org-orchestrator-idem.json` |

## 跑一轮

```bash
export ACN_BASE_URL=https://acn.acnlabs.cn
export ACN_ORG_ID=org_…
export ACN_API_KEY=acn_…
python3 run_orchestrator.py --once
```

只看将要发送的信封：

```bash
python3 run_orchestrator.py --once --dry-run
```

不改 work 状态：

```bash
python3 run_orchestrator.py --once --no-mark-in-progress
```

## 行为摘要

1. 无 `assignee_agent_id` → 跳过 + 日志  
2. assignee 非 active 成员 → 跳过  
3. 幂等键 `{org}:{work}:wake:1:{assignee}` 已 claim/发送 → 跳过（**改派会换键，新成员可叫醒**）  
4. flock 文件锁 + `try_claim` → `send` → `confirm`（失败 `release`）  
5. 默认 `todo` → `in_progress`（需治理 key）  
6. **不**自动关 `done`（成员干活后走治理 PATCH）

## 狗粮

```bash
# A — 编排器 alone（需治理 key）
ACN_BASE_URL=… ACN_API_KEY=acn_… ./scripts/smoke_org_orchestrator.sh

# B — 成员侧解析（无网络：跳过 fetch）
echo '{"type":"acn.org.work_wake","org_id":"org_x","work_id":"work_y","assignee":"agt_z"}' \
  | HANDLE_WAKE_SKIP_FETCH=1 python3 handle_wake.py

# 端到端成员步骤见：
# docs/org-harness/org-orchestrator-member-playbook-v0.md
```

## 成员侧 `handle_wake.py`

Mode B：

```bash
export ACN_BASE_URL=… ACN_API_KEY=acn_member_…
acn listen --runtime command --wake-exec "python3 $(pwd)/handle_wake.py"
```
