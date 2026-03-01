"""add agent wallet_addresses and token_pricing columns

Revision ID: a1b2c3d4e5f6
Revises: 8d958bd38c11
Create Date: 2026-02-28 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "8d958bd38c11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("wallet_addresses", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "agents",
        sa.Column("token_pricing", postgresql.JSONB(), nullable=True),
    )
    op.create_index(
        "ix_agents_wallet_addresses",
        "agents",
        ["wallet_addresses"],
        postgresql_using="gin",
    )
    # Backfill: copy existing wallet_address into wallet_addresses["ethereum"] for non-null rows
    op.execute(
        """
        UPDATE agents
        SET wallet_addresses = jsonb_build_object('ethereum', wallet_address)
        WHERE wallet_address IS NOT NULL AND wallet_addresses IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_agents_wallet_addresses", table_name="agents")
    op.drop_column("agents", "token_pricing")
    op.drop_column("agents", "wallet_addresses")
