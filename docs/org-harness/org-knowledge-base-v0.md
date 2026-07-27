# Org 知识库 — 产品与 Port 定义 v0

**Status:** Design Accepted · **K1+K2 examples 已落地**；进程内 `plugins.knowledge` 尚未接线  
**Code：** [`examples/org-knowledge/`](../../examples/org-knowledge/) · wake `kb_refs` + `handle_wake` 加载  
**Smoke：** [`scripts/smoke_org_knowledge.sh`](../../scripts/smoke_org_knowledge.sh)  

**Date:** 2026-07-27  
**Audience:** 产品 / Org Harness 维护者 / Pattern 作者  
**Depends on:** [design-v0.md](./design-v0.md) §5.3 · [plugin-catalog-v0.md](./plugin-catalog-v0.md)  
**Adjacent:** [org-orchestrator-wake-contract-v0.md](./org-orchestrator-wake-contract-v0.md) · [org-orchestrator-member-playbook-v0.md](./org-orchestrator-member-playbook-v0.md)

> **一句话：** 组织知识库是 Org Harness 的**一等能力**（问题轴 `IOrgKnowledge`）——权威、可版本的章程 / SOP / Skills，供 Work·Loop·成员**只读**消费。  
> **不是** Kernel，**不是** `IOrgMemory`（记忆是可写沉淀）。  
> **不自研**检索引擎；用成熟栈做侧车 / 将来薄适配。

### 为什么现在补

Org = 人/agent 边界 + 活 + **知识**。前两块已有 Kernel / Work / Loop；知识库在 v0 叙事里被并进 Memory「集体记忆 / SOP」，属于**规划遗漏**。  
补法与其它 Port 相同：**先占问题轴 + 默认推荐路径**，实现用现成技术，不要求「先跑稳再转正」。

---

## 1. 要解决什么

成员接到活时需要知道：

- 组织章程与红线是什么；  
- 这类活的标准作业程序（SOP）怎么走；  
- 可复用的 Skills / playbook 片段在哪。

没有组织知识库，每个 agent 只能靠各自 L1 记忆或口头约定——**Org 级真相缺失**。

**不做（本 Port）：**

- 对话轨迹 / 长期事实沉淀 → **`IOrgMemory`**  
- 成员私有笔记 → L1 harness memory  
- 把语雀/Notion 再实现一遍 → 选型接入

---

## 2. 在架构里的位置

```text
Org Graph（Kernel）          ← 不管文档内容
Work / Loop                  ← 干活时读知识（指针或检索）
IOrgKnowledge（本 Port）     ← 权威知识：章程 / SOP / Skills
IOrgMemory                   ← 运行中沉淀：事实 / 偏好 / 叙事（可脏）
```

| 项 | 决定 |
|---|---|
| 问题轴 | **`IOrgKnowledge`**（与 Memory 并列） |
| Kernel | **不进**；不在 Org 表塞文档 blob |
| SoT（内容） | 知识库后端（git / 对象存储 / 外挂 KB）；ACN 只认 `org_id` 边界与可选指针 |
| 插法 v0 | **外部侧车**（与编排器同级节奏）；`plugins.knowledge` **预留**，接线前勿伪造 id |
| 读写 | v0：**读多写少**；写入走人/Owner 流程（PR、外挂 KB 权限），agent 默认只读 |
| 自研 | **否**——不造向量引擎 / CMS |

### 与 Memory 的硬边界

| | **知识库 `IOrgKnowledge`** | **记忆 `IOrgMemory`** |
|---|---|---|
| 性质 | 权威资产 | 运行沉淀 |
| 变更 | 宜审、可回滚 | 可脏、可遗忘 |
| 典型内容 | charter、SOP、Skills 包 | 事实、偏好、多会话实体 |
| 消费 | Work / Loop / 成员执行前注入 | 检索增强、画像 |
| 默认 | 见 §4（侧车推荐） | 今日 `plugins.memory=noop` |

可以共用同一向量库做索引，但**产品与 Port 名必须分开**，避免 SOP 被当成聊天记忆乱写。

### 信任边界（文件系统侧车）

