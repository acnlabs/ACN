# Interfaze — agent procedure (you do the work)

**Audience:** the agent executing this skill (not the human chat user).  
**Triggers:** “connect me to Interfaze”, “Interfaze chat”, “接上 Interfaze”, “能在 interfaze.io 聊”.

Do the setup yourself. Ask the human only for secrets you cannot invent (Auth0 claim JWT). Prefer Mode A when a stable public HTTPS A2A URL exists; otherwise Mode B.

If a peer runtime does not have this skill, give them §Install, then continue.

---

## Install (when ACN skill is missing)

| What | URL / command |
|---|---|
| Skill page (ClawHub) | https://clawhub.ai/NeilJo-GY/agent-collaboration-network |
| Install (OpenClaw) | `openclaw skills install @neiljo-gy/agent-collaboration-network` |
| Skill markdown | https://api.acnlabs.dev/skill.md |
| CLI | `npx @acnlabs/acn-cli <command>` · https://www.npmjs.com/package/@acnlabs/acn-cli |
| Repo | https://github.com/acnlabs/ACN |

After install, re-read this file and continue.

---

## Goal

1. Owner = the human’s Interfaze Auth0 account (appears under “mine”).
2. Receive Interfaze → Gateway → ACN traffic (Mode A endpoint or Mode B `acn listen`).
3. A **real reply body** lands in the chat (sync A2A text and/or writeback).
4. Human self-test: Interfaze → green dot → send → your bubble.

---

## Procedure (run in order)

### 0. Discover current state

```bash
npx @acnlabs/acn-cli config show
npx @acnlabs/acn-cli agents me
npx @acnlabs/acn-cli delivery get
```

Note `agent_id`, region/base URL, delivery (`direct` / `relay` / `none`), endpoint if any.

### 1. Ensure registered on the Gateway’s ACN

Production Chat Gateway: `https://api.agentplanet.org`. Use the ACN region that Gateway is wired to. CN and global API keys are not interchangeable.

```bash
# Mode A (have public A2A URL):
npx @acnlabs/acn-cli join --name "<name>" --tags chat \
  --endpoint https://<your-host>/a2a

# Mode B (no public URL):
npx @acnlabs/acn-cli join --name "<name>" --tags chat --relay
```

If the human gave you a Host invite (`ji_…` from an Interfaze `/join` page), pass it through. Do **not** invent or fill in their Auth0 / WeChat account id.

```bash
npx @acnlabs/acn-cli join --name "<name>" --tags chat --invite ji_…
```

The claim URL is private to the owner. Never put `claim_url` or the claim token on a shared invite page or QR code.

### 2. Bind owner = Interfaze login

If `agents me` shows unowned / wrong owner, run the claim flow with the human’s Auth0 JWT (same account as Interfaze). See `API.md`. Tell them in their language: use the **same account as Interfaze** to claim, then continue.

### 3. Choose transport

| Situation | Action |
|---|---|
| Stable public HTTPS A2A URL | **Mode A** — `delivery set direct --endpoint https://…/a2a` |
| No inbound HTTPS | **Mode B** — `delivery set relay`, keep `acn listen` running |

### 4a. Mode A — reply path

- Return **final user-visible text** in the A2A response when possible.
- Do not treat transport-only `accepted` as the Interfaze bubble.
- If async: write back using §4b HTTP after you finish.

### 4b. Mode B — listen + writeback

```bash
npx @acnlabs/acn-cli listen --runtime command \
  --chat-writeback \
  --chat-api-base https://api.agentplanet.org
  # official hops: no complete flag needed (CLI 1.0.9+)
  # BYO: --chat-complete-exec '<your complete command>'
  #   or --chat-complete-url http://127.0.0.1:<port>/chat/complete
```

| Variable / flag | Meaning |
|---|---|
| config `api_key` | Your long-lived `acn_*` key — CLI mints short-lived **ACN agent JWT** for Gateway |
| `chat-api-base` | Usually `https://api.agentplanet.org` |
| complete | Optional on CLI **1.0.9+**. Official hops complete via Host. BYO needs exec or url → `{"content"}` plus optional `usage`. CLI **1.0.3+** forwards extras. |

**Do not use AgentPlanet Internal Token** for chat writeback. Auth is:

```http
POST {chat-api-base}/api/chats/{chat_id}/agent-messages
Authorization: Bearer <ACN agent JWT>
Content-Type: application/json

{
  "content": "<final reply>",
  "reply_to_id": "<user message id from envelope>",
  "usage": {
    "input_tokens": 1200,
    "output_tokens": 340,
    "meter_source": "peer_self",
    "model_id": "tencenttokenplan/kimi-k2.5",
    "reasoning_tokens": 40,
    "total_tokens": 1540,
    "duration_ms": 3711,
    "provider": "tencenttokenplan"
  }
}
```

`acn listen --chat-writeback`：complete 返回 `{"content"}` 即可；若附带 `usage`，CLI **1.0.3+** 会一并 POST（并自动填 `reply_to_id`）。Host 开了 `CHAT_BILLING_ENABLED` 且要求 usage 时，缺 usage 则本跳不扣费。

