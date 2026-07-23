# ACN Local Receiver MVP — 实现清单

**状态：** 已实现（CLI `0.14.0`）  
**范围：** 仅 `@acnlabs/acn-cli`（`acn listen`）  
**来源：** ComicLaw RFC `acn-local-receiver-rfc.md`；延续 ADR-0012 Mode B  
**不动：** ACN server 协议、ComicLaw 业务、OpenClaw 深度集成、Python/TS SDK（P2）  
**实现：** `clients/cli/src/commands/{listen,local-receiver,normalize-event,runtime-adapter}.ts`  
**运维示例：** [`docs/runbooks/acn-listen-heartbeat.md`](../runbooks/acn-listen-heartbeat.md)

---

## 问题与目标

Mode B 今天只有传输：

```bash
acn listen --forward http://127.0.0.1:PORT
```

CLI 不保证本机有合法 A2A 接收端，也不叫醒宿主 runtime。空端口时推送静默失败，任务仍 `open`，agent 看起来 online。

**目标：** 一条命令完成本机接收 + 规范化事件投递；业务 accept/干活仍归宿主。

```
ACN relay (已有)
  → listen WS
  → [NEW] 内置 A2A receiver（合法 JSON-RPC 应答）
  → [NEW] 规范化事件
  → [NEW] runtime adapter（http | command | log）
  → 宿主被叫醒
```

### 相对 RFC 的有意简化

RFC 曾写「本机绑定 `127.0.0.1` 高位端口」。**MVP 不启本地 HTTP 端口**：receiver 嵌在 `listen` 进程内，经 Mode B WebSocket 直接应答。少一个空端口失败面，且对 relay 语义足够。

旧路径保留：`--forward` / `--exec`（原语义不变）。

---

## Flags

| Flag | 必填 | 说明 |
|---|---|---|
| `--runtime <id>` | 与旧 handler 三选一 | `http` \| `command` \| `log` |
| `--wake-url <url>` | `runtime=http` 时必填 | `POST` 规范化事件 JSON |
| `--wake-header <k:v>` | 否，可重复 | 附加到 wake 请求 |
| `--wake-exec <cmd>` | `runtime=command` 时必填 | 事件 JSON 走 stdin |
| `--wake-timeout <ms>` | 否，默认 `5000` | 单次 wake 超时 |
| `--no-dedupe` | 否 | 关闭去重（**默认开启**） |
| `--dedupe-ttl <sec>` | 否，默认 `3600` | 去重窗口（进程内；重启丢失） |
| `--forward` / `--exec` | 兼容 | 与 `--runtime` **互斥** |
| `-i, --agent-id` | 否 | 沿用现有 |

**互斥：** 必须恰好一种：`--runtime` **或** `--forward` **或** `--exec`。

**命名警示（易混）：**

| Flag | 含义 |
|---|---|
| `--exec <cmd>`（旧） | 子进程 **stdout = 完整 A2A JSON-RPC 响应**；CLI 不代答 |
| `--runtime command --wake-exec <cmd>`（新） | CLI **自己**回 A2A accepted；子进程只负责 **wake**（stdin = 规范化事件） |

**推荐生产用法：**

```bash
acn listen --runtime http \
  --wake-url http://127.0.0.1:10122/hooks/agent \
  --wake-header 'Authorization: Bearer …'
```

---

## 接口

### 处理顺序（硬约束）

对每个 relay 来的请求：

1. 解析 JSON-RPC / 抽取事件字段  
2. **立即**回合法 A2A 应答（不阻塞在 wake 上）  
3. 若未 dedupe：在 `--wake-timeout` 内异步 wake  
4. wake 失败 → stderr 记 `wake_failed`（含 `message_id` / `task_id`）；**不**改已发出的 A2A 应答  

原因：ACN relay 有约 30s `deadline_ms`；wake 堵死会导致发送方 504，重新引入「看起来失败」的静默坑。

**语义取舍：** `A2A accepted ≠ 宿主已处理`。缓解：异步 wake + 固定日志字段 + 文档强调仍要 inbox / Task list reconcile。MVP **不**在 wake 失败时返回 A2A error。

### 内置 A2A 应答（receiver → ACN）

对 `message/send` **与** `message/stream`，MVP 一律回**单发**合法 `message`（不转发 SSE）：

```json
{
  "jsonrpc": "2.0",
  "id": "<request-id>",
  "result": {
    "kind": "message",
    "messageId": "<uuid>",
    "role": "agent",
    "parts": [{ "kind": "text", "text": "accepted" }]
  }
}
```

- 未知 method → JSON-RPC `-32601`
- 解析失败 → `-32700` / `-32600`
- **不**在 CLI 内 `tasks accept` / 跑业务

### 规范化事件（adapter 输入）

```json
{
  "event_type": "a2a_message",
  "task_id": null,
  "message_id": "…",
  "context_id": null,
  "from_agent": null,
  "received_at": "2026-07-23T12:00:00Z",
  "raw": {}
}
```

| 字段 | 规则 |
|---|---|
| `event_type` | 固定 `a2a_message`（有 `task_id` 也不改成 `task_request`，避免假称已接 Task） |
| `task_id` | 按下方抽取表；都没有则为 `null` |
| `message_id` | 必填（无则 CLI 生成） |
| `raw` | 原始 JSON-RPC body（object） |

#### `task_id` 抽取（按优先级，命中即停）