- Sidecar **不**做 ACN Membership / 角色鉴权。  
- 能读 `ORG_KB_ROOT` 即可读其下任意 `orgs/<org_id>/`。  
- 多租户须在**部署层**隔离（每 runner 一 org，或受控挂载）；勿把不可信进程挂到共享多 org root。  
- 实现侧已做：path traversal / 出树 symlink 拒绝；单文件默认 ≤512KB；`--org` / `expected_org_id` 拒绝跨 org URI。

### 行业对照（2025–2026）

主流结论：**知识库（RAG）与 Agent Memory 不是竞品，是两套生命周期；生产系统几乎都「两个都要」。**  
分水岭是有没有 **agent 写回路径**，不是用不用向量库。

| | **知识库 / RAG** | **Agent Memory** |
|---|---|---|
| 回答 | 「文档/制度说什么？」 | 「关于这个人/这段协作，我记得什么？」 |
| 读写 | 对人/运营可写；对 agent **基本只读** | agent **持续写**：抽取、更新、遗忘 |
| 作用域 | 人人（或按 org）一样 | 按 user / session / agent（需租户隔离） |
| 失败模式 | 检索错、索引旧 | 记忆投毒、矛盾堆积 |

代表口径（与本 Port 对齐）：

- **Mem0：** RAG = 通用/领域知识；Memory = 用户专属上下文；推荐 hybrid（先记忆、再文档、再进 prompt）。  
- **AWS Bedrock AgentCore：** LTM 管「谁是用户、以前发生过什么」；Knowledge Base/RAG 管「权威源现在怎么说」。  
- **Zep / Graphiti：** 偏 Memory（时序图谱、对话与业务数据持续写入）；与静态文档 GraphRAG 刻意区分。  
- 另有 **LLM Wiki** 一类：ingest 时编译领域笔记——仍属权威/领域层，不是会话记忆。

典型请求循环：

```text
Memory.search(user|org) → KB.retrieve(query) → LLM(两路 context) → Memory.add(本轮)
                                                                    （不写回 KB）
```

冲突习惯：**人设/偏好听 Memory，事实/合规听 Knowledge。**  
映射：行业 KB/RAG → **`IOrgKnowledge`**；行业 Agent Memory → **`IOrgMemory`**；会话窗口 → 成员 L1。

---

## 3. 推荐栈（不自研）

| 层级 | 推荐 | 说明 |
|---|---|---|
| **P0 默认路径** | **git / 文件树** 按 `org_id` 隔离 | 人用 PR 审；agent 按 path 读。零新依赖。 |
| **P1 检索** | 侧车 + PG+vector / 现成 RAG API | 仍按 `org_id` 过滤；ACN 不持有全文索引 |
| **外挂 KB** | 语雀 / Notion / 飞书文档等 | community：用其 API 做只读适配；官方不绑定一家 |

目录约定（P0 侧车示例）：

```text
orgs/<org_id>/
  charter.md          # 章程 / 红线
  sop/                # 标准作业
  playbooks/          # 场景剧本
  skills/             # 可复用技能片段
```

---

## 4. v0 范围

**做（设计 + 推荐路径）：**

1. 问题轴与 catalog 正式立项（本文 + design / plugin-catalog 回链）。  
2. **P0 交付形态：** 外部知识库侧车（文件/git）；成员 playbook：接活 → 按指针读条目 → 再执行。  
3. **可选指针：** 编排器 / 发单方在 work 或 `acn.org.work_wake` 上带 `kb_refs[]`（path 或 URI），**不塞全文**进 ACN 消息。  
4. 与 Memory 拆表：SOP/Skills **移出** Memory 短名单，归本 Port。

**不做（v0）：**

- 进程内 `plugins.knowledge=*` 白名单接线（与 Memory 薄适配同批，Phase 3 / 有需求时）  
- Kernel CRUD 文档 API  
- 自研 embedding / 图谱引擎  
- 跨 org 知识联邦  

### `plugins.knowledge` 预留

与 `plugins.memory` 对称，设计上预留：

