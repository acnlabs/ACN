# Task invite → A2A push

**状态：** 已合并 — [ACN #198](https://github.com/acnlabs/ACN/pull/198) (`07e643a`)  
**后续：** 部署后 ComicLaw 端到端验收；身份正途见 [task-invite-sender.md](../features/task-invite-sender.md)

## Summary

`TaskService.invite_agent` 曾只写 `invited_agent_ids`，不推 A2A。#198 后 best-effort 推送 `task_request`（含人类 inviter → `system:task-invite`），并 emit `task.invited`。

## Acceptance（部署后）

1. Mode B worker online with `acn listen --runtime …`
2. Creator invites that worker
3. Listen receives A2A / wake within seconds (no reconcile needed)
4. Offline invitee → inbox; whitelist still written

## Related

- CLI runtime: #191 / `@acnlabs/acn-cli@0.14.0`
- ComicLaw: `docs/acn-invite-no-a2a-defect.md` → **ACN 已修 / 待验收**
