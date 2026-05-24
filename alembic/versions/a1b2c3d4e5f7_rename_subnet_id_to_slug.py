"""rename subnets.subnet_id → slug, parent_subnet_id → parent_slug

Revision ID: a1b2c3d4e5f7
Revises: 2b3c4d5e6f7a
Create Date: 2026-05-23

Renames the primary-key column of the ``subnets`` table from the
legacy ``subnet_id`` (a slug/human-readable string) to ``slug``, and
the nesting self-reference ``parent_subnet_id`` to ``parent_slug``.

Cross-table references (tasks.subnet_id, subnet_join_requests.subnet_id,
subnet_allowlist.subnet_id) store slug *values* but are named for the
concept "which subnet does this row belong to" — those column names are
kept as-is to avoid a broader disruptive migration; they will be revisited
in a dedicated cross-table cleanup migration once all application code has
been updated to use the entity attribute ``subnet_slug``.

The partial index ``subnets_parent_idx`` is rebuilt to reference the
renamed column.
"""
from __future__ import annotations

from alembic import op

revision: str = "a1b2c3d4e5f7"
down_revision: str | None = "2b3c4d5e6f7a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename primary-key column subnet_id → slug.
    # PostgreSQL renames the column and updates its primary-key
    # constraint automatically; indexes referencing the column by
    # position are also updated automatically, but named indexes that
    # reference the column by name (partial indexes with text predicates)
    # must be rebuilt manually.
    op.alter_column("subnets", "subnet_id", new_column_name="slug")

    # Rename the self-referencing nesting column.
    op.alter_column("subnets", "parent_subnet_id", new_column_name="parent_slug")

    # Rebuild the partial index that references the renamed column by name
    # in its WHERE clause.  Drop the old one first (still references the
    # old column name in its predicate text on some PG versions).
    op.drop_index("subnets_parent_idx", table_name="subnets")
    op.execute(
        "CREATE INDEX subnets_parent_idx ON subnets (parent_slug) "
        "WHERE parent_slug IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("subnets_parent_idx", table_name="subnets")
    op.execute(
        "CREATE INDEX subnets_parent_idx ON subnets (parent_subnet_id) "
        "WHERE parent_subnet_id IS NOT NULL"
    )
    op.alter_column("subnets", "parent_slug", new_column_name="parent_subnet_id")
    op.alter_column("subnets", "slug", new_column_name="subnet_id")
