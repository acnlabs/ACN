"""add agents.invoke_floor_credits

Integer Credits charged on invoke writeback when the hop has no token
usage. NULL means unlisted (Host rejects that complete). 0 means the
owner declared no-usage hops free. Cap is enforced in the API
(0..100000), not the column.

Revision ID: d5e6f7a8b9c0
Revises: c0d1e2f3a4b5
Create Date: 2026-09-24 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "c0d1e2f3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("invoke_floor_credits", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agents", "invoke_floor_credits")
