"""add org harness tables (orgs, org_memberships, org_work_items)

ADR-0014 Phase 1 Kernel + minimal work queue.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "orgs",
        sa.Column("org_id", sa.String(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("charter", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("owner_kind", sa.String(length=16), nullable=False, server_default="none"),
        sa.Column("owner_subject", sa.String(), nullable=True),
        sa.Column("created_by_kind", sa.String(length=16), nullable=False),
        sa.Column("created_by_subject", sa.String(), nullable=False),
        sa.Column("subnet_id", sa.String(), nullable=False),
        sa.Column("steward_agent_id", sa.String(), nullable=False),
        sa.Column("plugins", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("roles", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_orgs_subnet_id", "orgs", ["subnet_id"])
    op.create_index("ix_orgs_steward_agent_id", "orgs", ["steward_agent_id"])

    op.create_table(
        "org_memberships",
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False, server_default="worker"),
        sa.Column("reports_to", sa.String(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("org_id", "agent_id"),
    )
    op.create_index("ix_org_memberships_agent_id", "org_memberships", ["agent_id"])

    op.create_table(
        "org_work_items",
        sa.Column("work_id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="todo"),
        sa.Column("assignee_agent_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_org_work_items_org_id", "org_work_items", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_org_work_items_org_id", table_name="org_work_items")
    op.drop_table("org_work_items")
    op.drop_index("ix_org_memberships_agent_id", table_name="org_memberships")
    op.drop_table("org_memberships")
    op.drop_index("ix_orgs_steward_agent_id", table_name="orgs")
    op.drop_index("ix_orgs_subnet_id", table_name="orgs")
    op.drop_table("orgs")
