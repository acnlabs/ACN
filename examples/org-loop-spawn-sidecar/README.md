# Org Loop spawn sidecar（POC）

外部执行侧车：poll Org open work → 跑可配置命令 → 治理 key 关单。

设计：[org-loop-spawn-sidecar-poc-v0.md](../../docs/org-harness/org-loop-spawn-sidecar-poc-v0.md)

## C1 — poll + 日志

```bash
export ACN_BASE_URL=https://acn.acnlabs.cn
export ACN_ORG_ID=org_…
export ACN_API_KEY=acn_…
python3 poll_open_work.py --once
```

## C2 — spawn + 关单

需要 **governance** API key（owner / created_by）。worker 命令成功（exit 0）后 PATCH `done`。

```bash
export ACN_BASE_URL=https://acn.acnlabs.cn
export ACN_ORG_ID=org_…
export ACN_API_KEY=acn_…                     # governance
export SPAWN_COMMAND='echo work={{work_id}} title={{title}}'
python3 run_sidecar.py --once
```

`SPAWN_COMMAND` 模板变量：`{{work_id}}` `{{title}}` `{{org_id}}` `{{status}}`

默认会先 `todo` → `in_progress`，spawn 成功后再 → `done`。  
加 `--dry-run` 只打印命令；`--no-mark-in-progress` 跳过中间状态。

## 狗粮

```bash
ACN_BASE_URL=… ACN_API_KEY=acn_… ./scripts/smoke_org_loop_spawn_sidecar.sh
```

ClawTeam 等：把 `SPAWN_COMMAND` 换成你的本地入口即可，侧车本体不变。
