"""one active exec workspace per task / per org (D15)

Partial unique indexes so closed rows do not block a new register.
allowlist workspaces are unrestricted (no org_id / task_id occupancy).

Revision ID: c0d1e2f3a4b5
Revises: b9d0e1f2a3b4
Create Date: 2026-08-29 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c0d1e2f3a4b5"
down_revision: str | None = "b9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_exec_workspaces_active_task",
        "exec_workspaces",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND task_id IS NOT NULL"),
    )
    op.create_index(
        "uq_exec_workspaces_active_org",
        "exec_workspaces",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'active' AND org_id IS NOT NULL AND admit = 'org'"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_exec_workspaces_active_org", table_name="exec_workspaces"
    )
    op.drop_index(
        "uq_exec_workspaces_active_task", table_name="exec_workspaces"
    )
