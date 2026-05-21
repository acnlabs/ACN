"""Alembic migration ``2b3c4d5e6f7a_add_subnet_uuid`` regressions.

The full DDL is exercised against a real PostgreSQL in CI's
``alembic upgrade head`` job; this file pins the migration's
*structure* in unit-test land so contributors can catch obvious
mistakes (wrong revision chain, missing pgcrypto extension,
missing unique constraint, drop ordering) without spinning up a
database.

What we check
-------------
- ``revision`` / ``down_revision`` form a single-parent edge
  pointing at ``a3b5c7d9e1f2`` (the ADR-0005 join-request /
  allowlist head).
- ``upgrade()`` runs ``CREATE EXTENSION pgcrypto`` *before* adding
  the column (otherwise ``server_default=gen_random_uuid()`` would
  fail on PG ≤12 where the function lives in pgcrypto).
- ``upgrade()`` adds the ``id`` column with the right type
  (``UUID``), nullable=True initially, with
  ``server_default=gen_random_uuid()``.
- ``upgrade()`` then promotes the column to NOT NULL after a
  defensive backfill — in that order, in the same transaction.
- ``upgrade()`` creates a unique constraint on ``id`` so two rows
  cannot share the same opaque identifier.
- ``downgrade()`` drops the constraint *before* the column
  (PostgreSQL refuses to drop a column a constraint still
  references) and runs no data-mutating statements.
"""

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def migration_module():
    """Load the migration file as an isolated module.

    Alembic's ``versions/`` directory is not a Python package — each
    revision is loaded dynamically by ``ScriptDirectory``. We mirror
    that with ``importlib.util.spec_from_file_location`` rather than
    importing by dotted name (which would fail).
    """
    repo_root = Path(__file__).resolve().parents[2]
    migration_path = (
        repo_root
        / "alembic"
        / "versions"
        / "2b3c4d5e6f7a_add_subnet_uuid.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_migration_2b3c4d5e6f7a", migration_path
    )
    assert spec is not None and spec.loader is not None, (
        f"failed to build module spec for {migration_path}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_and_down_revision(migration_module):
    """Revision chain: this revision sits on top of the ADR-0005
    join-request / allowlist head. Pointing at the wrong ancestor
    would make ``alembic upgrade head`` either skip this migration
    or refuse with a multiple-head error."""
    assert migration_module.revision == "2b3c4d5e6f7a"
    assert migration_module.down_revision == "a3b5c7d9e1f2"


def test_upgrade_creates_pgcrypto_before_column(migration_module):
    """``CREATE EXTENSION pgcrypto`` must run before ``add_column``
    so the ``server_default=gen_random_uuid()`` evaluates on PG ≤12
    (where the function lives in the pgcrypto extension, not core).
    Reversing the order would fail outright on those clusters."""
    with patch.object(migration_module, "op") as mock_op:
        migration_module.upgrade()

    ordered = [
        c[0].split(".")[-1]
        for c in mock_op.mock_calls
        if c[0].split(".")[-1] in {"execute", "add_column"}
    ]
    assert ordered, "upgrade() recorded no execute/add_column calls"
    first_execute = ordered.index("execute")
    first_add_column = ordered.index("add_column")
    assert first_execute < first_add_column, (
        f"CREATE EXTENSION pgcrypto must precede add_column; got order: "
        f"{ordered!r}"
    )

    pgcrypto_sql = str(mock_op.execute.call_args_list[0].args[0])
    assert "CREATE EXTENSION" in pgcrypto_sql
    assert "pgcrypto" in pgcrypto_sql
    assert "IF NOT EXISTS" in pgcrypto_sql, (
        "extension creation must be idempotent — re-running upgrade() on a "
        "DB that already has pgcrypto should be a no-op, not a hard error"
    )


def test_upgrade_adds_id_column_with_server_default(migration_module):
    """``upgrade()`` must add a UUID ``id`` column with
    ``server_default=gen_random_uuid()`` so existing rows get
    populated during the ALTER on non-empty ``subnets`` tables.
    Without the default, ``ALTER TABLE ... ADD COLUMN id UUID NOT
    NULL`` would fail outright on populated tables."""
    with patch.object(migration_module, "op") as mock_op:
        migration_module.upgrade()

    add_calls = mock_op.add_column.call_args_list
    assert len(add_calls) == 1, (
        f"upgrade() should add exactly one column, got "
        f"{len(add_calls)}: {[c.args[1].name for c in add_calls]!r}"
    )

    table_arg, column = add_calls[0].args
    assert table_arg == "subnets"
    assert column.name == "id"
    assert column.nullable is True, (
        "column must start nullable so the ALTER succeeds; the "
        "alter_column NOT NULL promotion happens after backfill"
    )
    assert column.server_default is not None, (
        "id must carry server_default=gen_random_uuid() so existing "
        "rows are auto-filled during ALTER on a non-empty table"
    )
    default_text = (
        column.server_default.arg.text
        if hasattr(column.server_default.arg, "text")
        else str(column.server_default.arg)
    )
    assert "gen_random_uuid()" in default_text, (
        f"id server_default must be gen_random_uuid(); got {default_text!r}"
    )


def test_upgrade_promotes_column_to_not_null_after_backfill(migration_module):
    """After the defensive ``UPDATE ... WHERE id IS NULL`` backfill
    fills any rows that bypassed the ``server_default`` (e.g. raw
    SQL inserts in the same transaction window),
    ``alter_column(..., nullable=False)`` promotes the column to
    NOT NULL. Doing the promotion *before* the backfill would fail
    on those bypass rows."""
    with patch.object(migration_module, "op") as mock_op:
        migration_module.upgrade()

    alter_calls = mock_op.alter_column.call_args_list
    assert len(alter_calls) == 1, (
        f"upgrade() should run exactly one alter_column (the NOT NULL "
        f"promotion); got {len(alter_calls)}"
    )
    args, kwargs = alter_calls[0].args, alter_calls[0].kwargs
    assert args[0] == "subnets"
    assert args[1] == "id"
    assert kwargs.get("nullable") is False, (
        "alter_column must promote id to NOT NULL"
    )

    # Order: backfill UPDATE must run before alter_column.
    ordered = [
        c[0].split(".")[-1]
        for c in mock_op.mock_calls
        if c[0].split(".")[-1] in {"execute", "alter_column"}
    ]
    last_execute = max(
        i for i, name in enumerate(ordered) if name == "execute"
    )
    alter_index = ordered.index("alter_column")
    assert last_execute < alter_index, (
        f"backfill UPDATE must precede alter_column NOT NULL promotion; "
        f"order was: {ordered!r}"
    )


def test_upgrade_creates_unique_constraint(migration_module):
    """``id`` must carry a unique constraint so two rows cannot
    share the same opaque identifier — the (vanishingly unlikely)
    ``gen_random_uuid()`` collision would otherwise silently break
    every consumer that treats the column as a primary key."""
    with patch.object(migration_module, "op") as mock_op:
        migration_module.upgrade()

    uc_calls = mock_op.create_unique_constraint.call_args_list
    assert len(uc_calls) == 1, (
        f"upgrade() should create exactly one unique constraint; "
        f"got {len(uc_calls)}"
    )
    name, table, columns = uc_calls[0].args
    assert name == "uq_subnets_id"
    assert table == "subnets"
    assert columns == ["id"]


def test_downgrade_drops_constraint_before_column(migration_module):
    """``downgrade()`` must drop the unique constraint *before* the
    column. PostgreSQL refuses to drop a column a constraint still
    references, so reversing the order would leave the database in
    a half-downgraded state."""
    with patch.object(migration_module, "op") as mock_op:
        migration_module.downgrade()

    ordered = [c[0].split(".")[-1] for c in mock_op.mock_calls]
    assert ordered == ["drop_constraint", "drop_column"], (
        f"downgrade() must drop_constraint before drop_column; "
        f"actual order: {ordered!r}"
    )

    drop_constraint_calls = mock_op.drop_constraint.call_args_list
    assert len(drop_constraint_calls) == 1
    assert drop_constraint_calls[0].args[0] == "uq_subnets_id"
    assert drop_constraint_calls[0].args[1] == "subnets"

    drop_column_calls = mock_op.drop_column.call_args_list
    assert len(drop_column_calls) == 1
    assert drop_column_calls[0].args[0] == "subnets"
    assert drop_column_calls[0].args[1] == "id"


def test_downgrade_runs_no_data_mutations(migration_module):
    """Downgrade is structural-only. No ``execute()`` calls means
    no UPDATE / DELETE / TRUNCATE — the column drop already
    discards the UUIDs cleanly. Adding data mutations here would
    risk wiping unrelated rows on operator typos."""
    with patch.object(migration_module, "op") as mock_op:
        migration_module.downgrade()

    assert mock_op.execute.call_count == 0, (
        f"downgrade() should run no UPDATE / DELETE; "
        f"got {mock_op.execute.call_count} execute calls"
    )