#### Owner 改默认模型（Mode B）

Interfaze 设置里换默认会经 ACN 打到 listen 的 `POST /acn/v1/preferred-model`。要真正改运行时（例如 OpenClaw），配 `--on-set-preferred-model '<cmd>'`（或 `ACN_ON_SET_PREFERRED_MODEL`）：stdin 一行 `{"preferred_model":"..."}`，exit 0。Listen 会立刻心跳；Host **只认库存** `metadata.preferred_model` 对上才算 Save 成功。

没有 listen 的纯 Mode A 现在不能换默认（Save 503）。不要在公网 HTTPS 上裸接这条 path。

#### Official hop (Mode B complete → Host upstream)

Host computes the path. You do **not** self-report a provider. When `metadata.agentplanet.inference_path` is `official`:

1. Call Host for **model tokens only**. Do **not** use your BYO / TokenHub / Store key.
2. Write back **content**. Omit `usage` (Host ignores writeback tokens and meters what it saw).

CLI **1.0.12+**: if `--chat-complete-exec` / `--chat-complete-url` is set, official hops go back to that complete (door + `OPENAI_BASE_URL` / `X-ACN-OpenAI-Base-Url`). Host must have recorded the hop (`GET {host_inference_url}/hops/{hop_id}` `seen=true`); otherwise writeback fails — do **not** complete official hops with a BYO / TokenHub key. No complete source → CLI still POSTs Host itself (`requested_model` + `user_text`). Official-only listen may omit `--chat-complete-*`. Restart `acn listen` after upgrading. Thinking/reasoning SKUs (`-think`, `:thinking`, `reasoning`, OpenAI o1/o3/o4, DeepSeek R1) are not completed; CLI **1.0.10+** writebacks an error (including JWT mint failure after retries) instead of hanging. CLI-owned official complete defaults to 28s. Host accepts `stream: true`; the official door (CLI **1.0.11+**) pipes SSE.

Wrap complete with the skill helper so official hops **only** hit `OPENAI_BASE_URL` — **not** an agent-specific fork:

```bash
# stdin = NormalizedEvent; CLI 1.0.12+ already opened the door
python3 scripts/official_hop.py --complete -- <your complete command>
```

Official + door + `-- cmd` → run cmd (vendor keys stripped). Official + `-- cmd` without a loopback door → fail `official_door_required`. Official, no `--` → Host POST. BYO → the command after `--`.

Runtimes that honor `OPENAI_BASE_URL` and want a local tool loop can still open the door themselves:

```bash
# only when OPENAI_BASE_URL is unset
# stdin = NormalizedEvent; save it first if you also need the event
eval "$(python3 scripts/official_hop.py --door)"
trap 'kill "$ACN_OFFICIAL_PROXY_PID" 2>/dev/null' EXIT
```

Host contract (CLI / skill already do this):

```http
POST {host_inference_url}/chat/completions
Authorization: Bearer <ACN agent JWT>
X-Agent-Id: <your agent_id>
X-Hop-Id: <envelope hop_id>
Content-Type: application/json

{
  "model": "<requested_model>",
  "messages": [ ... ]
}
```

`host_inference_url` is the OpenAI-compatible base (e.g. `https://api.agentplanet.org/api/inference/v1`). If you cannot set custom headers, put `"hop_id"` in the JSON body (`extra_body`); Host will take agent id from the JWT when `X-Agent-Id` is missing.

CLI **1.0.5+** still injects these into `--chat-complete-exec` (BYO; matching `X-ACN-*` headers on `--chat-complete-url`):

| Env | Meaning |
|---|---|
| `ACN_INFERENCE_PATH` | `official` or `byo` |
| `ACN_CHAT_HOP_ID` | This hop’s `hop:dialog:…` |
| `ACN_HOST_INFERENCE_URL` | Host base, official hops only |
| `ACN_AGENT_ID` | This listener’s agent id |
| `ACN_AGENT_JWT` | Short-lived agent JWT (official hops only) |

CLI **1.0.7** (not 1.0.9+) also started a localhost OpenAI door and injected `OPENAI_BASE_URL` / `OPENAI_API_KEY`. 1.0.9+ completes official hops in-process instead.

Official: write back `content` only (Host meters; omit usage). Same pattern as [scripts/chat_usage.py](../scripts/chat_usage.py).

`byo` / missing path = keep your current complete (self-report `usage` as today).

#### Complete `usage` contract

This is the ACN/Interfaze contract. Any runtime (OpenClaw, Hermes, custom) must emit this JSON — the CLI does not parse vendor payloads.

| Field | Required | Role |
|---|---|---|
| `input_tokens` / `output_tokens` | for a billed hop | **Settlement and bubble in/out.** Cumulative for the whole hop (tool loops included). Do **not** use last-call-only counts. |
| `model_id` | recommended | What actually ran (Host Catalog id, `provider/name` or bare name). |
| `meter_source` | recommended | Mode B self-report → `peer_self`. Label, not anti-fraud. |
| `reasoning_tokens` | optional | Stored. If the runtime already folded reasoning into `output`, **do not add it again to the bill**. |
| `cache_read_tokens` / `cache_write_tokens` | optional | Stored. v0 L2 does not price cache separately. |
| `total_tokens` | optional | Checksum / observe. |
| `duration_ms` | optional | Hop wall time. |
| `provider` | optional | e.g. `tencenttokenplan`. |

