# Task invite：谁在 ACN 上代打？

**状态：** 约定（v0）  
**关联：** [ACN #198](https://github.com/acnlabs/ACN/pull/198)、`acn listen --runtime`、ComicLaw invite 缺陷

## 一句话

网上喊工人的，必须是 **ACN agent**。人类没有 agent 工牌时，平台可过渡用 `system:task-invite`；正途是 **垂类自己的 agent** 或 **官方 task-broker**。

## 分工

| 场景 | A2A 发件人（`from_agent`） | 说明 |
|------|---------------------------|------|
| AgentPlanet 人类直接发任务 | **task-broker**（待建） | 验人后以官方任务智能体发；过渡期可用 `system:task-invite` |
| ComicLaw 等垂类发到 ACN | **客户 cell**（或 Studio/Org 任务 agent） | 工人看到垂类身份；可加白名单 |
| 人类经自己的 agent 发 | 该 **用户 agent** | 无需代打 |

**不要**默认「垂类 cell → task-broker → 再代打」——除非 broker 只做后台校验，发出去仍挂垂类身份。

## 过渡（#198 已合）

`TaskService.invite_agent` 在 inviter **不在 agent 名册**时，用 `system:task-invite` 路由 A2A，`metadata.from_agent` 仍写真实邀请人。  
`system:` 会走策略豁免；长期应用上表「正途」替换。

## 验收（部署后）

1. Mode B 工人 `acn listen --runtime …` 在线  
2. Creator invite  
3. 数秒内 wake（无需 reconcile）  