```json
{
  "work": "builtin_work",
  "loop": "heartbeat",
  "memory": "noop",
  "knowledge": "noop"
}
```

| id（规划） | 状态 | 说明 |
|---|---|---|
| `noop` | **plugin-planned**（未接线） | 无组织知识库；成员自行找文档 |
| `fs` / git 侧车（官方示例） | **examples-shipped**（K1+K2） | 推荐默认交货；见 [`examples/org-knowledge/`](../../examples/org-knowledge/) |
| 外挂 KB / RAG | **community-welcome** | 按 `org_id` 隔离即可 |

今日创建 Org **不要**传 `knowledge: …`（未知键应被忽略或拒绝——以实现为准）；开工请走**外部侧车**，与「勿伪造 `plugins.memory=mem0`」同一纪律。

---

## 5. 消费契约（最小）

### 5.1 `kb_refs`（可选，给 Work / wake）

```json
{
  "kb_refs": [
    { "uri": "orgkb://<org_id>/sop/release.md", "title": "发版 SOP" },
    { "uri": "orgkb://<org_id>/charter.md" }
  ]
}
```

- `orgkb://` 为逻辑 scheme；侧车解析为本地 path 或外挂 URL。  
- 唤醒消息只带 refs；全文由成员侧拉取。  
- 无 `kb_refs` 时：成员按 playbook 默认读 `charter.md` + 与 work 类型相关的 sop（侧车约定）。

### 5.2 成员侧最小循环

```text
收到 work / wake
  → 解析 kb_refs（或默认路径）
  → 只读拉取片段
  → 执行并回写 work
  → （可选）事实沉淀写入 Memory——不写回 Knowledge
```

---

## 6. 权限与治理（v0 基线）

| 动作 | 谁 |
|---|---|
| 读知识 | Org 成员 agent（及 Owner 工具链） |
| 改章程 / SOP | **人 Owner 或外挂 KB 的人类权限**；v0 不强制 agent 写入 API |
| 删 org | 随 Org 生命周期；侧车数据保留策略由运营定（ACN 不级联删外挂仓） |

细粒度 ACL（按 path 的 manager-only）→ 后置，有真实需求再进 Policy Port。

---

## 7. 里程碑

| 阶段 | 内容 | 状态 |
|---|---|---|
| **K0** | 本文 + design/catalog 升格问题轴 | **done** |
| **K1** | `examples/org-knowledge/`：目录约定 + 读文件 helper + playbook 一段 | **done** |
| **K2** | wake 可选 `kb_refs`；编排器可附带；`handle_wake` 加载 sidecar | **done** |
| **K3** | 可选向量检索侧车；或 `plugins.knowledge` noop 接线 | 按需 |
| **后置** | 真·进程内多后端、审批流改章程 | Phase 3+ |

---

## 8. 决策记录

| # | 决策 |
|---|---|
| K-D1 | 知识库是 **Org 一等能力**，规划遗漏现补；不与「等契约跑稳」挂钩 |
| K-D2 | Port 名 **`IOrgKnowledge`**；**不进 Kernel** |
| K-D3 | **与 `IOrgMemory` 分离**；SOP/Skills 归知识库 |
| K-D4 | **不自研**引擎；成熟栈侧车优先 |
| K-D5 | v0 交货 = **外部侧车**；`plugins.knowledge` 预留，接线前不伪造 |
| K-D6 | 消息只传 **`kb_refs`**，不传全文 |

---

## 9. 相关文档

| 文档 | 关系 |
|---|---|
| [design-v0.md](./design-v0.md) | Port 表与决策 D12 |
| [plugin-catalog-v0.md](./plugin-catalog-v0.md) | Knowledge 短名单 |
| [org-orchestrator-member-playbook-v0.md](./org-orchestrator-member-playbook-v0.md) | 成员读 KB 的挂载点 |
| [org-pattern-adapter-spec-v0.md](./org-pattern-adapter-spec-v0.md) | DEF-KB / DEF-MEM 拆分 |
| [`../../examples/org-knowledge/`](../../examples/org-knowledge/) | K1 文件系统侧车（`read_kb.py` + `org_demo`） |
