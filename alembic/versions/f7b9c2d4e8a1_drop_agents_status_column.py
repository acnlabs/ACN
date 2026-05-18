"""drop agents.status column (alive-as-single-source phase 2)

Phase 1 removed every reader of ``agents.status`` from production code
paths and stopped the watchdog that flipped it (see
``IAgentRepository.mark_offline_stale``, deleted with PR #70). Phase 2
removes the write surface from the entity / Postgres model and now
this migration drops the dead column itself, together with the
partial index that supported the watchdog's keyset scan.

Online-ness for an agent is now a function over the Redis
``acn:agents:{id}:alive`` TTL key, queried at read time via
``AgentService.is_alive`` / ``filter_alive``. The API-layer field
``AgentInfo.status`` is computed in the route serializers; clients
that consume the JSON response see no schema change.

Safety
------
- The Dockerfile applies ``alembic upgrade head`` at container
  startup, before uvicorn binds the port. Railway's deploy model is
  single-instance stop-old/start-new, so there is no window where an
  OLD pod still writes ``status`` while a NEW pod has dropped the
  column. (Verified against ``railway.json`` — no ``numReplicas``
  override, no rolling strategy.)
- ``op.execute("ALTER TABLE ... DROP COLUMN IF EXISTS status")``
  rather than ``op.drop_column`` so a partial / replayed run cannot
  abort on a missing column.
- The column was plain ``String(32)`` not a PostgreSQL ENUM, so
  there is no ``agentstatus`` TYPE to drop alongside it.
- ``ix_agents_status_online_agent_id`` is dropped *before* the
  column, since Postgres refuses to drop a column that an index
  still references.

Revision ID: f7b9c2d4e8a1
Revises: e1f2a3b4c5d6
Create Date: 2026-05-18 22:50:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f7b9c2d4e8a1"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Order matters: index must go before the column it references.
    op.execute(
        "DROP INDEX IF EXISTS ix_agents_status_online_agent_id"
    )
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS status")


def downgrade() -> None:
    # Restore both the column and the partial index in the shape
    # they had at the time this migration ran. ``server_default``
    # backfills existing rows in one statement so the NOT NULL
    # constraint holds on add, matching the original DDL from
    # ``8d958bd38c11`` / the column's SQLAlchemy default.
    op.add_column(
        "agents",
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'online'"),
        ),
    )
    op.create_index(
        "ix_agents_status_online_agent_id",
        "agents",
        ["agent_id"],
        postgresql_where=sa.text("status = 'online'"),
    )
