"""migrate subnet_id to UUID — add id (UUID PK) + slug column

Semantically correct subnet identifier migration:

- ``subnets.subnet_id`` (String PK, human-readable slug) is replaced by:
  - ``subnets.id`` (UUID PK) — opaque, stable machine identifier
  - ``subnets.slug`` (String UNIQUE NOT NULL) — the former slug value
- ``subnets.parent_subnet_id`` (slug String) → ``subnets.parent_id`` (UUID)
- All referencing tables migrated from slug values to UUID values:
  - ``agents.subnet_ids``
  - ``tasks.subnet_id``
  - ``subnet_join_requests.subnet_id``
  - ``subnet_allowlist.subnet_id`` (part of composite PK)

Privacy rationale
-----------------
Human-readable subnet slugs like ``acnlabs-core`` reveal organizational
structure when exposed through ``SubnetStub``.  UUIDs are opaque; once
``subnet_id`` is a UUID the stub can safely include it without leaking
naming conventions.

Dual-resolution routing
-----------------------
The application layer (``resolve_subnet_ref``) accepts both UUIDs and
slugs so CLI / human callers can still write ``acn subnet get acnlabs-core``
while machine-to-machine always uses the UUID.

Data migration safety
---------------------
- Every step backfills before dropping the old column.
- ``agents.subnet_ids`` uses ``UNNEST … JOIN subnets`` to convert; slugs
  not found in ``subnets`` are dropped (orphan cleanup).
- ``subnet_allowlist`` PK swap done via temp column to avoid duplicate
  primary key states.
- Redis key migration is handled by ``scripts/migrate_subnet_keys_to_uuid.py``
  which must be run after this Alembic migration.

Revision ID: 1a2b3c4d5e6f
Revises: f6a7b8c9d0e1
Create Date: 2026-05-21 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "1a2b3c4d5e6f"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # =========================================================================
    # Step 1 — subnets: add slug + id columns, swap PK
    # =========================================================================

    # 1a. Add slug column (backfill from current subnet_id)
    op.add_column(
        "subnets",
        sa.Column("slug", sa.String(100), nullable=True),
    )
    op.execute(sa.text("UPDATE subnets SET slug = subnet_id"))
    op.alter_column("subnets", "slug", nullable=False)
    op.create_unique_constraint("uq_subnets_slug", "subnets", ["slug"])

    # 1b. Add id UUID column (gen_random_uuid fills every row atomically)
    op.add_column(
        "subnets",
        sa.Column(
            "id",
            UUID(as_uuid=False),
            nullable=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
    )
    # Backfill any rows that didn't get a server_default (should be none, but be safe)
    op.execute(sa.text("UPDATE subnets SET id = gen_random_uuid() WHERE id IS NULL"))
    op.alter_column("subnets", "id", nullable=False)

    # 1c. Swap primary key: drop old String PK, add UUID PK
    op.drop_constraint("subnets_pkey", "subnets", type_="primary")
    # Keep subnet_id column for now (needed in step 3 for FK backfills)
    op.create_primary_key("subnets_pkey", "subnets", ["id"])

    # =========================================================================
    # Step 2 — subnets: convert parent_subnet_id (slug) → parent_id (UUID)
    # =========================================================================

    # 2a. Drop old partial index before modifying the column
    op.drop_index("subnets_parent_idx", table_name="subnets")

    # 2b. Add parent_id UUID column
    op.add_column(
        "subnets",
        sa.Column("parent_id", UUID(as_uuid=False), nullable=True),
    )

    # 2c. Backfill: resolve slug → UUID via self-join
    op.execute(
        sa.text(
            "UPDATE subnets child "
            "SET parent_id = parent.id "
            "FROM subnets parent "
            "WHERE child.parent_subnet_id = parent.slug "
            "AND child.parent_subnet_id IS NOT NULL"
        )
    )

    # 2d. Drop old slug-based parent column
    op.drop_column("subnets", "parent_subnet_id")

    # 2e. Recreate the partial index on the new UUID column
    op.create_index(
        "subnets_parent_idx",
        "subnets",
        ["parent_id"],
        postgresql_where=sa.text("parent_id IS NOT NULL"),
    )

    # 2f. Drop the now-redundant old subnet_id column
    # (was the PK; slug carries the same value; all FK backfills use slug)
    op.drop_column("subnets", "subnet_id")

    # =========================================================================
    # Step 3 — agents.subnet_ids: slug array → UUID text array
    # =========================================================================
    # UNNEST + JOIN resolves each slug element to its UUID.  Slugs that no
    # longer exist in subnets (orphan references) are silently dropped — they
    # were already dangling and removal is the correct clean-up action.
    op.execute(
        sa.text(
            """
            UPDATE agents
            SET subnet_ids = ARRAY(
                SELECT s.id
                FROM UNNEST(subnet_ids) WITH ORDINALITY AS u(slug_val, ord)
                JOIN subnets s ON s.slug = u.slug_val
                ORDER BY u.ord
            )::varchar[]
            WHERE subnet_ids IS NOT NULL
            """
        )
    )

    # =========================================================================
    # Step 4 — tasks.subnet_id: slug → UUID text
    # =========================================================================
    op.execute(
        sa.text(
            "UPDATE tasks t "
            "SET subnet_id = s.id "
            "FROM subnets s "
            "WHERE t.subnet_id = s.slug "
            "AND t.subnet_id IS NOT NULL"
        )
    )

    # =========================================================================
    # Step 5 — subnet_join_requests.subnet_id: slug → UUID
    # =========================================================================
    # subnet_id is not part of the PK here (PK = request_id), so a direct
    # UPDATE is sufficient.
    op.execute(
        sa.text(
            "UPDATE subnet_join_requests sjr "
            "SET subnet_id = s.id "
            "FROM subnets s "
            "WHERE sjr.subnet_id = s.slug"
        )
    )

    # =========================================================================
    # Step 6 — subnet_allowlist.subnet_id: slug → UUID
    # subnet_id IS part of the composite PK (subnet_id, agent_id), so we
    # must swap via a temp column to avoid duplicate PK states.
    # =========================================================================
    # 6a. Drop PK constraint and dependent index
    op.drop_index("ix_subnet_allowlist_agent_id", table_name="subnet_allowlist")
    op.drop_constraint("subnet_allowlist_pkey", "subnet_allowlist", type_="primary")

    # 6b. Add temp column and backfill
    op.add_column(
        "subnet_allowlist",
        sa.Column("subnet_uuid", sa.String(100), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE subnet_allowlist sa "
            "SET subnet_uuid = s.id "
            "FROM subnets s "
            "WHERE sa.subnet_id = s.slug"
        )
    )
    op.alter_column("subnet_allowlist", "subnet_uuid", nullable=False)

    # 6c. Swap columns
    op.drop_column("subnet_allowlist", "subnet_id")
    op.alter_column("subnet_allowlist", "subnet_uuid", new_column_name="subnet_id")

    # 6d. Recreate PK and index
    op.create_primary_key(
        "subnet_allowlist_pkey", "subnet_allowlist", ["subnet_id", "agent_id"]
    )
    op.create_index(
        "ix_subnet_allowlist_agent_id", "subnet_allowlist", ["agent_id"]
    )


def downgrade() -> None:
    # The downgrade reverses every step in reverse order.
    # It relies on the slug column (still present in the subnets table)
    # to convert UUIDs back to slugs.

    # =========================================================================
    # Step 6 — subnet_allowlist: UUID → slug (reverse of upgrade step 6)
    # =========================================================================
    op.drop_index("ix_subnet_allowlist_agent_id", table_name="subnet_allowlist")
    op.drop_constraint("subnet_allowlist_pkey", "subnet_allowlist", type_="primary")

    op.add_column(
        "subnet_allowlist",
        sa.Column("subnet_slug", sa.String(100), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE subnet_allowlist sa "
            "SET subnet_slug = s.slug "
            "FROM subnets s "
            "WHERE sa.subnet_id = s.id"
        )
    )
    op.alter_column("subnet_allowlist", "subnet_slug", nullable=False)
    op.drop_column("subnet_allowlist", "subnet_id")
    op.alter_column("subnet_allowlist", "subnet_slug", new_column_name="subnet_id")
    op.create_primary_key(
        "subnet_allowlist_pkey", "subnet_allowlist", ["subnet_id", "agent_id"]
    )
    op.create_index(
        "ix_subnet_allowlist_agent_id", "subnet_allowlist", ["agent_id"]
    )

    # =========================================================================
    # Step 5 — subnet_join_requests: UUID → slug
    # =========================================================================
    op.execute(
        sa.text(
            "UPDATE subnet_join_requests sjr "
            "SET subnet_id = s.slug "
            "FROM subnets s "
            "WHERE sjr.subnet_id = s.id"
        )
    )

    # =========================================================================
    # Step 4 — tasks: UUID → slug
    # =========================================================================
    op.execute(
        sa.text(
            "UPDATE tasks t "
            "SET subnet_id = s.slug "
            "FROM subnets s "
            "WHERE t.subnet_id = s.id "
            "AND t.subnet_id IS NOT NULL"
        )
    )

    # =========================================================================
    # Step 3 — agents.subnet_ids: UUID array → slug array
    # =========================================================================
    op.execute(
        sa.text(
            """
            UPDATE agents
            SET subnet_ids = ARRAY(
                SELECT s.slug
                FROM UNNEST(subnet_ids) WITH ORDINALITY AS u(uuid_val, ord)
                JOIN subnets s ON s.id = u.uuid_val
                ORDER BY u.ord
            )::varchar[]
            WHERE subnet_ids IS NOT NULL
            """
        )
    )

    # =========================================================================
    # Steps 2 + 1 — subnets: restore subnet_id PK from slug
    # =========================================================================
    # Re-add parent_subnet_id (slug) from parent_id (UUID)
    op.drop_index("subnets_parent_idx", table_name="subnets")
    op.add_column(
        "subnets",
        sa.Column("parent_subnet_id", sa.String(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE subnets child "
            "SET parent_subnet_id = parent.slug "
            "FROM subnets parent "
            "WHERE child.parent_id = parent.id "
            "AND child.parent_id IS NOT NULL"
        )
    )
    op.drop_column("subnets", "parent_id")
    op.create_index(
        "subnets_parent_idx",
        "subnets",
        ["parent_subnet_id"],
        postgresql_where=sa.text("parent_subnet_id IS NOT NULL"),
    )

    # Restore subnet_id column (slug value) as PK
    op.add_column(
        "subnets",
        sa.Column("subnet_id", sa.String(), nullable=True),
    )
    op.execute(sa.text("UPDATE subnets SET subnet_id = slug"))
    op.alter_column("subnets", "subnet_id", nullable=False)

    op.drop_constraint("subnets_pkey", "subnets", type_="primary")
    op.drop_column("subnets", "id")
    op.drop_constraint("uq_subnets_slug", "subnets", type_="unique")
    op.drop_column("subnets", "slug")
    op.create_primary_key("subnets_pkey", "subnets", ["subnet_id"])
