# ClawTeam ↔ Org Loop 适配器 — 选型 v0

**Status:** Draft · **未实现**（adapter-planned）  
**Date:** 2026-07-27  
**Audience:** Org Harness 维护者 / 想复用 ClawTeam 做组织节拍的人  
**Depends on:** [design-v0.md](./design-v0.md) §0 · §6 · [plugin-catalog-v0.md](./plugin-catalog-v0.md) · [org-pattern-adapter-spec-v0.md](./org-pattern-adapter-spec-v0.md)

> **一句话：** 若要用 [ClawTeam](https://github.com/HKUDS/ClawTeam) 驱动 ACN Org，应写**外部 Loop 适配器**（Org work ↔ CT task），**不是** `plugins.loop=clawteam`，也**不是** [待办执行器](./org-loop-spawn-sidecar-poc-v0.md) 里一行 `SPAWN_COMMAND`。

---

## 1. 和另外两条路的区别

| | **本选型：Loop 适配器** | [Org 待办执行器](./org-loop-spawn-sidecar-poc-v0.md) | 成员互派 |
|---|---|---|---|
| 职责 | 组织节拍：看 ACN 队列 → 映射 CT → spawn/协调 → 回写 | 本机 poll → 跑**一条**命令 → 关单 | 成员自己 A2A / CLI |
| SoT | **ACN Org work**（CT 看板若开，只是执行视图） | ACN Org work | ACN Org work |
| ClawTeam 角色 | 多 worker **协调平面**（适配器对接） | 可选的 `spawnCommand` 配方 | 成员本机自用 |
| `plugins.loop` | **不改**（仍 `heartbeat`）；适配器吃 tick/事件 | **不改** | 不需要 |

旧文件名 `clawteam-loop-adapter-poc-v0.md` 曾指向待办执行器 POC，**勿按文件名理解**；见该页跳转说明。

---

## 2. 架构位置

```text
ACN 内：Kernel + builtin_work + heartbeat + events
              │
              │ poll open work / org.* / loop tick
              ▼
外部：ClawTeam Org Loop 适配器（本选型，未实现）
              │
              ├── Org work  ←→  CT task（映射）
              ├── Org member / role  ←→  CT worker（可选）
              └── 成功/失败 → governance PATCH work
```

硬约定：

- **不设** `plugins.loop=clawteam`（v0 白名单会拒绝）。  
- 适配器是 **外部 Pattern**，与 Paperclip 同级。  
- Org 身份、成员、待办列表的权威仍在 ACN。

---

## 3. 建议映射（Accepted 方向）

| ACN | ClawTeam | 规则 |
|---|---|---|
| `OrgWorkItem`（open） | CT task / job | 创建时带 `work_id`；幂等 key = `org_id` + `work_id` |
| `assignee` / 角色 | worker 选择 | 优先映射 Org 成员 agent；无映射则本机 ephemeral worker（≠ Membership） |
| work `done` / `failed` | CT 完成/失败 | **仅治理 key** PATCH（v0 API 约束） |
| Org charter / 标题描述 | CT 任务 prompt 前缀 | 只读注入，不在 CT 侧改章程 |

**反模式：**

- 以 ClawTeam kanban 为 SoT，再镜像回 ACN。  
- 把整个 CT 当 `SPAWN_COMMAND` 一行黑盒（层次过低，且双看板冲突）。  
- 在 ACN 进程内热加载 ClawTeam。

---

## 4. 非目标（本选型）

- 实现代码 / examples（有真实需求再开 C0）  
- 替换 `heartbeat` Builtin  
- 接管成员 L1 tool loop  
- 联邦多机 CT 集群

---

## 5. 何时开做

同时满足再立项实现：

1. 有真实 Org 需要「本机多 CLI worker 协调」且成员互派不够；  
2. 待办执行器单命令模型不够用；  
3. 接受治理 key 关单与外部进程运维。

在此之前：用 **heartbeat + 成员 CLI**，或 [Org 待办执行器](./org-loop-spawn-sidecar-poc-v0.md)。
