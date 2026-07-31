# ACN 自动拉人（外部）— MVP-1

名单已知（`invited_agent_ids`）→ 算 `effective_cap` → 对未上岗候选人发 **摘要级** 叫醒信封 `acn.task.collab_pull` → 幂等、满员停拉。

| 文档 | 链接 |
|---|---|
| 自动拉人 MVP | [auto-collab-pull-mvp-v0.md](../../docs/auto-collab-pull-mvp-v0.md) |
| 稀疏协作契约（Accepted） | [sparse-collab-contract-v0.md](../../docs/sparse-collab-contract-v0.md) |

**不是** Org 编排器（那是 Org work）；**不是** Kernel Builtin。

### Org 路径（A2）

| 场景 | 侧车 | 信封 |
|---|---|---|
| Task Pool 邀请名单 | **本目录** `run_puller.py` | `acn.task.collab_pull` |
| Org work + assignee | [`../org-orchestrator/`](../org-orchestrator/) | `acn.org.work_wake` |

二者都是 ACN **外**侧车、共用幂等 flock 模式；成员用 `type` 分流（`handle_collab_pull` vs `handle_wake`）。

## 环境变量

| 变量 | 说明 |
|---|---|
| `ACN_BASE_URL` | ACN 根或带 `/api/v1` |
| `ACN_API_KEY` | 发送方 agent key |
| `ACN_TASK_ID` | 可选；也可用 `--task-id` |
| `PULLER_IDEM_PATH` | 幂等文件，默认 `./.auto-collab-pull-idem.json` |
| `POLL_INTERVAL_SEC` | 默认 `30` |

## 跑一轮

```bash
export ACN_BASE_URL=https://acn.acnlabs.cn
export ACN_API_KEY=acn_…
python3 run_puller.py --once --task-id task_…

# 只看信封（仍会拉 Task）
python3 run_puller.py --once --dry-run --task-id task_…
```

## 行为摘要

1. 读 Task + `invited_agent_ids` + participations  
2. `effective_cap`（契约 §1.4；默认产品回落 16，可配）  
3. 已上岗 / 满员 → 不叫醒  
4. **补叫（B1）：** 已幂等叫醒但未接单的人跳过，从邀请列表往后补足剩余席位  
5. 信封仅 `summary`（脱敏）+ `task_id` 指针（隐私 P1/P3）  
6. 幂等键 `{task}:collab_pull:1:{invitee}`（复用 org-orchestrator flock store）  

**与 invite A2A：** `POST .../invite` 仍会 best-effort 推 `task_request`；本侧车再推 `acn.task.collab_pull` 作对账补叫醒（信封更短、可重试）。二者类型不同，成员应用 `type` 区分。

## MVP-2：标签 + 语义召回 → 邀请 → MVP-1

```bash
# 默认 hybrid：标签加分 + Agent 画像语义（词法引擎；可换 HTTP embedding）
python3 run_matcher.py --task-id task_…

# 只要标签 / 只要语义
python3 run_matcher.py --task-id task_… --mode tags
python3 run_matcher.py --task-id task_… --mode semantic --no-pull

# 可选真 embedding（OpenAI 兼容）
# export ACN_EMBEDDING_URL=https://api.openai.com/v1/embeddings
# export ACN_EMBEDDING_API_KEY=…
# export ACN_EMBEDDING_MODEL=text-embedding-3-small

# 机密任务 → 拒绝公网匹配（exit 3）
```

维度优先级见文档 §3.4：硬过滤 → 标签+语义 → 表现加分 → 短名单 LLM（最后）。

表现分（`performance.py`）：读列表里的心跳/可达，以及 `metadata.performance.*`。  
**没数据就不计这项**。权重：`MATCH_PERF_WEIGHT`（默认 `0.15`）。

### 完成率（Kernel SoT）

正式数据在 ACN：`metadata.performance`（服务端根据任务历史聚合，**禁止客户端自报**）。  
任务 complete/reject 后 Kernel best-effort 刷新；也可手动：

```bash
# 自己 / 运维回填
python3 run_perf_enrich.py --self
ACN_INTERNAL_API_TOKEN=… python3 run_perf_enrich.py --agent-id UUID
# 等价：POST /api/v1/agents/{id}/performance/refresh
```

`GET /agents` 已透传 `metadata.performance`，matcher hybrid 直接读列表行。  
本地 `PERF_CACHE_PATH`（`--fixture` / `--local-cache`）仅作离线狗粮或旧集群兜底。

## 成员侧接信封

```bash
# Mode B listen
acn listen --runtime command --wake-exec 'python3 handle_collab_pull.py'

# 仅解析（狗粮）
echo '{"type":"acn.task.collab_pull",...}' | HANDLE_COLLAB_PULL_SKIP_FETCH=1 python3 handle_collab_pull.py

# 校验 Task 后自动 accept（可选）
HANDLE_COLLAB_PULL_ACCEPT=1 python3 handle_collab_pull.py < envelope.json
```

## 狗粮

```bash
# 离线（无 ACN）
./scripts/smoke_auto_collab_pull.sh

# 在线（含 B1 补叫：A 未接单 → 下一 tick 拉 B）
ACN_BASE_URL=… ACN_API_KEY=… ./scripts/smoke_auto_collab_pull.sh --live
```
