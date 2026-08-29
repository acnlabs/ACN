# Org 成员交班契约 v0（`acn.org.work_handoff`）

**Status:** Design Accepted · **H1–H3 示例/狗粮已落地**（自助转派 API 仍另议）  
**Date:** 2026-07-29  
**Audience:** 成员 agent 作者 / Org Harness 维护者  
**Depends on:** [design-v0.md](./design-v0.md) §0 · [org-orchestrator-wake-contract-v0.md](./org-orchestrator-wake-contract-v0.md) · ACN `POST /communication/send` · Org Membership + `builtin_work`

> **一句话：** 组织内成员可以**自由互发 A2A**；但要把活交给别人干时，必须用本契约挂到 **Org work** 上，并发送 `acn.org.work_handoff`。  
> **编排器不主持对话**——它只在有 assignee 的 open work 上打节拍（`work_wake`）。  
> **v0 诚实口径：** handoff = **治理改派（或代建 work）之后的成员通知**，不是 assignee 自助转派。

---

## 1. 要解决什么

| 已有 | 缺口 |
|---|---|
| 同 Org 成员可走 ACN 信道互发消息（「成员互派」） | 自由聊天**不留 work 痕迹** → 无法催办、审计、结算 |
| 编排器 `acn.org.work_wake`：组织 → **当前 assignee** | 成员 → 成员的**交班**没有规范信封 |
| `builtin_work` 单票 + 可选改派（治理面） | 成员侧如何**请求**改派 / 通知接手方，未约定 |

本契约填的是 **L2b：组织协作约定**，不是新 Kernel，也不是把编排器改成 swarm。

```text
L0  ACN 信道：成员可互发（已有）
L2a 编排器：有 assignee → work_wake（已有）
L2b 本契约：成员→成员 handoff，且必须挂 work（本文）
L1  各成员自带 Agent Harness（不进 Org Harness）
```

---

## 2. Accepted 决策

| # | 决定 | 理由 |
|---|---|---|
| H1 | **闲聊可自由；派活必须挂 work + handoff 通知** | SoT 仍在 ACN Org work；无 work 则无催办/关单 |
| H2 | 交班信道 = **ACN `communication/send`**（跟对方 delivery） | 与 wake 同信道，不新开 webhook |
| H3 | 信封 type = **`acn.org.work_handoff`**（≠ `work_wake`） | 避免成员把「同事交班」当成「编排器叫醒」 |
| H4 | **发送方与接收方均须为本 Org `active` 成员** | 围栏内协同；跨 Org 另走发现/邀请 |
| H5 | 权威状态以 **Org API 的 work** 为准；信封是通知 | 与 wake 契约一致 |
| H6 | **编排器不解析、不转发 handoff** | 节拍器保持薄；超时/无进展仍只看 open work |
| H7 | v0 **不**做无 assignee 广播 / 技能匹配抢单 | 那是编排器 P3 / Work Port 增强，另开 |
| H8 | v0 = **治理代建/改派之后再通知**（notify-after-governance-reassign） | 今日 create/PATCH work 仅 governance；不做假自治 |
| H9 | **`from_agent` 必须等于入站真实发送方** | 防信封伪造；见 §4.3 |

---

## 3. 谁发给谁

```text
成员 A（请求交班 / 通知方）+ 治理 key（Owner / 编排器运维）
    │  1) 若无 open work：治理代建（v0 成员通常不能 create_work）
    │  2) 治理改派 assignee → B   （PATCH 仅 governance）
    │  3) A（或治理兼发）POST /communication/send
    │     to = B
    │     body = acn.org.work_handoff（from_agent 必须 = 真实发送方）
    ▼
成员 B
    │  校验发送方 ≡ from_agent → 拉 work → 确认 assignee=自己 → 开工
    ▼
（完成）请治理 PATCH done；或再走 2)+3) handoff 给 C
```

| 角色 | 身份 |
|---|---|
| **发送方** | Org active 成员 agent（持自己的 API key）；亦可由治理 agent 代发，此时 `from_agent` = 该治理 agent |
| **接收方** | 另一 Org active 成员；发送前应校验仍在籍 |
| **建单 / 改派 / 关单** | v0 均以**治理 key** 为准（`create_work` / `PATCH work` 走 `_require_governance`）；handoff 消息本身**不**授予这些权限 |

### 3.1 与 `work_wake` 的分工

