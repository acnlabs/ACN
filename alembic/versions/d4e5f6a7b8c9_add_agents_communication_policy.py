"""add agents.communication_policy JSONB column

Phase 1 of the ACN communication economic model proposal
(see docs/features/acn-communication-economic-model.md). Adds a
gateway-level access control field on agent rows.

NULL is treated as the implicit default {"mode": "open"} by the domain
layer (Agent.__post_init__), which keeps existing agents on the
push-to-inbox behavior with no migration of existing data required.

Future phases extend the JSON shape with allowlist, rate_limit, and
attention_fee fields. JSONB lets those evolutions land without another
ALTER TABLE.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-04-29 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("communication_policy", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agents", "communication_policy")
