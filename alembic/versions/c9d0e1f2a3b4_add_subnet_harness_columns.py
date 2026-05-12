"""add subnet harness_url and harness_secret columns

Adds pluggable Org Harness support to subnets. When a subnet owner
registers a ``harness_url`` (via ``PATCH /api/v1/subnets/{subnet_id}/harness``),
ACN will deliver lifecycle webhooks (``agent.joined_subnet``,
``task.created``, ``task.completed`` etc.) to that URL, signed with
``harness_secret`` (HMAC-SHA256, same scheme as payment webhooks).

This is the protocol-level extension point that lets external Org
Harness systems (Paperclip, OpenHarness, in-house orchestrators)
plug into ACN without ACN itself implementing organisation logic.

Both columns are nullable: existing subnets remain valid with NULL
(no harness registered → only the default platform webhook fires).

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-05-12 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subnets",
        sa.Column("harness_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "subnets",
        sa.Column("harness_secret", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subnets", "harness_secret")
    op.drop_column("subnets", "harness_url")
