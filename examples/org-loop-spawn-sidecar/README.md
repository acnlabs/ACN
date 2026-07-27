# Org Loop spawn sidecar（POC）

外部执行侧车：poll Org open work →（C2）跑可配置命令 → 治理 key 关单。

设计：[org-loop-spawn-sidecar-poc-v0.md](../../docs/org-harness/org-loop-spawn-sidecar-poc-v0.md)

## C1 — poll + 日志

不 spawn、不改状态；只证明能看见待办。

```bash
export ACN_BASE_URL=https://acn.acnlabs.cn   # 或你的 ACN
export ACN_ORG_ID=org_…
export ACN_API_KEY=acn_…                     # 有权读该 Org work 的 key
python3 poll_open_work.py
# 或单次：
python3 poll_open_work.py --once
```

环境变量：

| 变量 | 说明 |
|---|---|
| `ACN_BASE_URL` | 含或不含 `/api/v1` 均可 |
| `ACN_ORG_ID` | Org id |
| `ACN_API_KEY` | Bearer |
| `POLL_INTERVAL_SEC` | 默认 `30`（`--once` 时忽略） |

## C2（未做）

`spawnCommand` + worker 成功后 `PATCH …/work/{id}` → `done`（governance key）。