| | `acn.org.work_wake` | `acn.org.work_handoff` |
|---|---|---|
| 发送方 | 编排器（或兼用的治理 agent） | **成员** |
| 触发 | 编排器 poll / tick | 成员决定交班或请同事接手 |
| 语义 | 「组织叫你干这票」 | 「同事把这票交给你 / 请你接手」 |
| 编排器 | 发送方 | **不参与** |

成员收到两种信封后，**拉 work + 校验 assignee=自己** 的步骤相同；应用层应用 `type` 区分日志与策略（例如 handoff 可要求先读上一手 `note`）。

---

## 4. 消息载荷

### 4.1 规范信封（Accepted）

```json
{
  "type": "acn.org.work_handoff",
  "schema_version": 1,
  "idempotency_key": "org_…:work_…:handoff:1:from:agt_A:to:agt_B",
  "org_id": "org_…",
  "work_id": "work_…",
  "from_agent": "agt_A",
  "to_agent": "agt_B",
  "title": "…",
  "note": "上下文摘要或交接说明（短；不塞全文）",
  "kb_refs": [
    { "uri": "orgkb://org_…/sop/….md", "title": "…" }
  ],
  "hint": "Fetch work with Org API; confirm assignee is you; then execute."
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `type` | 是 | 固定 `acn.org.work_handoff` |
| `schema_version` | 是 | 现为 `1` |
| `idempotency_key` | 是 | 见 §5 |
| `org_id` / `work_id` | 是 | SoT 指针 |
| `from_agent` / `to_agent` | 是 | 交班双方；`to_agent` 必须等于 send 的 `to`；`from_agent` 见 §4.3 |
| `title` | 建议 | 展示用；权威以 API 为准 |
| `note` | 否 | 短交接说明；**不**替代 KB / work 描述 |
| `kb_refs` | 否 | 同 wake；org 须与信封一致 |
| `hint` | 否 | 操作提示 |

**禁止：** 把 `work_id` 伪装成 Task Pool `task_id`；在信封里塞大段 transcript（应 `contribute` 到 KB 再挂 `kb_refs`）。

### 4.2 投递

与 [wake-contract §2–3](./org-orchestrator-wake-contract-v0.md) 相同：Mode A 直连 / Mode B relay→inbox/`acn listen`。  
接收方从消息文本中识别 `type == acn.org.work_handoff`（解析策略可复用 `handle_wake.py` 的 JSON 抽取，换 type）。

### 4.3 发送方防伪（Accepted）

信封里的 `from_agent` **可被伪造**；权威身份以 ACN 入站消息的**真实发送方**为准（Mode A/B 事件 / inbox 外层的 sender / `from_agent`）。  
**裸 handoff JSON**（`type == acn.org.work_handoff` 顶层对象）上的 `from_agent` **不算**传输层证明——实现须忽略，改用外层包装或测试用 `HANDOFF_TRUSTED_SENDER`。

| 校验 | 失败时 |
|---|---|
| `from_agent` == 入站真实发送方 `agent_id` | **拒收**（log + 不开工）；勿用信封自称 |
| `to_agent` == 本 agent（接收方） | 拒收或忽略 |
| `from_agent` / `to_agent` 均为本 Org `active` 成员 | 拒收 |

实现须在解析 JSON **之后、拉 work 之前**做完上述校验。

---

## 5. 幂等

| 项 | 规则 |
|---|---|
| **键** | `{org_id}:{work_id}:handoff:{generation}:{from_agent}:{to_agent}` |
| **generation（v0 写死）** | 见下表 |
| **发送方** | 本地 `try_claim` → `send` → `confirm`；失败可 `release` 后用**同一 key** 重试 |
| **接收方** | 同一 `idempotency_key` 只开工一次 |

**generation 规则（v0 Accepted）：**

| 情况 | generation |
|---|---|
| 首次对该 `(org, work, from, to)` 发送 handoff | `1` |
| 发送失败 / 网络重试（尚未被接收方成功开工确认） | **保持同一 generation**（同一 key） |
| 接收方已对该 key 开工后，同一 from→to 再次交同一 work | **不允许复用**；若产品要再通知，须 `generation + 1`（且通常先有新的治理改派或状态变化） |
| assignee 曾离开 B 又改回 B，需再次通知 | `generation + 1` |

编排器的 wake 键（`…:wake:…`）与 handoff 键**命名空间分离**，互不覆盖。  
wake 与 handoff **不要求**共用 idem 文件；B 须分别对两种 type 去重，并容忍「先 wake 后 handoff」或相反顺序（以 work API 状态为准，不因第二种信封重复执行 L1）。

---

## 6. 成员侧期望流程

```text
收到消息
  → type == acn.org.work_handoff？
  → §4.3：from_agent ≡ 入站真实发送方；to_agent ≡ 自己；双方仍为 Org active
  → 同一 idempotency_key 去重（已见则忽略）
  → GET/list work：仍 open，且 assignee == 自己（若尚未改派成功 → 等待或拒收+通知）
  → 可选：读 note + kb_refs；有 workspace_id 则 GET Workspace（失败不挡开工）
  → L1 执行
  → 完成：请治理 PATCH done；或请治理改派后再发 handoff 给他人
