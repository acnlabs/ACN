"""initial_schema

Revision ID: 8d958bd38c11
Revises:
Create Date: 2026-02-28

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "8d958bd38c11"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # =========================================================
    # tasks
    # =========================================================
    op.create_table(
        "tasks",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("creator_id", sa.String(), nullable=False),
        sa.Column("creator_type", sa.String(32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("reward_amount", sa.String(64), nullable=False, server_default="0"),
        sa.Column("reward_currency", sa.String(32), nullable=False, server_default="points"),
        sa.Column("assignee_id", sa.String(), nullable=True),
        sa.Column("is_multi_participant", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("max_completions", sa.Integer(), nullable=True),
        sa.Column("completed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("required_skills", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_creator_id", "tasks", ["creator_id"])
    op.create_index("ix_tasks_assignee_id", "tasks", ["assignee_id"])
    op.create_index("ix_tasks_mode", "tasks", ["mode"])
    op.create_index(
        "ix_tasks_required_skills",
        "tasks",
        ["required_skills"],
        postgresql_using="gin",
    )

    # =========================================================
    # participations
    # =========================================================
    op.create_table(
        "participations",
        sa.Column("participation_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("participant_id", sa.String(), nullable=False),
        sa.Column("participant_name", sa.Text(), nullable=False),
        sa.Column("participant_type", sa.String(32), nullable=False, server_default="agent"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("submission", sa.Text(), nullable=True),
        sa.Column(
            "submission_artifacts", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_response_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_request_id", sa.String(), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.String(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("participation_id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_participations_task_id", "participations", ["task_id"])
    op.create_index("ix_participations_participant_id", "participations", ["participant_id"])
    op.create_index("ix_participations_status", "participations", ["status"])
    op.create_index(
        "ix_participations_task_participant",
        "participations",
        ["task_id", "participant_id"],
    )

    # =========================================================
    # agents
    # =========================================================
    op.create_table(
        "agents",
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("owner", sa.String(), nullable=True),
        sa.Column("endpoint", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="online"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("skills", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("subnet_ids", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("api_key", sa.String(), nullable=True),
        sa.Column("auth0_client_id", sa.String(), nullable=True),
        sa.Column("auth0_token_endpoint", sa.String(), nullable=True),
        sa.Column("claim_status", sa.String(32), nullable=True),
        sa.Column("verification_code", sa.String(16), nullable=True),
        sa.Column("referrer_id", sa.String(), nullable=True),
        sa.Column("wallet_address", sa.String(), nullable=True),
        sa.Column("accepts_payment", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("payment_methods", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("agent_card", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.create_index("ix_agents_owner", "agents", ["owner"])
    op.create_index("ix_agents_owner_endpoint", "agents", ["owner", "endpoint"])
    op.create_index("ix_agents_api_key", "agents", ["api_key"], unique=True)
    op.create_index(
        "ix_agents_skills", "agents", ["skills"], postgresql_using="gin"
    )

    # =========================================================
    # subnets
    # =========================================================
    op.create_table(
        "subnets",
        sa.Column("subnet_id", sa.String(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_private", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("security_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("member_agent_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("subnet_id"),
    )
    op.create_index("ix_subnets_owner", "subnets", ["owner"])

    # =========================================================
    # billing_transactions
    # =========================================================
    op.create_table(
        "billing_transactions",
        sa.Column("transaction_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("agent_owner_id", sa.String(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("total_credits", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("network_fee_credits", sa.Float(), nullable=False, server_default="0"),
        sa.Column("agent_income_credits", sa.Float(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("transaction_id"),
    )
    op.create_index("ix_billing_transactions_user_id", "billing_transactions", ["user_id"])
    op.create_index("ix_billing_transactions_agent_id", "billing_transactions", ["agent_id"])
    op.create_index("ix_billing_transactions_task_id", "billing_transactions", ["task_id"])
    op.create_index("ix_billing_transactions_status", "billing_transactions", ["status"])
    op.create_index("ix_billing_transactions_created_at", "billing_transactions", ["created_at"])

    # =========================================================
    # activities
    # =========================================================
    op.create_table(
        "activities",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("actor_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("points", sa.Integer(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_activities_type", "activities", ["type"])
    op.create_index("ix_activities_actor_id", "activities", ["actor_id"])
    op.create_index("ix_activities_task_id", "activities", ["task_id"])
    op.create_index("ix_activities_timestamp", "activities", ["timestamp"])


def downgrade() -> None:
    op.drop_table("activities")
    op.drop_table("billing_transactions")
    op.drop_table("subnets")
    op.drop_table("agents")
    op.drop_table("participations")
    op.drop_table("tasks")