| 优先级 | 路径 |
|---|---|
| 1 | `params.message.metadata.task_id` |
| 2 | `params.message.metadata.acn_task_id` |
| 3 | `params.message` 的 data part：`data.task_id` / `data.acn_task_id` |
| 4 | 无 → `null`（去重仅用 `message_id`） |

`message_id`：`params.message.messageId` → `params.message.message_id` → CLI 生成 UUID。

#### 覆盖边界（写进验收预期）

`acn listen --runtime …` **只处理经 Mode B relay 到达的 A2A 请求**。  
不订阅 Task Pool list；从未推到本 agent、仅出现在 `acn tasks list` 的 open 任务，**仍靠** reconcile / 轮询 / 人工 handle。本 MVP 不取代拉取兜底。

### 去重

- 默认开启；`--no-dedupe` 关闭  
- key：`task_id ?? message_id`  
- 存储：**进程内** Map + TTL；**重启丢失窗口**（不要求跨重启去重）  
- 应答时先占位（挡住并发双推）；**wake 成功才保留**；wake 失败则 `forget` 该 key，便于 ACN at-least-once 重推再叫醒  
- 命中（成功 wake 之后的重复推送）：仍回 A2A accepted，**不再 wake**；stderr 打 `deduped`（含 key）

### Runtime 行为

| id | 行为 | 失败 |
|---|---|---|
| `http` | `POST wake-url`，body = 事件 JSON；尊重 `--wake-header` | 非 2xx / 超时 → `wake_failed` |
| `command` | 跑 `--wake-exec`，stdin = 事件 JSON | 非 0 / 超时 → `wake_failed` |
| `log` | 只打 stderr JSON 一行（完整事件） | 无 |

stderr 固定可检索字段（示例）：

```text
[acn listen] wake_failed message_id=… task_id=… reason=timeout|http_502|exit_1
[acn listen] deduped key=…
```

---

## 非目标

- `openclaw-hooks` 一等公民（P1：用 `--runtime http` + OpenClaw hooks URL 作为文档示例即可）
- listen 内嵌 `heartbeat`（P1；见下方生产组合）
- SDK `ACNClient.listen({ runtime })`（P2）
- 服务端改协议 / vanity subdomain
- ComicLaw Studio / 扣款 / `production-worker.sh`
- 跨进程/跨重启持久去重
- 本地绑定 HTTP 端口（相对 RFC 有意不做）

---

## 验收

仅需 CLI + 本机 wake 探针：

| # | 步骤 | 期望 |
|---|---|---|
| 1 | `acn join --relay` + `acn listen --runtime http --wake-url http://127.0.0.1:<probe>/wake` | 进程常驻，WS connected；**无**本机 A2A 端口监听 |
| 2 | 他方对该 agent `message send`（走 relay） | probe **数秒内**收到含 `message_id` 的 JSON；有 metadata 时含 `task_id` |
| 3 | 重复推同一 `task_id`（或同 `message_id`），且首次 wake 已成功 | A2A 仍成功；第二次 **不**再 wake；stderr `deduped` |
| 3b | 首次 wake 失败后再推同一 key | 释放 dedupe；第二次 **应再 wake** |
| 4 | 故意不启 probe，只 listen | A2A 应答仍成功（不再「空端口」）；stderr `wake_failed` |
| 5 | wake 探针 sleep > deadline 模拟慢宿主 | A2A **仍在 wake 完成前**返回 accepted（先应答后 wake） |
| 6 | `message/stream` 经 relay | 回单发 accepted `message`，不要求 SSE |
| 7 | `--forward http://127.0.0.1:9` 旧路径 | 行为与今日一致（兼容不回归） |
| 8 | `--exec` 旧路径与 `--runtime command` | 语义不混淆；单测分别覆盖 |
| 9 | 仅存在于 Task Pool、从未 A2A 推送的 open task | **不**期望 listen 自动 wake（拉取兜底仍必要） |
| 10 | 单测 | receiver 形状、互斥 flag、dedupe、先应答后 wake、http/command/log adapter |

### 文档交付（必须落地，非「一小段」）

| 文件 | 内容 |
|---|---|
| `clients/cli/README.md` | 生产推荐 `--runtime`；旧 `--forward`/`--exec` 降为高级/兼容 |
| `skills/acn/SKILL.md` | Mode B 小节推荐 `--runtime http\|command\|log`；注明覆盖边界 |
| `clients/cli/CHANGELOG.md` | `0.14.0` 条目 |
| 示例 unit（可放 README 或 `docs/runbooks/`） | `listen` 与 `acn heartbeat` **同生命周期**（两个 ExecStart 或 sidecar timer），避免「接得住消息但 discovery offline」 |

---

## 交付切面

| 项 | 位置 |
|---|---|
| 实现 | `clients/cli/src/commands/listen.ts`（建议拆 `runtime-adapter.ts` / `normalize-event.ts`） |
| 测试 | `clients/cli/tests/listen*.test.ts` |
| 说明 | 上表文档交付 |
| 版本 | CLI minor（`0.14.0`），**无需** server 发版 |

---

## 后续（非本 MVP）

| 优先级 | 项 |
|---|---|
| P1 | `openclaw-hooks` 预设；正式 systemd 示例（listen + heartbeat） |
| P2 | SDK 对等 API；metrics / 日志字段对齐；可选持久去重 |
