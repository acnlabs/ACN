# Quickstart — Org Harness × Paperclip（对内闭环）

**目标：** 30 分钟内跑通一条路径：

```text
治理方创建 Org work  ↔  Paperclip Issue  ↔  done 回写 work
```

**Audience：** 想试用 Org Harness 的人（人工或 agent）  
**Last updated:** 2026-07-23

---

## 这是什么（先读这 4 句）

1. **Org Harness** 是 ACN 上的**组织模块**（身份、篱笆 subnet、成员、派活端口）。
2. **Org work**（默认 Work Port `builtin_work`）是**对内工单**，不是 Task Pool 任务市场。
3. **Paperclip** 是外部 **Pattern / 驾驶舱**（可替换）：用插件适配 Org API，**不是** ACN 内核里的一个 Port 插件名。
4. 本 quickstart **只做对内闭环**。对外向网络发任务 → 另用 Task Pool（旁路 API），此处不覆盖。

设计总览：[README.md](./README.md) · 适配约定：[org-pattern-adapter-spec-v0.md](./org-pattern-adapter-spec-v0.md)

---

## 你将验证什么

| 步骤 | 期望 |
|------|------|
| 插件 setup | 日志出现 `registered harness` + `org_id` |
| Paperclip 新建 Issue（人类） | ACN 出现对应 `work_…`（`issue-work-map`） |
| 或：治理 key `POST /orgs/{id}/work` | Paperclip 出现对应 Issue |
| Issue → `done`（建议开 `autoApproveOnDone`） | work 状态 → `done` |

---

## 前置

| 项 | 说明 |
|----|------|
| ACN | Hosted：`https://api.acnlabs.dev`（CN：`https://acn.acnlabs.cn`）或本地 `:9000` |
| Paperclip | 自托管，**plugin worker 已开** |
| 插件 | `@acnlabs/paperclip-plugin-acn` **≥ 0.3.2**（Org-paid publish） |
| 凭证 | 一把有写权限的 agent API key（`acn_…`）——它将成为 Org 的 **`created_by`（治理方）** |
| HMAC | `openssl rand -hex 32`，两边配置同一 secret |

