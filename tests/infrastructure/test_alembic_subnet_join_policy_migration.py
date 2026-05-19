"""Alembic migration ``f0a1b2c3d4e5_add_subnet_join_policy_field`` regressions.

The full DDL + backfill is exercised against a real PostgreSQL in
CI's ``alembic upgrade head`` job; this file pins the migration's
*structure* in unit-test land so contributors can catch obvious
mistakes (wrong revision chain, missing backfill, drop ordering)
without spinning up a database.

What we check
-------------
- ``revision`` / ``down_revision`` form a single-parent edge
  pointing at the ADR-0003 head (``e1f2a3b4c5d6``).
- ``upgrade()`` adds the ``join_policy`` column with the right
  default and then runs the ``is_private = true`` backfill — in
  that order, in the same transaction.
- ``downgrade()`` drops the column without touching the data
  underneath.
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
        / "f0a1b2c3d4e5_add_subnet_join_policy_field.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_migration_f0a1b2c3d4e5", migration_path
    )
    assert spec is not None and spec.loader is not None, (
        f"failed to build module spec for {migration_path}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_and_down_revision(migration_module):
    """Revision chain: this revision sits directly on top of the
    ADR-0003 head. Splitting onto a branch (or pointing at a stale
    ancestor) would make ``alembic upgrade head`` either skip this
    migration or refuse with a multiple-head error."""
    assert migration_module.revision == "f0a1b2c3d4e5"
    assert migration_module.down_revision == "e1f2a3b4c5d6"


def test_upgrade_adds_join_policy_column_with_server_default(migration_module):
    """``upgrade()`` must add a NOT NULL ``join_policy`` column with
    ``server_default='open'``. Without ``server_default`` the ALTER
    on a non-empty ``subnets`` table would fail (PG refuses NOT NULL
    columns without a default on populated tables)."""
    with patch.object(migration_module, "op") as mock_op:
        migration_module.upgrade()

    add_calls = mock_op.add_column.call_args_list
    assert len(add_calls) == 1, (
        f"upgrade() should add exactly one column, got "
        f"{len(add_calls)}: {[c.args[1].name for c in add_calls]!r}"
    )

    column = add_calls[0].args[1]
    assert column.name == "join_policy"
    assert column.nullable is False, "join_policy column must be NOT NULL"
    assert column.server_default is not None, (
        "join_policy must carry server_default='open' so ALTER on a "
        "non-empty table succeeds"
    )
    # ``server_default`` wraps the literal text in a DefaultClause-shaped
    # object. The ``.arg`` attribute holds the actual SQL literal.
    default_text = (
        column.server_default.arg.text
        if hasattr(column.server_default.arg, "text")
        else str(column.server_default.arg)
    )
    assert default_text == "open", (
        f"join_policy server_default mismatch: {default_text!r}"
    )


def test_upgrade_backfills_is_private_rows_to_approval(migration_module):
    """``upgrade()`` must run the
    ``UPDATE subnets SET join_policy='approval' WHERE is_private=true``
    backfill in the same transaction as the column add. Without the
    backfill, ``is_private=true`` rows would sit at the entity-
    invariant-violating ``private + open`` combination after the
    migration — exactly the gap ADR-0004 closes."""
    with patch.object(migration_module, "op") as mock_op:
        migration_module.upgrade()

    execute_calls = mock_op.execute.call_args_list
    assert len(execute_calls) == 1, (
        f"upgrade() should run exactly one execute (the backfill); "
        f"got {len(execute_calls)} calls"
    )

    # The migration wraps the SQL in ``sa.text(...)``; stringify to
    # match the literal.
    sql = str(execute_calls[0].args[0])
    assert "UPDATE subnets" in sql
    assert "SET join_policy = 'approval'" in sql
    assert "WHERE is_private = true" in sql


def test_upgrade_column_before_backfill(migration_module):
    """The backfill must run **after** the column exists. Reversing
    the order would attempt ``UPDATE`` on a column the schema hasn't
    declared yet and fail outright."""
    with patch.object(migration_module, "op") as mock_op:
        migration_module.upgrade()

    # ``mock_calls`` preserves global ordering across method names.
    ordered = [
        c[0].split(".")[-1]
        for c in mock_op.mock_calls
        if c[0].split(".")[-1] in {"add_column", "execute"}
    ]
    assert ordered == ["add_column", "execute"], (
        f"upgrade() must add_column before execute; actual order: {ordered!r}"
    )


def test_downgrade_drops_join_policy_column(migration_module):
    """``downgrade()`` must drop the column. The backfilled values
    vanish with it — there is no reverse backfill (we can't recover
    the original ``open`` vs ``approval`` distribution from the
    ``is_private`` column alone, but that's acceptable: downgrading
    re-exposes the historical bug, which the operator is
    knowingly accepting by running ``alembic downgrade``)."""
    with patch.object(migration_module, "op") as mock_op:
        migration_module.downgrade()

    drop_calls = mock_op.drop_column.call_args_list
    assert len(drop_calls) == 1, (
        f"downgrade() should drop exactly one column; got "
        f"{len(drop_calls)} calls"
    )
    assert drop_calls[0].args[1] == "join_policy"

    # No execute() calls — downgrade is structural, not data-mutating.
    assert mock_op.execute.call_count == 0, (
        f"downgrade() should run no UPDATE / DELETE; "
        f"got {mock_op.execute.call_count} execute calls"
    )
