# Task invite → A2A push

**状态：** 已合并并验收 — [ACN #198](https://github.com/acnlabs/ACN/pull/198) / `v0.15.6`；ComicLaw 生产验收通过（2026-07-24）

## Follow-up

- Removed `system:task-invite` spoof — ACN invite A2A is **agent-only**.
  Platforms that need human UX register their own service agent.
  See [task-invite-sender.md](../features/task-invite-sender.md).
