"""add subnet_id to tasks for private task visibility

Revision ID: b2c3d4e5f6a7
Revises: 1e400bcfd4ec
Create Date: 2026-04-07 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "1e400bcfd4ec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("subnet_id", sa.String(100), nullable=True))
    op.create_index("ix_tasks_subnet_id", "tasks", ["subnet_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_subnet_id", table_name="tasks")
    op.drop_column("tasks", "subnet_id")
