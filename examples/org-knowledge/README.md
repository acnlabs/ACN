# Org 知识库（外部侧车）— 读（K1/K2）+ 贡献（K4）

按 `org_id` 的**文件系统 / git** 知识树。  
**成员 agent 主贡献**：`contribute_kb.py` 写入 `sop|skills|playbooks|wiki|sources`；`charter` 需 `--as-owner`；冲突进 `disputed/`。  
**不是** ACN Kernel，**不是** Memory。  
ACN Org 可配 `plugins.knowledge=git|noop`（K3）；侧车内容仍在本目录。Runner 可设 `ORG_PLUGINS_KNOWLEDGE=noop` 拒绝 contribute / 跳过 wake 加载。

| 文档 | 链接 |
|---|---|
| Port 定义 | [org-knowledge-base-v0.md](../../docs/org-harness/org-knowledge-base-v0.md) |
| 唤醒契约（`kb_refs`） | [org-orchestrator-wake-contract-v0.md](../../docs/org-harness/org-orchestrator-wake-contract-v0.md) |
| 成员 playbook | [org-orchestrator-member-playbook-v0.md](../../docs/org-harness/org-orchestrator-member-playbook-v0.md) |

## 信任边界（必读）

- 本侧车**不**查 ACN Membership / 角色。  
- 能跑进程且能读 `ORG_KB_ROOT` 的主体，就能读其下**任意** `orgs/<org_id>/`。  
- **不要**把多租户知识树裸放在同一 root 上，再交给不可信 runner。部署上按 org 隔离挂载（或每 runner 只挂一个 org）。  
- Path traversal 与指向树外的 symlink 会被拒绝；单文件默认上限 **512KB**（`ORG_KB_MAX_FILE_BYTES`）。

## 目录约定

```text
$ORG_KB_ROOT/orgs/<org_id>/
  charter.md
  sop/
  playbooks/
  skills/
```

默认 `ORG_KB_ROOT` = 本目录下 `data/`（含示例 `org_demo`）。

## 环境变量

| 变量 | 说明 |
|---|---|
| `ORG_KB_ROOT` | 知识树根；默认 `./data` |
| `ORG_KB_ORG_ID` | 默认 org；与 `--org` 一起会**钉死** URI 的 org_id |
| `ORG_KB_MAX_FILE_BYTES` | 单文件上限，默认 `524288` |
| `ORG_PLUGINS_KNOWLEDGE` | 显式 `noop` 时 `contribute` 拒绝（`knowledge_plugin_noop`） |

## 试用

```bash
cd examples/org-knowledge

python3 read_kb.py --org org_demo
python3 read_kb.py --org org_demo --ref orgkb://org_demo/sop/release.md

# 跨 org URI 会被拒绝
python3 read_kb.py --org org_demo --ref orgkb://org_other/charter.md   # exit 1

echo '{"kb_refs":[{"uri":"orgkb://org_demo/charter.md"},{"uri":"orgkb://org_demo/sop/release.md","title":"发版"}]}' \
  | python3 read_kb.py --org org_demo --from-json -
```

## 贡献（K4）

```bash
# 成员：自动落到 sop/
python3 contribute_kb.py --org org_demo --from-agent agt_1 \
  --path sop/learned.md --body '# What we learned\n\n…' --work-id work_…

# charter 仅 Owner
python3 contribute_kb.py --org org_demo --from-agent agt_owner --as-owner \
  --path charter.md --body-file ./charter.md

# 冲突（目标已有不同内容）→ disputed/… ；--force 可覆盖
```

信任：侧车**不**校验 ACN 成员身份；生产上由 runner/编排器保证 `--from-agent` / `--as-owner` 真实。

本地 smoke（无 ACN）：

```bash
./scripts/smoke_org_knowledge.sh
```

## 与编排器（K2 读 / K4 写）

- 编排器信封可带可选 `kb_refs[]`（work 字段 / `ORG_KB_REFS_JSON` / `ORG_KB_ATTACH_DEFAULTS=1`）。  
- `handle_wake.py` 在校验通过后加载 sidecar（`HANDLE_WAKE_SKIP_KB=1` 可关）。

```bash
export ORG_KB_ROOT=$(pwd)/data
export HANDLE_WAKE_SKIP_FETCH=1
echo '{"type":"acn.org.work_wake","org_id":"org_demo","work_id":"work_x","assignee":"agt","idempotency_key":"k","kb_refs":[{"uri":"orgkb://org_demo/charter.md"}]}' \
  | python3 ../org-orchestrator/handle_wake.py
```
