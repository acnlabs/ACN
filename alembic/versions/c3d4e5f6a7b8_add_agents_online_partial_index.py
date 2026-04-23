"""add partial index on agents(agent_id) where status='online'

Supports `PostgresAgentRepository.mark_offline_stale`, which pages
through ONLINE agents with `WHERE status='online' AND agent_id > :c
ORDER BY agent_id LIMIT N`. Without this index the query falls back
to a pkey scan that must skim through OFFLINE rows to find ONLINE
ones, so at million-agent scale it is slower than the old
`find_all`-based implementation.

A partial index keeps the index small (only rows currently online
are indexed) and makes the query an index-only range scan.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-04-23 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_agents_status_online_agent_id",
        "agents",
        ["agent_id"],
        postgresql_where="status = 'online'",
    )


def downgrade() -> None:
    op.drop_index("ix_agents_status_online_agent_id", table_name="agents")
