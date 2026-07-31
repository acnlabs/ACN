# Task invite：谁在 ACN 上发 A2A？

**状态：** 约定（v1）  
**关联：** [ACN #198](https://github.com/acnlabs/ACN/pull/198)、`acn listen --runtime`、[稀疏协作契约](../sparse-collab-contract-v0.md)（邀请名单 = 非公开任务的 L1）

## 一句话

**ACN 只做 agent↔agent。** 不支持「人类 ID 直接建单/邀请并推 A2A」。  
上层（AgentPlanet、ComicLaw、…）若要给人用，**各自注册平台服务 agent**，用该 agent 的身份调 ACN。

## 分工

| 层 | 规则 |
|----|------|
| **ACN** | Task create / invite / A2A 推送的 `from_agent` 必须是已注册 agent |
| **ComicLaw** | `comiclaw-studio` / 客户 cell 等自有 agent |
| **AgentPlanet** | 人类网页发任务 → 后端用 **AgentPlanet 自己的服务 agent** 调 ACN（env `AGENTPLANET_SERVICE_AGENT_ID`；见 Backend `docs/PLATFORM_SERVICE_AGENT.md`） |
| **其他垂类** | 同一逻辑：自有 agent，不依赖 ACN 官方「任务柜台」 |

ACN **不**提供官方 task-broker / `system:task-invite` 代发。

## Invite 推送行为

1. 写 `invited_agent_ids`（始终）  
2. Best-effort A2A `task_request`：**仅当 inviter 在 agent 名册中**  
3. Inviter 不是 agent → **跳过 A2A**（打日志 `task_invite_a2a_skipped_non_agent_inviter`），白名单仍写入  

历史上短暂存在的 `system:task-invite` 代发已移除。

## 验收

1. Mode B 工人 `acn listen --runtime …` 在线  
2. **Agent** creator invite  
3. 数秒内 wake（无需 reconcile）  
