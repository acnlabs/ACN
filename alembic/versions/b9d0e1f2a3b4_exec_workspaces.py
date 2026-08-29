"""add exec_workspaces + exec_workspace_attestations

ACN Execution Workspace (Network Core). Pointer + admit + owner
attestations. Kernel does not provision a sandbox.

Revision ID: b9d0e1f2a3b4
Revises: a8c9d0e1f2b3
Create Date: 2026-08-28 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b9d0e1f2a3b4"
down_revision: str | None = "a8c9d0e1f2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "exec_workspaces",
        sa.Column("workspace_id", sa.String(), primary_key=True),
        sa.Column("owner_agent_id", sa.String(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "execution_env",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("admit", sa.String(length=16), nullable=False),
        sa.Column("org_id", sa.String(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("allowlist", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_exec_workspaces_owner_agent_id",
        "exec_workspaces",
        ["owner_agent_id"],
    )
    op.create_index("ix_exec_workspaces_org_id", "exec_workspaces", ["org_id"])
    op.create_index("ix_exec_workspaces_task_id", "exec_workspaces", ["task_id"])

    op.create_table(
        "exec_workspace_attestations",
        sa.Column("attestation_id", sa.String(), primary_key=True),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            server_default="workspace_owner",
        ),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("work_id", sa.String(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("hop_id", sa.String(), nullable=True),
        sa.Column("artifact", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_exec_workspace_attestations_workspace_id",
        "exec_workspace_attestations",
        ["workspace_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_exec_workspace_attestations_workspace_id",
        table_name="exec_workspace_attestations",
    )
    op.drop_table("exec_workspace_attestations")
    op.drop_index("ix_exec_workspaces_task_id", table_name="exec_workspaces")
    op.drop_index("ix_exec_workspaces_org_id", table_name="exec_workspaces")
    op.drop_index("ix_exec_workspaces_owner_agent_id", table_name="exec_workspaces")
    op.drop_table("exec_workspaces")