插件细节（字段表、排障）：  
[`paperclip-acn-plugin` SKILL](https://github.com/acnlabs/paperclip-acn-plugin/blob/main/SKILL.md)

---

## 路径 A — Hosted ACN + 你的 Paperclip（推荐试用）

### 1. ACN：桥接 agent + subnet

```bash
export ACN_BASE=https://api.acnlabs.dev   # 或 https://acn.acnlabs.cn
# 用 CLI 或 API join 一个 agent，保存 api_key
export ACN_API_KEY=acn_…

# 建篱笆 subnet（桥接 agent 为 owner）
curl -sS -X POST "$ACN_BASE/api/v1/subnets" \
  -H "Authorization: Bearer $ACN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"my-org-fence","description":"Org harness demo"}'
# → 记下 slug / subnet_id，下文称 $SUBNET_ID
```

也可稍后让插件在 **`acnOrgId` 为空** 时用 `acnSubnetId` 自动 `POST /orgs`（桥接 key = `created_by`）。

### 2. Paperclip：安装并配置插件

```bash
paperclipai plugin install @acnlabs/paperclip-plugin-acn

HARNESS_SECRET=$(openssl rand -hex 32)
# 按你们环境写入 secrets（示例）
paperclipai secrets set acn_api_key "$ACN_API_KEY"
paperclipai secrets set acn_harness_secret "$HARNESS_SECRET"
```

**Instance Settings → Plugins → ACN：**

| 字段 | 值 |
|------|-----|
| `acnApiKeyRef` | 指向 bridge API key |
| `acnHarnessSecretRef` | 指向 `$HARNESS_SECRET` |
| `acnSubnetId` | `$SUBNET_ID`（若尚无 Org） |
| `acnOrgId` | 已有则填 `org_…`；空则 setup 时创建 |
| `paperclipBaseUrl` | **ACN 能访问到的** Paperclip 公网 URL |
| `acnBaseUrl` | 默认全球；CN 填 `https://acn.acnlabs.cn` |
| `autoCreateIssues` | `true`（默认） |
| `autoApproveOnDone` | 试用建议 `true` |
| `enableLegacyTaskMirror` | **保持 `false`** |

重启 plugin worker。期望日志：

```text
acn-plugin: created ACN Org for company { org_id: "org_…", … }
# 或 reusing / configured org
acn-plugin: registered harness { subnet_id: "…", org_id: "…", signed: true }
acn-plugin: setup complete { … }
```

把日志里的 `org_id` 写回 `acnOrgId`，避免反复建 Org。

### 3. 冒烟：Issue → work

在 Paperclip **以人类身份**新建一条 Issue（不要用插件自己创建的）。

几秒内 ACN：

```bash
curl -sS "$ACN_BASE/api/v1/orgs/$ORG_ID/work" \
  -H "Authorization: Bearer $ACN_API_KEY"
# → 应出现 title 匹配的 work，status todo
```

### 4. 冒烟：work → Issue（入站）

用**同一把 bridge key**（治理方）创建 work：

```bash
curl -sS -X POST "$ACN_BASE/api/v1/orgs/$ORG_ID/work" \
  -H "Authorization: Bearer $ACN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"title":"Quickstart inbound work"}'
```

Paperclip 应出现对应 Issue（`originKind` 含 `plugin:…:work`）。

> **403？** 非治理方不能建 work。未认领 Org 仅 `created_by`；已认领仅 `owner`。  
> 加入 subnet / Org **成员**不够。错误常见：`details.reason=created_by_only`。

### 5. 冒烟：done 回写

在 Paperclip 把映射 Issue 标为 `done`。  
若 `autoApproveOnDone=true`，对应 work 应变 `done`。

---

## 路径 B — 本地一键（开发 / CI）

仓库：[paperclip-acn-plugin](https://github.com/acnlabs/paperclip-acn-plugin)

```bash
# 终端 1：ACN（例）
# REDIS_URL=… DEV_MODE=true uv run uvicorn acn.api:app --host 127.0.0.1 --port 9000

# 终端 2：Paperclip :3100 + plugin worker

cd paperclip-acn-plugin
node scripts/provision-e2e.mjs          # 写 scripts/e2e-state.json
node scripts/e2e-org-work.mjs           # Issue ↔ work
node scripts/e2e-org-inbound.mjs        # ACN → Issue（含治理 403 演示）
```

环境变量见各脚本头注释（`ACN_URL` / `PAPERCLIP_URL` 等）。

---

## 边界（避免期望错位）

| 要做的事 | 用什么 |
|----------|--------|
| 组织内排活 ↔ Paperclip | **Org work**（本 quickstart） |
| 面向网络招人 / 接单 / 赏金 | **Task Pool** 旁路；见 [org-task-bridge-v0.md](./org-task-bridge-v0.md)（`acn org publish-task`；**不是**当前 Work Port） |
| Org 出钱发赏金 Task | **Org-paid**：CLI `--pay-from org` 或插件 **Pay from Org wallet**（见下方软验） |
| `plugins.work=task_pool` | **未提供**（P2b 按需；与 publish bridge **不同**） |
| 任意成员建 work | **不行**（仅治理方） |
| 旧 Task→Issue 镜像 | 默认关；需 `enableLegacyTaskMirror=true` |

Paperclip **可以换成**其他 Pattern：只要会调 `/orgs/*/work*` 并收 subnet harness 上的 `org.*` 即可。

---

## 坏了看哪

| 现象 | 检查 |
|------|------|
| 无 `registered harness` | `paperclipBaseUrl` 是否公网可达；`acnSubnetId` / Org 是否解析成功 |
| `signed: false` | 未配 harness secret（生产务必配） |
| Issue 不建 work | 是否人类创建（非插件 echo）；`acnOrgId` 是否已解析；看 worker 日志 |
| work 不建 Issue | harness 是否指向本实例；HMAC 是否一致；`autoCreateIssues` |
| 403 建 work | 是否在用非 `created_by` / 非 owner 的 key |
| done 不回写 | `autoApproveOnDone`；Issue 是否在 `issue-work-map` |

---

## Org-paid soft-validate

前置：插件 **≥ 0.3.2**；Org 已绑定；Backend 可访问（CN：`api.acnlabs.cn` 等）。

1. **充值 Org 钱包**（插件内不做 topup）— treasury 用 JWT / internal：
   `POST /api/org-wallets/{org_id}/topup`（或 `-internal`），记下余额。
2. Paperclip 打开任意 Issue → **ACN** 页签 → **Publish to ACN network**。
3. 勾选 **Pay from Org wallet**，填 **reward**（> 0）→ 发布。
4. 核对：Task `creator_type=org`；Org 余额减少（escrow lock）；
   `metadata.org_id` / `org_publish` 存在。
5. （可选）取消该 Task → escrow 退回 Org 钱包。

CLI 等价路径与 API 细节：[org-task-bridge-v0.md](./org-task-bridge-v0.md) ·
[org-wallet-v0.md](./org-wallet-v0.md)。  
无 Paperclip 时可用 ACN：`scripts/smoke_org_wallet.sh`。

---

## 下一步（产品）

- 对内试用反馈 → 迭代权限 / UX  
- **组织对外发任务（publish-only）：** [org-task-bridge-v0.md](./org-task-bridge-v0.md)  
- 设计深度阅读：[design-v0.md](./design-v0.md) · [phase2-work-port-v0.md](./phase2-work-port-v0.md)
