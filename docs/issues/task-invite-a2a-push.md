# Draft GitHub issue: Task invite → A2A push

> Create with `gh issue create` when GitHub auth is available.
> Title: **Task invite should push A2A task_request to invitee**

## Summary

`TaskService.invite_agent` previously only wrote `invited_agent_ids` + activity — **no A2A / notify / inbox push**. Mode A and Mode B workers therefore never woke on invite (including `acn listen --runtime`).

This is a **send-side** gap, not a transport bug. Documented by ComicLaw: `docs/acn-invite-no-a2a-defect.md` (comiclaw-studio).

## Fix

Branch: `feat/task-invite-a2a-push`

- After invite whitelist save: best-effort `MessageService.send_message` with A2A `task_request` (`metadata.task_id` / `acn_task_id` for `--runtime` dedupe).
- New webhook `task.invited` (`WebhookEventType.TASK_INVITED`) with `invitee_id`.
- Push / webhook failure must **not** roll back the invite.

## Acceptance

1. Mode B worker online with `acn listen --runtime …`
2. Creator invites that worker
3. Listen receives A2A / wake within seconds (no reconcile needed)
4. Offline invitee → inbox (existing MessageRouter); whitelist still written

## Related

- CLI runtime receiver: #191 / `@acnlabs/acn-cli@0.14.0`
- ComicLaw: after this lands, mark defect doc **ACN 已修 / 待验收** and verify end-to-end
