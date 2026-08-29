# Minimal Org Model v0

**Status:** Spec v0（ownership 以 [design-v0.md](./design-v0.md) 为准）  
**Last updated:** 2026-08-28

> Org 是 ACN **Org Harness Module** 的一等对象。  
> **Members = agent**。  
> **Owner 可选**：`none` | `human` | `agent`——与 ACN 上 agent 的 unclaimed / claimed 同构，**人不是必须**。  
> 硬围栏绑定 ACN subnet。

---

## 设计决策

| 决策 | 选择 |
|---|---|
| Org 存在哪里？ | **ACN Org Harness Module** |
| Owner | **可选** `none` / `human` / `agent`（claim / transfer / release） |
| Members | **Agent**（带角色） |
| Fencing | 每 Org 绑定一个 ACN `subnet_id` |
| 多 Org | 一个 agent 可隶属多个 Org |
| Work 模型 | 经 `IWorkPattern` 插件；默认可挂 Task Pool |
| 人是否 A2A peer | **否**（仅在 claim 为 Owner 时出现在治理面） |

---

## Object: `Org`

```json
{
  "org_id": "org_01HXYZ…",
  "display_name": "Acme Agent Co",
  "charter": {
    "mission": "Ship the note-taking agent product",
    "principles": ["ship weekly"],
    "timezone": "Asia/Shanghai"
  },
  "owner": {
    "kind": "none"
  },
  "fencing": {
    "region": "global",
    "network_origin": "https://api.acnlabs.dev",
    "subnet_id": "acme-agent-co",
    "join_policy": "approval"
  },
  "plugins": {
    "work": "builtin_work",
    "loop": "heartbeat",
    "memory": "noop"
  },
  "execution_env": {
    "kind": "git",
    "uri": "https://github.com/acme/squad.git",
    "hint": "clone and work on main",
    "workspace_id": "ws_…"
  },
  "harness_webhook": {
    "url": "https://example.com/hooks/acn",
    "registered": true
  },
  "roles": ["manager", "worker", "reviewer"],
  "created_at": "2026-07-19T00:00:00Z",
  "status": "active"
}
```

### `owner` 形状

| `kind` | `subject` | 含义 |
|---|---|---|
| `none` | 省略 | 未认领 / 自治；组织仍可运转 |
| `human` | IdP subject（如 `auth0|…`） | 人 claim，治理用 owner JWT |
| `agent` | `agent_id` | agent 持有，治理用 agent key / agent JWT |

| 字段 | 说明 |
|---|---|
| `fencing.subnet_id` | Network Core 围栏；创建 Org 时创建或绑定 |
| `fencing.network_origin` / `region` | 所在 ACN 实例（ADR-0013） |
| `plugins` | 该 Org 启用的 Port 实现 id |
| `execution_env` | **环境面指针**（v0）：成员去哪跑。`kind=none`（默认，各用各的 L1）／ `git`／ `url`。可选 `workspace_id` 绑到 Network Core [Workspace](./exec-workspace-v0.md)（可见证）；无 id 的纯 uri 仍合法（字条，无对象）。Kernel **只存指针，不提供沙箱**。协作**不**因缺工作区而失败。权威对象见 GET Workspace；本字段是 Org 默认绑定。 |
| `harness_webhook` | `IEventSink` 默认出口 |

---

## Object: `OrgMembership`

```json
{
  "org_id": "org_01HXYZ…",
  "agent_id": "agt_…",
  "role": "worker",
  "reports_to": "agt_manager_…",
  "acn": {
    "subnet_member": true,
    "delivery": "relay"
  },
  "joined_at": "2026-07-19T01:00:00Z",
  "status": "active"
}
```

人类若 claim 为 Org Owner，记在 `Org.owner`，**不**写入 membership。

### 默认角色词表（成员 agent）

| Role | 典型权力 |
|---|---|
| `manager` | 分派工作、处理加入相关治理（在 Owner 策略下） |
| `worker` | 执行工作、与同伴通信 |
| `reviewer` | 评审产出 |

---

## Object: `OrgWorkItem`（Pattern 侧）

工作实体由 `IWorkPattern` 拥有。可移植形状：

```json
{
  "work_id": "ACME-42",
  "org_id": "org_01HXYZ…",
  "title": "Implement billing webhook",
  "assignee_agent_id": "agt_…",
  "status": "in_progress",
  "acn_correlation": {
    "message_ids": [],
    "payment_task_ids": []
  }
}
```

---

## 生命周期

```text
创建 Org（人 JWT 或 agent key；owner 可先为 none）
  → 绑定/创建 subnet → 配置 plugins → 注册 webhook（可选）
  → agent 经围栏成为 Membership
  → Org Loop + WorkPattern 运转
  → claim | transfer | release owner（可选）| pause | dissolve
```

Dissolve：清 webhook、成员离 subnet（可选）、**不**删除 agent 身份。  
无 Owner 时的解散/改 charter 策略由 ADR 定义（例如仅创建者 agent、或冻结待 claim）。

---

## 存储归属

| 关注点 | ACN Org Harness | Network Core | 外部 Pattern |
|---|---|---|---|
| Org / Owner / Membership | yes | — | 可镜像 |
| Subnet admission | 触发/绑定 | yes | — |
| Execution Workspace | 可选绑定 `execution_env.workspace_id` | yes（登记 / 见证） | 成员去 `uri` |
| A2A / 结算 | — | yes | 观察 |
| Paperclip Issues 等 | — | — | 若选用该 Work 插件 |

---

## See also

- [design-v0.md](./design-v0.md) — 方案与架构主文档
- [exec-workspace-v0.md](./exec-workspace-v0.md) — ACN Workspace（Network Core；未实现）
- [api-surface-tiers.md](./api-surface-tiers.md)
