"""add participations.resubmit_count

Tracks how many times a participant has resubmitted after rejection.
Org Harnesses use this together with Task.max_resubmit_attempts (stored in
task JSONB metadata) to cap the grader-retry loop and prevent agents from
resubmitting indefinitely.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-05-12 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "participations",
        sa.Column(
            "resubmit_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("participations", "resubmit_count")
