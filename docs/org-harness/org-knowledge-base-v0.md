# Org 知识库 — 产品与 Port 定义 v0

**Status:** Design Accepted · **K1–K4 examples 已落地** · **K3 `plugins.knowledge` 已接线**  
**Code：** [`examples/org-knowledge/`](../../examples/org-knowledge/)（`read_kb.py` · `contribute_kb.py` · wake 加载）  
**Plugins：** `acn.services.knowledge_patterns`（`noop` | `git`）  
**Smoke：** [`scripts/smoke_org_knowledge.sh`](../../scripts/smoke_org_knowledge.sh)  

**Date:** 2026-07-27  
**Audience:** 产品 / Org Harness 维护者 / Pattern 作者  
**Depends on:** [design-v0.md](./design-v0.md) §5.3 · [plugin-catalog-v0.md](./plugin-catalog-v0.md)  
**Adjacent:** [org-orchestrator-wake-contract-v0.md](./org-orchestrator-wake-contract-v0.md) · [org-orchestrator-member-playbook-v0.md](./org-orchestrator-member-playbook-v0.md)

> **一句话：** 组织知识库是 Org Harness 的**一等能力**（`IOrgKnowledge`）——**主要由成员 agent 贡献与维护**的共享知识资产（SOP、复盘、Skills、编译页），供后续 Work 消费。  
> **不是** Kernel，**不是** `IOrgMemory`（记忆可脏可忘；知识库是组织可治理资产）。  
> **人 / Owner** 管红线与冲突仲裁，**不是**默认主笔。  
> **不自研**检索引擎；用成熟栈（git/markdown、可选 LLM Wiki / Obsidian 前端）做侧车。

### 为什么现在补

Org = 边界 + 活 + **知识**。协作主体是 **agent**，知识也应主要由 agent 在干活中沉淀，而不是假设「人写手册、agent 只读」。  
早期 K1/K2 只做了**读路径**（冷启动）；本文修订把**贡献模型**订正为终局口径。

---

## 1. 要解决什么

成员接到活时需要知道：组织红线、同类活怎么做、可复用技能在哪。  
更重要：干完活后要把**可复用结论**写回组织，而不是只留在单个 agent 的 L1 记忆里。

没有组织知识库 → Org 级真相缺失，也**无法跨成员复利**。

**不做（本 Port）：**

- 对话轨迹 / 偏好抽取 → **`IOrgMemory`**  
- 成员私有笔记 → L1 harness memory  
- 自研语雀/Notion 级 CMS  

---

## 2. 在架构里的位置

```text
Org Graph（Kernel）          ← 不管文档内容
Work / Loop                  ← 读知识干活；干完可提议写入
IOrgKnowledge（本 Port）     ← 组织共享知识（agent 主贡献）
IOrgMemory                   ← 运行沉淀：事实 / 偏好 / 叙事（可脏）
```

| 项 | 决定 |
|---|---|
| 问题轴 | **`IOrgKnowledge`**（与 Memory 并列） |
| Kernel | **不进** |
| SoT（内容） | 知识库后端（git / markdown vault 等）；ACN 认 `org_id` + 指针 / 将来写契约 |
| 插法 | **外部侧车**；`plugins.knowledge` 预留，接线前勿伪造 |
| **贡献者** | **成员 agent 为主**；Owner（人/agent）定红线与仲裁 |
| 自研 | **否** |

### 2.1 贡献模型（Accepted 修订）

| 层 | 谁写 | 说明 |
|---|---|---|
| **charter / 红线** | Owner（人 or agent） | 少而稳；改动宜审 |
| **知识主体**（sop / playbooks / skills / wiki） | **成员 agent** | 完成 work 后提议写入；组织共享 |
| **记忆** | agent 自动抽 | 不进本 Port |

```text
agent 干活 → 读 KB → 产出
           → 提议 contribute（新页 / 补丁）
           → 治理：自动收 | manager 批 | 标争议
           → 写入侧车（git commit / vault 更新）
```

**K1/K2：** 读路径。**K4：** 侧车 `contribute`（成员区自动收；charter 需 Owner；冲突 → `disputed/`）。  
进程内 `plugins.knowledge`：**K3 已接线**（默认 `noop`；`git` 启用侧车契约）。

