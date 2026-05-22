"""add opaque UUID column to subnets (additive)

Privacy-only addition. Adds a new ``id`` column (UUID) to ``subnets`` so
``SubnetStub`` can expose an opaque identifier instead of the
human-readable ``subnet_id`` slug. Everything else stays unchanged:

- ``subnet_id`` remains the primary key and the canonical identifier
  used by every API route, agent.subnet_ids array, tasks.subnet_id
  column, Redis index, CLI, SDK, and frontend reference.
- ``id`` is a *secondary* opaque identifier, surfaced only in
  ``SubnetInfo.id`` and ``SubnetStub.id``.

Why additive (not a slug → UUID rename)
---------------------------------------
A full migration was scoped at ~169 referencing files plus 4 referencing
tables plus a Redis key namespace. The privacy goal — don't leak
``acnlabs-core`` in stubs returned to anonymous callers — only requires
a separate opaque identifier; it does NOT require flipping the PK.
Adding a column avoids any data migration on agents/tasks/Redis and
keeps every existing test green.

Schema change
-------------
``id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid()``

PostgreSQL ≥13 has ``gen_random_uuid()`` built into ``pgcrypto`` (since
PG 13 also into core). The ``server_default`` fills every existing row
during the ``ALTER TABLE`` so no separate backfill statement is needed.

Idempotency / safety
--------------------
- Re-running ``upgrade()`` is rejected by Alembic (revision tracking).
- The unique constraint catches the (vanishingly unlikely) collision
  case where two rows somehow get the same ``gen_random_uuid()``.
- ``downgrade()`` drops the column; UUIDs are not referenced anywhere
  outside of API responses, so dropping is loss-free for any consumer
  that hasn't started persisting them.

Revision ID: 2b3c4d5e6f7a
Revises: a3b5c7d9e1f2
Create Date: 2026-05-21 12:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "2b3c4d5e6f7a"
down_revision: str | None = "a3b5c7d9e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Ensure pgcrypto is available for ``gen_random_uuid()`` on PG ≤12.
    # No-op on PG 13+ where the function lives in core.
    op.execute(sa.text('CREATE EXTENSION IF NOT EXISTS "pgcrypto"'))

    # Add column nullable first so existing rows accept the default,
    # then promote to NOT NULL once gen_random_uuid() has filled them.
    op.add_column(
        "subnets",
        sa.Column(
            "id",
            UUID(as_uuid=False),
            nullable=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
    )
    # Defensive backfill for any row that bypassed the server_default
    # (e.g. inserted via raw SQL during the same transaction window).
    op.execute(sa.text("UPDATE subnets SET id = gen_random_uuid() WHERE id IS NULL"))
    op.alter_column("subnets", "id", nullable=False)

    op.create_unique_constraint("uq_subnets_id", "subnets", ["id"])


def downgrade() -> None:
    op.drop_constraint("uq_subnets_id", "subnets", type_="unique")
    op.drop_column("subnets", "id")
