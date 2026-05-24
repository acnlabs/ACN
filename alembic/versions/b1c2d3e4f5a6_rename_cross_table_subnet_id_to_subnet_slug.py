"""rename tasks.subnet_id → subnet_slug; allowlist/join_requests .subnet_id → slug

Revision ID: b1c2d3e4f5a6
Revises: a1b2c3d4e5f6
Create Date: 2026-05-24

Step 2 of the slug-rename migration (ADR slug rename).

Step 1 (``a1b2c3d4e5f6``) renamed the Postgres PK column of the
``subnets`` table from ``subnet_id`` → ``slug`` and the self-reference
``parent_subnet_id`` → ``parent_slug``.

This step renames the *cross-table* FK-style columns that carry a slug
value but whose column names were still the legacy ``subnet_id``:

- ``tasks.subnet_id``             → ``tasks.subnet_slug``
- ``subnet_join_requests.subnet_id`` → ``subnet_join_requests.slug``
- ``subnet_allowlist.subnet_id``  → ``subnet_allowlist.slug``

These three columns are NOT true FK columns (they reference the
subnets slug but carry no FK constraint — see ADR notes on schema
decisions).  Renaming is therefore a pure ``ALTER COLUMN … RENAME``
with no FK cascade work needed.

The Python ORM models already use the new attribute names (``slug``,
``subnet_slug``) with ``mapped_column("subnet_id", …)`` overrides;
once this migration runs the overrides become redundant and should be
removed in a follow-up cleanup commit.

Indexes are automatically updated by PostgreSQL on a column rename;
no manual index rebuild is required here.
"""
from __future__ import annotations

from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # tasks: subnet_id → subnet_slug
    op.alter_column("tasks", "subnet_id", new_column_name="subnet_slug")

    # subnet_join_requests: subnet_id → slug
    # The composite PK is (subnet_id, agent_id) or (subnet_id, request_id)
    # depending on table; column rename is safe — the PK constraint stays,
    # only the column name changes.
    op.alter_column("subnet_join_requests", "subnet_id", new_column_name="slug")

    # subnet_allowlist: subnet_id → slug
    op.alter_column("subnet_allowlist", "subnet_id", new_column_name="slug")


def downgrade() -> None:
    op.alter_column("subnet_allowlist", "slug", new_column_name="subnet_id")
    op.alter_column("subnet_join_requests", "slug", new_column_name="subnet_id")
    op.alter_column("tasks", "subnet_slug", new_column_name="subnet_id")
