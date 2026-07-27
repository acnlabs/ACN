# Org 知识库（外部侧车）— K1 / K2 消费端

按 `org_id` 的**文件系统 / git** 知识树：章程、SOP、playbooks、skills。  
成员接活时**只读**拉取；**不是** ACN Kernel，**不是** `plugins.knowledge`（未接线），**不是** Memory。

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

本地 smoke（无 ACN）：

```bash
./scripts/smoke_org_knowledge.sh
```

## 与编排器（K2）

- 编排器信封可带可选 `kb_refs[]`（work 字段 / `ORG_KB_REFS_JSON` / `ORG_KB_ATTACH_DEFAULTS=1`）。  
- `handle_wake.py` 在校验通过后加载 sidecar（`HANDLE_WAKE_SKIP_KB=1` 可关）。

```bash
export ORG_KB_ROOT=$(pwd)/data
export HANDLE_WAKE_SKIP_FETCH=1
echo '{"type":"acn.org.work_wake","org_id":"org_demo","work_id":"work_x","assignee":"agt","idempotency_key":"k","kb_refs":[{"uri":"orgkb://org_demo/charter.md"}]}' \
  | python3 ../org-orchestrator/handle_wake.py
```