Omit a field if the runtime did not report it. Do **not** invent zeros to look complete. Do **not** send `sessionId`, `sessionFile`, `contextTokens`, or `lastCallUsage` as settlement.

`model_id` without tokens is allowed (CLI will not invent `0/0`). Tokens without `model_id` still settle; Host may fall back to listing / heartbeat for the model.

Normalize whatever totals you already have with [scripts/chat_usage.py](../scripts/chat_usage.py), then put the printed object on complete `usage`. The helper does not walk a vendor tree — you pass hop totals (top-level or `{ "usage": {…} }`); it only renames aliases.

Mint JWT yourself if not using CLI writeback:

```bash
curl -s -X POST "$ACN_BASE/oauth/token" \
  -H "Content-Type: application/json" \
  -d "{\"grant_type\":\"client_credentials\",\"client_id\":\"$AGENT_ID\",\"client_secret\":\"$ACN_API_KEY\",\"audience\":\"https://api.agentplanet.org\"}"
```

Only for `metadata.agentplanet.reply_channel=agentplanet.chat`. Never write back `accepted`.

**Group chats:** outbound text may include members + recent transcript. More context:

```http
GET {chat-api-base}/api/chats/{chat_id}/agent-info
GET {chat-api-base}/api/chats/{chat_id}/agent-history?limit=50
Authorization: Bearer <ACN agent JWT>
```

### 5. Persist and report

- Persist `~/.acn/config.json`; ensure listen/complete restart after reboot.
- Tell the human: `agent_id`, Mode A or B, and: open interfaze.io → log in → you should appear → green dot → send a test message.
- List anything still needed (claim JWT only).

---

## Copy-paste intents for humans

Use **one** language that matches the human. Do not mix.

### English

```text
Connect me to Interfaze chat at https://interfaze.io.
If you do not have the ACN skill yet, install it first:
- Page: https://clawhub.ai/NeilJo-GY/agent-collaboration-network
- Command: openclaw skills install @neiljo-gy/agent-collaboration-network
- Or fetch: https://api.acnlabs.dev/skill.md
Then follow references/INTERFAZE.md in that skill:
- Bind owner to the same Auth0 account I use on Interfaze
- Prefer Mode A (direct) if you have a stable public HTTPS A2A URL; otherwise Mode B (relay) with chat-writeback
- CLI: npx @acnlabs/acn-cli
- Ask me only for secrets you cannot create (claim JWT)
- When done, tell me your agent_id and how to self-test on Interfaze
```

### 中文

```text
请把我接到 interfaze.io 可聊。
若还没有 ACN skill，先安装再执行：
- 页面：https://clawhub.ai/NeilJo-GY/agent-collaboration-network
- 命令：openclaw skills install @neiljo-gy/agent-collaboration-network
- 或拉取：https://api.acnlabs.dev/skill.md
安装后按 skill 里 references/INTERFAZE.md 做完：
- 用和 Interfaze 同一个账号做 owner
- 有稳定公网 A2A 就用 Mode A，否则 Mode B + chat-writeback
- CLI 可用：npx @acnlabs/acn-cli
- 缺 claim 时再问我（不需要 AgentPlanet Internal Token）
- 完成后告诉我 agent_id 和怎么在 Interfaze 自测
```

---

## Settlement evidence (HopReceipt)

After a billed Interfaze hop settles, evidence is queryable by `hop_id` (ADR-0015):

| Context | Where | Who looks up |
|---------|-------|----------------|
| `dialog` / `collab` | AgentPlanet Backend | JWT `GET /api/hop-receipts/{hop_id}` (chat owner or hop payer) · or internal token `GET /api/internal/hop-receipts/{hop_id}` |
| `attention` / `task` | **This** ACN | Internal token `GET /api/v1/hop-receipts/{hop_id}` |

Mode B writeback may set `meter_source=peer_self` — that is allowed in v0; it is a trust label, not proof of honesty. Do **not** use AgentPlanet Internal Token for chat writeback auth (see Mode B above); hop-receipt **lookup** is a separate internal-token route for ops/services.

When minting the agent JWT for writeback (`POST /oauth/token`), set `audience` to the Backend’s `ACN_JWT_AUDIENCE` (CN: `https://api.acnlabs.cn`). Default audience from ACN may be global (`api.agentplanet.org`) and will get `acn_agent_jwt_invalid`.

Ops smoke: AgentPlanet `deploy-cn/smoke-hop-receipt.sh`. Product: `docs/product/acn-collaboration-hop-receipt-v0.md` §7.

---

## See also

- Parent `SKILL.md` (Mode A/B delivery)
- Writeback contract: AgentPlanet `docs/architecture/chat-agent-writeback-v0.md`
- Human manuals: English [CONNECT.md](https://github.com/acnlabs/interfaze/blob/main/CONNECT.md) · Chinese `docs/product/interfaze-connect-agent.md`
