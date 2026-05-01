"""add agents.social_card_url column

Adds a SOCIAL.md pointer to the agent record. SOCIAL.md is the open
spec at https://agentsocial.one — agents publish a markdown + YAML
front-matter file at a well-known URL that describes their contact
mode, communication norms, economics, privacy posture, and boundaries.

ACN stores ONLY the URL — never the body. The consumption model
(https://agentsocial.one/consumption-model) requires consumers to
fetch the body on demand and honor the source's Cache-Control / ETag
so each agent owner remains the single source of truth for their own
contact terms. Storing the body here would turn ACN into a stale
mirror of every agent's social profile, which the spec explicitly
warns against.

Field is nullable: existing agents stay valid with NULL, and the
domain layer treats absence as "no SOCIAL.md published" (clients
fall back to defaults — same semantics as a missing file at the
well-known path).

Revision ID: e5f6a7b8c9d1
Revises: d4e5f6a7b8c9
Create Date: 2026-05-01 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d1"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("social_card_url", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agents", "social_card_url")
