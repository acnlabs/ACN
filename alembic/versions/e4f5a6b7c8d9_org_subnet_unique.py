"""unique orgs.subnet_id — one Org per fence (ADR-0014)

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "e4f5a6b7c8d9"
down_revision: str | None = "d3e4f5a6b7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint("uq_orgs_subnet_id", "orgs", ["subnet_id"])


def downgrade() -> None:
    op.drop_constraint("uq_orgs_subnet_id", "orgs", type_="unique")