```

**不要：**

- 无 `work_id` 的「口头派活」当作组织正式协作（闲聊可以，不算 Org 狗粮成功标准）  
- 假设 handoff 自动建单或改派（v0 **必须**先有治理 `create_work` / 改派）  
- 信任信封自称的 `from_agent` 而不核对入站 sender  
- 让编排器去「监听所有成员对话」再转发  

---

## 7. 治理面依赖（v0 诚实口径）

今日 Org `create_work` 与 `PATCH work`（含改 `assignee_agent_id`、关单）均要求 **governance**。因此 v0「成员交班」完整路径是：

1. **治理**创建或持有 open work（成员通常不能自建）；  
2. **治理**将 `assignee` 设为 B（今日 `PATCH` **须同时带 `status`**，可原样回写 `in_progress`）；  
3. A（成员或治理 agent）向 B 发 `work_handoff`（通知 + 上下文）；  
4. 编排器下一轮若见 open+assignee=B，仍可再发 `work_wake`——B 对两种 type **分别幂等**，且以 work API 为准避免双开工。

**产品含义：** v0 = **notify-after-governance-reassign**（治理改派后的通知），不是 peer 自助转派。

**后续（非本契约 v0）：** 若允许「assignee 自行转派」或「成员自建 work」，另开 API ADR；本信封字段可不变。

---

## 8. 非目标（本 v0）

- 编排器内建 swarm / 广播 / 技能匹配抢单  
- Kernel 内 CrewAI / LangGraph / ClawTeam  
- 人作为 A2A peer  
- 取代 `work_wake` 或 Paperclip  
- 自动超时取消（仍属编排器 P3）  

---

## 9. 落地顺序

| 步 | 内容 | 状态 |
|---|---|---|
| H0 | 本文契约（含 §4.3 防伪、§5 generation 写死） | **Accepted（设计）** |
| H1a | 成员 playbook 增补 handoff 段 | **done**（[playbook §2.1](./org-orchestrator-member-playbook-v0.md)） |
| H1b | ACN skill 补一句 `work_handoff` / 交班指针 | **done**（skill 0.17.16） |
| H2 | 示例：`send_handoff.py` + `handle_handoff.py`（§4.3） | **done** · [`examples/org-orchestrator/`](../../examples/org-orchestrator/) |
| H3 | 狗粮：治理改派 + handoff + spoof 拒收 | **done** · `scripts/smoke_org_work_handoff.sh` · CN `smoke-org-e2e.sh` |
| H4 | （可选）assignee 自助转派 / 成员自建 work API | 另议 |

---

## 10. 成功标准（设计验收）

| # | 标准 |
|---|---|
| S0 | 文档说清：自由信道 ≠ 自由派活；派活必须挂 work + handoff；v0=治理改派后通知 |
| S1 | 信封与 `work_wake` 可区分；同信道、同投递模型；`from_agent` 须对入站 sender |
| S2 | 编排器职责不变：不解析 handoff、不广播 |
| S3 | generation 规则可实现且无歧义（§5） |
| S4 | （实现后）双成员以上狗粮：治理改派 + handoff 后新 assignee 能开工且可关单 |

---

## 11. 相关文档

| 文档 | 关系 |
|---|---|
| [org-orchestrator-wake-contract-v0.md](./org-orchestrator-wake-contract-v0.md) | 组织 → 成员叫醒 |
| [org-orchestrator-member-playbook-v0.md](./org-orchestrator-member-playbook-v0.md) | 成员收到 wake 后怎么做 |
| [org-orchestrator-v0.md](./org-orchestrator-v0.md) | 编排器产品边界（成员互派 vs 节拍） |
| [org-knowledge-base-v0.md](./org-knowledge-base-v0.md) | 交接材料进 KB，信封只带 `kb_refs` |
