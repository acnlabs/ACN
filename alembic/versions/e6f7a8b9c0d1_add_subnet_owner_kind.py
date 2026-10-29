"""add subnets.owner_kind

Governance owner can be an agent, a human IdP subject, or unclaimed.
Existing rows stay ``agent``.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-10-29 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e6f7a8b9c0d1"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subnets",
        sa.Column(
            "owner_kind",
            sa.String(length=16),
            nullable=False,
            server_default="agent",
        ),
    )


def downgrade() -> None:
    op.drop_column("subnets", "owner_kind")
