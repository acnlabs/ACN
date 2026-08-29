"""add orgs.execution_env JSONB (shared workplace pointer)

Org Harness stores a pointer only; members follow git/url on their own L1.
Kernel does not provision a sandbox.

Revision ID: a8c9d0e1f2b3
Revises: f5a6b7c8d9e0
Create Date: 2026-08-28 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a8c9d0e1f2b3"
down_revision: str | None = "f5a6b7c8d9e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orgs",
        sa.Column(
            "execution_env",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("orgs", "execution_env")