### 与 Memory 的硬边界

| | **知识库 `IOrgKnowledge`** | **记忆 `IOrgMemory`** |
|---|---|---|
| 性质 | 组织可治理资产（可版本、可争议） | 运行沉淀 |
| 主作者 | **agent（贡献）** + Owner 红线 | agent 自动抽取 |
| 变更 | 提议 → 治理 → 落库 | 可脏、可遗忘、冲突消解 |
| 典型内容 | SOP、复盘、Skills、编译 wiki 页 | 事实、偏好、多会话实体 |
| 消费 | Work / Loop / 成员执行前 | 检索增强、画像 |

### 信任边界（文件系统侧车）

- Sidecar **不**做 ACN Membership 鉴权（部署隔离多租户）。  
- 写路径落地后：须校验「写作者是该 Org 成员」——优先在**治理/编排侧**做，或侧车校 ACN；勿裸写共享盘。  
- 读路径已做：traversal / 出树 symlink 拒绝；单文件默认 ≤512KB；跨 org URI 拒绝。

### 行业对照（简）

通用 SaaS 常假设「人对 KB 只读、Memory 可写」。  
**ACN Org 不同：** 协作主体是 agent → **KB 也应以 agent 写为主**，再用治理区分「可进组织资产」与「仅记忆」。  
Karpathy **LLM Wiki + Obsidian**：适合作为**编译/浏览层配方**（agent 维护互链 markdown；Obsidian 可选前端），与「权威 charter 少而稳」可叠用，**不**单独替代治理。

典型循环（修订后）：

```text
KB.read → 干活 → Memory.add（可选）
                → KB.contribute（提议写入组织知识）
```

---

## 3. 推荐栈（用户可选方向）

| 选项 id（规划） | 适合 | 说明 |
|---|---|---|
| **`git`**（默认推荐） | 组织手册 + agent 以 PR/commit 贡献 | 零新依赖；人可用 Obsidian 打开同一仓 |
| **`llm_wiki`** | agent 编译 raw→wiki | Karpathy 模式配方；Obsidian 看图谱；须叠加治理防幻觉进红线 |
| **`noop`** | 不要组织知识库 | 仅 L1 / Memory |
| 外挂 RAG / 语雀等 | community | 按 `org_id` 隔离后欢迎适配 |

目录约定（`git` / 文件侧车）：

```text
orgs/<org_id>/
  charter.md          # 红线（Owner）
  sop/                # agent 可贡献
  playbooks/
  skills/
  sources/            # 可选：原料（llm_wiki）
  wiki/               # 可选：编译层（llm_wiki）
```

---

## 4. 已交货 vs 其后

### 已交货（K0–K4）

1. 问题轴 + catalog（agent 主贡献）。  
2. 读：侧车 + `kb_refs` + `handle_wake`。  
3. **写：** [`contribute_kb.py`](../../examples/org-knowledge/contribute_kb.py) — 提议落盘 + 最小治理（见 §5.3）。  
4. 与 Memory 拆表。

### 其后

| 项 | 方向 |
|---|---|
| **用户可选** | 创建 Org 货架：`git` / `noop`（`plugins.knowledge` 接线） |
| **llm_wiki** | sources→wiki 编译配方 |
| **真·成员校验** | contribute 前打 ACN Membership（今日信任 runner 断言 `from_agent` / `--as-owner`） |

### 仍不做

- Kernel CRUD 文档 API  
- 自研向量引擎 / 跨 org 联邦  

### `plugins.knowledge`（K3）

```json
{
  "work": "builtin_work",
  "loop": "heartbeat",
  "memory": "noop",
  "knowledge": "git"
}
```

| id | 状态 | 说明 |
|---|---|---|
| `noop` | **wired**（默认） | 无组织知识库；runner 可设 `ORG_PLUGINS_KNOWLEDGE=noop` 跳过读/写 |
| `git` | **wired** + 侧车 examples | 启用 filesystem/git 侧车契约（`read_kb` / `contribute_kb`） |
| `llm_wiki` | **plugin-unavailable**（K5） | Karpathy 配方；可选第二档 |
| 外挂 KB / RAG | **community-welcome** | |

