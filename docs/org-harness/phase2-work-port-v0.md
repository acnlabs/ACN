# Phase 2 — Work Port（短方案）

**Status:** Agreed for implementation sequencing（2026-07-21）  
**Depends on:** [design-v0.md](./design-v0.md) §5 / §10, [ADR-0014](../adr/0014-org-harness-module.md) D5–D7  
**Audience:** 实施与审阅；先定边界，再写代码

---

## 一句话

Phase 1 盖好了「组织」这栋房子。  
Phase 2 给「怎么派活」装**可换插座**——先把**现在这套小工单**插上当默认，再按需接 Task Pool / Paperclip，不拆房重建。

---

## 已锁定的选择

| 题 | 决定 |
|---|---|
| 默认 Work 实现 | **`builtin_work`** = 现有 `OrgWorkItem` + `/api/v1/orgs/{id}/work*`（不是一上来换成 Task Pool） |
| Task Pool | **可选** Builtin：`plugins.work = task_pool`，仅 **进程内**调用（ADR-0014 D5）；外部 Pattern 仍 **禁止**依赖 `/api/v1/tasks/*` |
| Paperclip | 迁到 Org work + `org.*` 事件；Task 镜像标为 legacy，逐步停写 |
| Plugin 宿主 | Phase 2 只要**最小 resolve**（按 `org.plugins` 找实现 + 未知 id 报错）；完整发现/版本/热加载 → Phase 3 |
| 短命子代理 | ≠ `OrgMembership`；WorkPort 若以后支持，也不自动进成员表 |
| 现网 CN | Redis-only；默认路径不得强依赖 Postgres Task 表 |

---

## 切片（按序）

### P2a — 插座 + 默认插件（必做）

1. 定义 `IWorkPattern`（及 Loop 侧对 Port 的只读依赖）。
2. `builtin_work` 包装现有 create/list/update work。
3. `org.plugins.work` 默认 `builtin_work`；create/update 可改，非法 id → 明确错误。
4. `loop/tick` 只通过 Port 读 open work（对外行为与今日一致）。
5. **验收：** `scripts/smoke_org_kernel.sh` 零改动仍绿；相关单测绿。

### P2b — Task Pool 可选适配（按需）

6. `plugins.work=task_pool` → in-process 适配器。  
7. 文档写清：选用 Task Pool ≠ 外部 Pattern 可以绑 `/tasks/*`。  
8. 若需双写/迁移，另开附录，不阻塞 P2a。

### P2c — Paperclip WorkPort（可并行）

9. ~~Adapter 读写 Org work + 消费 `org.work_*` / `org.loop_tick`。~~（`paperclip-acn-plugin` C2）  
10. ~~Issue ↔ `OrgWorkItem` 映射；**新链路不写 Task 镜像**。~~（C0/C1 + C3 status PATCH）  
11. ~~更新 [org-pattern-adapter-spec-v0.md](./org-pattern-adapter-spec-v0.md)：bootstrap 以 `POST /orgs` 为准；验收「issue → agent run」不经 Task Pool。~~  

---

## 明确不进 Phase 2

- Memory / Policy / Capability 真插件化  
- ClawTeam / Swarm 适配器  
- 完整 Plugin 宿主（市场、版本、热加载）  
- DEF-ORG-LUA、Federation、Dispute、跨 Org 信誉  

---

## 验收清单

| 项 | 通过标准 |
|---|---|
| 默认路径 | `smoke_org_kernel.sh` 通过 |
| 切换 | 两 Org 可配不同 `plugins.work`，互不影响（P2b 落地后含 task_pool） |
| 外部契约 | Paperclip 新路径不调用 `/api/v1/tasks/*`（P2c） |
| 回归 | 私有 Org ACL、Redis fence NX 相关测试仍绿 |

---

## 和旧文案的关系

design-v0 §10 原写「Task Pool 收编为默认」——**收编仍做，但默认实现改为 `builtin_work`**；Task Pool 是可选 Builtin，不是默认。  
详见本页「已锁定的选择」。
