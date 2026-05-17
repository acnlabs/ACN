"""add subnet nesting fields (parent_subnet_id, lifecycle, linked_task_id)

ADR-0003 Phase 1: extends the ``subnets`` table with three optional
fields plus two partial indexes that let one subnet declare itself a
child of another, optionally bound to a task that auto-dissolves it
on terminal state.

This migration only ships the schema and indexes. The service /
route / cascade consumers land in Phase 2 (acnlabs/ACN#50) and
Phase 3 (acnlabs/ACN#51). Until then the new columns can only be
populated through their defaults, so legacy callers behave
unchanged.

Columns
-------
- ``parent_subnet_id VARCHAR NULL`` — the parent subnet's id, or
  NULL for top-level. Immutable after insert per ADR-0003 §5.
- ``lifecycle VARCHAR NOT NULL DEFAULT 'persistent'`` — either
  ``'persistent'`` (default, original semantics) or
  ``'task_scoped'`` (auto-dissolves when ``linked_task_id`` reaches
  a terminal state).
- ``linked_task_id VARCHAR NULL`` — the task id the subnet is bound
  to when ``lifecycle = 'task_scoped'``. NULL otherwise.

Indexes
-------
Partial indexes only — top-level subnets without a parent / linked
task don't contribute rows, keeping the index size proportional to
the actual nesting cardinality.

- ``subnets_parent_idx`` ON ``parent_subnet_id`` WHERE NOT NULL
- ``subnets_linked_task_idx`` ON ``linked_task_id`` WHERE NOT NULL

Both are shipped here (not split across phases) so the schema
change is a single atomic event — Phase 3's cascade lookups
(``find_by_linked_task``) reuse the index without needing another
migration.

Backward compatibility
----------------------
``lifecycle`` carries a ``server_default`` that PostgreSQL fills in
for every existing row at ALTER time, so no separate backfill is
required. ``parent_subnet_id`` and ``linked_task_id`` are nullable
with no default, so they read back as NULL on legacy rows — exactly
matching the entity-layer default for "top-level persistent
subnet". The ``Subnet.from_dict`` round-trip also tolerates missing
keys, so Redis legacy rows behave identically.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-05-17 17:50:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Columns: lifecycle carries a server_default so ALTER fills
    # every existing row in a single statement; parent_subnet_id and
    # linked_task_id stay NULL on legacy rows.
    op.add_column(
        "subnets",
        sa.Column("parent_subnet_id", sa.String(), nullable=True),
    )
    op.add_column(
        "subnets",
        sa.Column(
            "lifecycle",
            sa.String(),
            nullable=False,
            server_default="persistent",
        ),
    )
    op.add_column(
        "subnets",
        sa.Column("linked_task_id", sa.String(), nullable=True),
    )

    # Partial indexes: only nesting rows contribute. The naming
    # mirrors the SQLAlchemy ``Index(...)`` declarations on
    # ``SubnetModel.__table_args__`` so drift between the model and
    # the migration would surface as a diff during
    # ``alembic revision --autogenerate`` review.
    op.create_index(
        "subnets_parent_idx",
        "subnets",
        ["parent_subnet_id"],
        postgresql_where=sa.text("parent_subnet_id IS NOT NULL"),
    )
    op.create_index(
        "subnets_linked_task_idx",
        "subnets",
        ["linked_task_id"],
        postgresql_where=sa.text("linked_task_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Drop indexes before columns — PostgreSQL won't allow dropping
    # a column that an index still references.
    op.drop_index("subnets_linked_task_idx", table_name="subnets")
    op.drop_index("subnets_parent_idx", table_name="subnets")
    op.drop_column("subnets", "linked_task_id")
    op.drop_column("subnets", "lifecycle")
    op.drop_column("subnets", "parent_subnet_id")