创建 Org 时可显式传 `knowledge=git`；内容仍在侧车，不进 Kernel CRUD。

---

## 5. 消费与贡献契约（最小）

### 5.1 读：`kb_refs`（已有）

```json
{
  "kb_refs": [
    { "uri": "orgkb://<org_id>/sop/release.md", "title": "发版 SOP" }
  ]
}
```

消息只带 refs，不塞全文。

### 5.2 读循环（K2）

```text
wake → 拉 kb_refs / 默认 charter → 干活 → 治理关单
```

### 5.3 写循环（K4 · 已实现侧车）

```text
work done（或阶段性复盘）
  → agent 调用 contribute_kb.py / contribute()
      { org_id, path, body, from_agent, work_id?, title?, as_owner? }
  → 治理：
      - sop|skills|playbooks|wiki|sources → 成员自动 accepted
      - charter.md|charter/ → 需 as_owner，否则 rejected
      - 其它路径 → 非 Owner rejected
      - 目标已存在且内容不同（无 force）→ disputed/<path>
  → 落盘并附 provenance 注释（agent / work / 时间）
```

```bash
python3 contribute_kb.py --org org_x --from-agent agt_1 \
  --path sop/learned.md --body-file ./note.md --work-id work_…
```

**禁止：** 未授权改 charter；信任边界见 §2（runner 须保证 `from_agent` / `--as-owner` 真实）。

---

## 6. 权限与治理（修订基线）

| 动作 | 谁 |
|---|---|
| 读知识 | Org **成员 agent** |
| 贡献 sop/skills/playbooks/wiki | **成员 agent**（经 contribute + 治理） |
| 改 charter / 红线 | **Owner**（人 or agent） |
| 争议仲裁 | Owner / manager |
| 删 org | 随 Org；侧车保留策略由运营定 |

细粒度 path ACL → Policy Port 后置。

---

## 7. 里程碑

| 阶段 | 内容 | 状态 |
|---|---|---|
| **K0** | 问题轴升格 | **done** |
| **K1–K2** | 读侧车 + wake `kb_refs` | **done** |
| **K4** | **agent contribute** + 最小治理（`contribute_kb.py`） | **done** |
| **K3** | 用户可选：`git` / `noop`（`plugins.knowledge`）；可选向量 | **done**（向量后置） |
| **K5** | 可选 `llm_wiki` 配方（sources→wiki，Obsidian 前端） | 按需 |
| **后置** | `plugins.knowledge` 多后端、审批 UI | Phase 3+ |

---

## 8. 决策记录

| # | 决策 |
|---|---|
| K-D1 | 知识库是 **Org 一等能力** |
| K-D2 | Port 名 **`IOrgKnowledge`**；**不进 Kernel** |
| K-D3 | **与 `IOrgMemory` 分离** |
| K-D4 | **不自研**引擎；成熟栈侧车优先 |
| K-D5 | 交货形态优先 **外部侧车**；`plugins.knowledge` 预留 |
| K-D6 | 读消息只传 **`kb_refs`**，不传全文 |
| **K-D7** | **主贡献者是成员 agent**；人/Owner 管红线与仲裁，不是默认主笔 |
| **K-D8** | K1/K2 只读 = 冷启动；终局 = **可读 + 可治理写入** |
| **K-D9** | 默认推荐技术 **`git`**；**`llm_wiki`** 为可选编译配方，不替代治理 |

---

## 9. 相关文档

| 文档 | 关系 |
|---|---|
| [design-v0.md](./design-v0.md) | Port 表与决策 D12 |
| [plugin-catalog-v0.md](./plugin-catalog-v0.md) | Knowledge 短名单 |
| [org-orchestrator-member-playbook-v0.md](./org-orchestrator-member-playbook-v0.md) | 成员读 KB |
| [org-pattern-adapter-spec-v0.md](./org-pattern-adapter-spec-v0.md) | DEF-KB |
| [`../../examples/org-knowledge/`](../../examples/org-knowledge/) | 读/写侧车（`read_kb` · `contribute_kb`） |
| [Karpathy llm-wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) | 可选编译层灵感（非官方依赖） |
