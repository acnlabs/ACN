# ClawTeam × Org Loop — 选型短文 + POC 草图 v0

**Status:** Selection draft — **not started**（确认后再写代码）  
**Date:** 2026-07-25  
**Audience:** Org Harness 维护者 / Pattern 作者  
**Depends on:** [design-v0.md](./design-v0.md) §5–§6 · [plugin-catalog-v0.md](./plugin-catalog-v0.md) · [org-pattern-adapter-spec-v0.md](./org-pattern-adapter-spec-v0.md)

> 目标：给「纯 agent 组织」一条官方可试路径，**不**再造 Paperclip 式人类看板。  
> 结论倾向：**外部侧车适配 ClawTeam 作 Loop/执行器**；Work 仍用 `builtin_work`。

---

## 1. 问题

| 已有 | 缺口 |
|---|---|
| Paperclip | 人 + 公司 Issues；agent 不是一等驾驶舱 |
| `builtin_work` + skill/CLI | agent 能派活/改状态，但**没有**「tick → 拉起多 worker」的执行器 |
| `POST …/loop/tick` + `org.loop_tick` | 节拍有了；**谁**在本地 spawn 成员还没官方样板 |

一句话：Org 身份/工单在 ACN；**回合内怎么打一仗**交给 agent-native Pattern。

---

## 2. 选型（已倾向）

| 候选 | 挂点 | 决定 |
|---|---|---|
| **ClawTeam** | **Loop / 执行器**（spawn CLI workers） | **POC 首选** — design-v0 D7；偏 agent |
| LangGraph / CrewAI / Swarm | Work Graph 策略 | **延后** — 除非已有图编排生产路径 |
| 只加强 skill | — | 不够覆盖「多 worker 被 tick 拉起」 |
| `plugins.loop=clawteam` 进程内 | Kernel registry | **不做** — 硬规则：自定义 = 外部 Pattern/侧车 |

```text
ACN Org (Kernel)
  ├── plugins.work = builtin_work     ← 不变
  ├── plugins.loop = heartbeat          ← ACN 薄 tick 不变
  └── subnet harness / poll
            │ org.loop_tick / org.work_*
            ▼
   clawteam-acn-adapter（外部侧车，POC）
            │ spawn / wake
            ▼
   成员 L1（OpenHarness / Claude Code / …）
```

**硬边界：** Org 身份、成员、钱包、work 状态机仍在 ACN。  
ClawTeam **不**成为 Org Harness 平替；**不**把 Project/Workspace 上提进 Kernel。

---

## 3. POC 场景（最小可验）

**场景名：** Org tick → 打开 open work → ClawTeam 拉起一个 worker → worker 把 work 标 `done`。

| 步骤 | 谁做 |
|---|---|
| 1. 治理方 `acn org work create`（或 API） | ACN |
| 2. `acn org tick`（或 cron）→ `org.loop_tick` | ACN |
| 3. 侧车收事件（webhook **或** poll open work，同 Paperclip S4b） | Adapter |
| 4. 对每条 open work：spawn/唤醒一个 CLI worker（ClawTeam 语义） | Adapter + ClawTeam |
| 5. Worker 用成员 `ACN_API_KEY`：`PATCH …/work/{id}` → `done` | 成员 agent |
| 6. （可选）失败 → work 保持 `todo`/`in_progress` + 侧车日志 | Adapter |

**验收（狗粮）：** 一脚本或 README 步骤；本地 ACN 或 hosted CN；**不**要求公网 webhook（poll 优先）。

---

## 4. 非目标（POC）

- 不进 ACN 进程内 plugin registry；不设 `plugins.loop=clawteam`
- 不替换 `builtin_work`；不镜像 Task Pool
- 不做人类看板 / 审批 / Org Memory
- 不做完整 ClawTeam 功能对等；只接「tick → spawn → 回写 work」
- 不把短命 worker 写成 `OrgMembership`
- 不在本 POC 做 LangGraph / Swarm 并行适配

---

## 5. 接口草图（侧车）

配置（示意）：

```yaml
acnBaseUrl: https://acn.acnlabs.cn   # 或 global
acnOrgId: org_…
acnApiKey: acn_…                     # 治理或只读+成员分派策略另定
mode: poll                           # poc 默认；webhook 可选
pollIntervalSec: 30
clawteam:
  # 指向本地 ClawTeam / worker 入口（具体 CLI 以上游为准）
  spawnCommand: "…"
```

入站（二选一，POC 先 poll）：

1. **Poll：** `GET /orgs/{id}/work?open_only=true`  
2. **Push：** subnet harness HMAC → `org.loop_tick` / `org.work_created`

出站：

- 成员身份调用 `PATCH /orgs/{id}/work/{work_id}`（status）  
- **不**写 `/api/v1/tasks/*` 做组织内派活

与 Paperclip 对照：

| | Paperclip adapter | ClawTeam adapter（本 POC） |
|---|---|---|
| 主用户 | 人 | agent / CLI worker |
| Work 映射 | Issue ↔ Org work | work id → spawn 参数 |
| UI | 有 | 无（日志即可） |
| 仓库形态 | `@acnlabs/paperclip-plugin-acn` | 新 repo 或 `examples/clawteam-acn-adapter`（待定） |

---

## 6. 切片（确认后）

| # | 切片 | 产出 |
|---|---|---|
| **C0** | 本短文 Accepted | 链接进 README + catalog 状态 → `adapter-planned` |
| **C1** | 空侧车 + poll open work + 日志 | 无 spawn，证明事件/列表通路 |
| **C2** | spawn 一次成功路径 + PATCH done | 狗粮脚本绿 |
| **C3** |（可选）webhook HMAC；失败重试/幂等 | 再开 |

估计：C0 本文；C1–C2 约 **1–2 人日**（视 ClawTeam CLI 稳定度）。

---

## 7. 待你拍板

1. **仓库：** 独立 `clawteam-acn-adapter` vs ACN 仓 `examples/`？  
2. **身份：** spawn 时用**治理 key**代劳 PATCH，还是必须**成员 agent key**？  
3. **上游：** POC 钉 ClawTeam 某 tag/commit，还是「兼容任意 spawnCommand」？

拍板后改 Status → Accepted，再开 C1。
