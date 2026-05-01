"""add agent_allowlist table (Phase 2 PR #2)

Phase 2 PR #2 of the ACN communication economic model proposal
(see docs/features/acn-communication-economic-model.md). Adds the
relational allowlist table that backs the
``communication_policy.mode=allowlist`` decision branch added in the
same PR.

Why a separate table rather than extending the ``agents.communication_policy``
JSONB:

* The policy dict's ``allowed_keys = {"mode", "reject_reason"}`` is
  intentionally narrow and strict-keys (see policy_service.py); putting
  a member list inside it would force every policy read to scan the
  list and would balloon the JSONB for high-trust agents.
* Reverse lookups (``INDEX(target_id)``) are required for ops /
  anti-abuse but never exposed via API (the recipient's allowlist is
  private — see Phase 2 design doc, "不提供 GET /allowlist/incoming").
* ``ON DELETE CASCADE`` on both columns means agent unregistration
  cleans up dangling rows automatically. The Redis SET cache (TTL 30s)
  is eventual-consistency; cascade keeps PG canonical.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-04-30 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_allowlist",
        sa.Column(
            "owner_id",
            sa.String(),
            sa.ForeignKey("agents.agent_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_id",
            sa.String(),
            sa.ForeignKey("agents.agent_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("owner_id", "target_id"),
    )
    op.create_index(
        "ix_agent_allowlist_target_id",
        "agent_allowlist",
        ["target_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_allowlist_target_id", table_name="agent_allowlist")
    op.drop_table("agent_allowlist")
