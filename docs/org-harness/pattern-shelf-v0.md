# Org Pattern 货架 v0

**Status:** Draft（目录，不是商店）· **Date:** 2026-08-15  
**Audience:** 想拼组织运转方式的人 / 想自荐 Pattern 的作者  
**Depends on:** [org-runtime-modes-v0.md](./org-runtime-modes-v0.md) · [org-pattern-adapter-spec-v0.md](./org-pattern-adapter-spec-v0.md) · [plugin-catalog-v0.md](./plugin-catalog-v0.md)

> **人话：** 能换能组，得先看得见米。本页只摆 **已经能跑的外部 Pattern**。  
> **不是** 进程内插件市场，**不是** `plugins.*=你的包名`。  
> 上架 = 自己的进程 + Org/work API + `org.*` 事件。契约见 [adapter spec](./org-pattern-adapter-spec-v0.md)。

---

## 首批货（官方，能跑）

| id | 干什么 | 怎么装 / 试用 | 常和哪个文档预设拼 |
|---|---|---|---|
| **paperclip** | 人看板 ↔ Org work | [quickstart](./quickstart-org-paperclip.md) · [`@acnlabs/paperclip-plugin-acn`](https://www.npmjs.com/package/@acnlabs/paperclip-plugin-acn) ≥ 0.3.5 | `corp-board`；要叫醒再叠加 `dispatch` |
| **orchestrator** | 中心叫醒成员 | [产品](./org-orchestrator-v0.md) · [wake 契约](./org-orchestrator-wake-contract-v0.md) · [`examples/org-orchestrator/`](../../examples/org-orchestrator/) | `dispatch`；要看板再叠加 `paperclip` |
| **handoff** | 成员交班通知 | [契约](./org-work-handoff-contract-v0.md) · 同目录 `send_handoff` / `handle_handoff` · [`smoke_org_work_handoff.sh`](../../scripts/smoke_org_work_handoff.sh) | `peer-handoff` |
| **knowledge-git** | 组织知识库侧车（agent 贡献） | [KB 设计](./org-knowledge-base-v0.md) · [`examples/org-knowledge/`](../../examples/org-knowledge/) · [`smoke_org_knowledge.sh`](../../scripts/smoke_org_knowledge.sh) | 任意预设上叠加；`plugins.knowledge=git` 只开契约，引擎在侧车 |

旁路（需要时再挂，不占对内主轴）：[task-bridge](./org-task-bridge-v0.md) · [org-wallet](./org-wallet-v0.md)。  
实验（examples）：[待办执行器](./org-loop-spawn-sidecar-poc-v0.md)。  
观测（不是 Pattern）：[wave 指标](./org-swarm-metrics-v0.md)。

预设怎么选、怎么混合 → [org-runtime-modes-v0.md §2](./org-runtime-modes-v0.md#2-预设--自由组合)。挂卸 → 同文 §6。

---

## 社区自荐（先不进内核）

有自己的看板 / 叫醒 / 交班 / 记忆 / 图编排？**不要**发 `plugins.*=…` 的 PR。

开 [ACN Discussion](https://github.com/acnlabs/ACN/discussions) 或 Issue，标题建议 `pattern-shelf: <短名>`，写清：

1. 干什么（一句话）  
2. 仓库 / 安装  
3. 调哪些 Org API、收哪些 `org.*`  
4. 建议和哪个文档预设拼  
5. 一条别人能复现的试用路径  

维护者只审「是否误导 / 是否声称进了进程内白名单」。**不承诺**官方维护，也不热加载进 ACN。

最小上架门槛与 [plugin-catalog](./plugin-catalog-v0.md) 官方筛选一致：可自托管（或 API 清楚）、能按 `org_id` 隔离、不强迫进 Kernel、有试用路径。

---

## 明确不是

| 不是 | 去哪 |
|---|---|
| 进程内 `plugins.*` 货架 | [plugin-catalog-v0.md](./plugin-catalog-v0.md)（官方电池） |
| npm/pypi 市场 UI、评分、一键安装进 ACN | 非目标 |
| 组织形式穷尽表 | [runtime-modes](./org-runtime-modes-v0.md)（预设 + 自由组合） |
