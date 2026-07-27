# Org Loop spawn sidecar — 选型 + POC v0

**Status:** Accepted（C0）· C1 in progress  
**Date:** 2026-07-27  
**Audience:** Org Harness 维护者 / Pattern 作者  
**Depends on:** [design-v0.md](./design-v0.md) §5–§6 · [plugin-catalog-v0.md](./plugin-catalog-v0.md) · [org-pattern-adapter-spec-v0.md](./org-pattern-adapter-spec-v0.md)  
**Code:** [`examples/org-loop-spawn-sidecar/`](../../examples/org-loop-spawn-sidecar/)

> **模块是什么：** 外部「执行侧车」——看见 Org 待办 → 跑你配置的命令 →（可选）回写 work。  
> **ClawTeam 是什么：** 推荐配方之一（`spawnCommand` 示例），**不是**侧车本体，也不是 ACN Kernel 插件名。

---

## 1. 问题（人话）

ACN 已经能建组织、派扁平行任务、打 tick。  
缺的是：tick 之后，**本机自动拉起某个 worker 去干活**。

Paperclip 补的是「给人看的看板」；这条 POC 补的是「给 agent 用的执行器钩子」。

---

## 2. 选型（已定）

| 题 | 决定 |
|---|---|
| 做什么 | **通用 spawn 侧车**（外部进程），契约稳定、实现可换 |
| 不做什么 | 不设 `plugins.loop=clawteam`；不进进程内 registry |
| Work / Loop | 仍用 `builtin_work` + `heartbeat` |
| ClawTeam / 其它 | 仅作 `spawnCommand` **示例配方** |
| LangGraph 等 | 延后（那是 Work Graph，不是本 POC） |

```text
ACN Org
  work = builtin_work · loop = heartbeat
            │ poll open work  (或以后 webhook)
            ▼
   org-loop-spawn-sidecar   ← 可插拔模块（本 POC）
            │ spawnCommand（可换）
            ▼
   ClawTeam / 脚本 / 任意 L1 worker
```

---

## 3. 已拍板（默认）

| # | 题 | 决定 |
|---|---|---|
| 1 | 仓库 | ACN 仓 [`examples/org-loop-spawn-sidecar/`](../../examples/org-loop-spawn-sidecar/)；成了再拆独立仓 |
| 2 | 身份 | **列表/poll**：治理或有权读者 API key。**关单 `PATCH work`：** 今日仅 **governance**（owner / created_by）→ 侧车持治理 key 在 worker 成功退出后关单；成员 key 留给 worker 自己的 L1/A2A，不冒充关单 |
| 3 | 上游 | **任意 `spawnCommand`**；README 给 ClawTeam（或 echo 狗粮）示例，不钉死上游版本 |

> 纠正：此前「成员 key PATCH done」与 v0 API 不符（`update_work` 走 `_require_governance`）。POC 按上表。

---

## 4. POC 场景

**最小可验：** 有 open work → 侧车 poll 到 →（C2）跑 `spawnCommand` → 退出 0 → 治理 key 把 work 标 `done`。

| 切片 | 产出 | 状态 |
|---|---|---|
| **C0** | 本文 Accepted + catalog 指向 | **done** |
| **C1** | poll open work + 日志（不 spawn） | **本 PR** |
| **C2** | `spawnCommand` + 成功后 PATCH done | 下一刀 |
| **C3** | webhook / 重试幂等（可选） | 按需 |

---

## 5. 非目标

- 人类看板、审批、Org Memory、Task Pool 镜像  
- 短命 worker ≠ `OrgMembership`  
- 完整对等任何上游（ClawTeam / LangGraph…）  
- 把 Project / Workspace 塞进 Kernel  

---

## 6. 配置草图

```yaml
acnBaseUrl: https://acn.acnlabs.cn
acnOrgId: org_…
acnApiKey: acn_…              # governance（C2 关单需要）
mode: poll
pollIntervalSec: 30
spawnCommand: "echo work={{work_id}}"   # 可换成 ClawTeam / 自写脚本
```

入站 POC：**poll** `GET /orgs/{id}/work?open_only=true`。  
出站（C2）：`PATCH /orgs/{id}/work/{work_id}` status（governance）。
