# Org 待办执行器（外部）— POC 示例代码

跑在 ACN **外面**：poll Org 待办 → 跑可配置命令 → 治理 key 关单。

设计：[org-loop-spawn-sidecar-poc-v0.md](../../docs/org-harness/org-loop-spawn-sidecar-poc-v0.md)

> 目录名 `org-loop-spawn-sidecar` 是历史 slug；产品名：**Org 待办执行器（外部）**。

## C1 — 只看待办

```bash
export ACN_BASE_URL=https://acn.acnlabs.cn
export ACN_ORG_ID=org_…
export ACN_API_KEY=acn_…
python3 poll_open_work.py --once
```

## C2 — 跑命令 + 关单

需要 **governance** API key。命令成功（exit 0）后标 `done`。

```bash
export SPAWN_COMMAND='echo work={{work_id}} title={{title}}'
python3 run_sidecar.py --once
```

模板变量：`{{work_id}}` `{{title}}` `{{org_id}}` `{{status}}`

## 狗粮

```bash
ACN_BASE_URL=… ACN_API_KEY=acn_… ./scripts/smoke_org_loop_spawn_sidecar.sh
```

ClawTeam 等：只改 `SPAWN_COMMAND`，执行器本体不变。
