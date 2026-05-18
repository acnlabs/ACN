"""Alembic migration ``f7b9c2d4e8a1_drop_agents_status_column`` regressions.

CI runs the migration against a real PostgreSQL via
``alembic upgrade head``; this file pins the migration's *structure*
in unit-test land so contributors catch the easy mistakes (wrong
parent, wrong drop order, accidental DROP TYPE) without spinning up
a database.

What we check
-------------
- ``revision`` / ``down_revision`` form a single-parent edge pointing
  at the previous head (``e1f2a3b4c5d6``).
- ``upgrade()`` drops the partial index *before* the column. PostgreSQL
  refuses to drop a column that still has an index referencing it.
- ``upgrade()`` uses ``IF EXISTS`` for both DROP statements so a
  partial / replayed run cannot abort on a missing object.
- ``upgrade()`` does NOT emit ``DROP TYPE`` — the column was plain
  ``String(32)`` rather than a PostgreSQL ENUM, so there is no
  ``agentstatus`` TYPE to drop. (Forgetting this was a real
  pre-flight bug in the planning phase; the test pins it down so
  the next refactor can't re-introduce it.)
- ``downgrade()`` re-creates the column with the original NOT NULL
  + ``server_default='online'`` shape and re-creates the partial
  index, so the migration is cleanly reversible.
"""

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def migration_module():
    repo_root = Path(__file__).resolve().parents[2]
    migration_path = (
        repo_root
        / "alembic"
        / "versions"
        / "f7b9c2d4e8a1_drop_agents_status_column.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_migration_f7b9c2d4e8a1", migration_path
    )
    assert spec is not None and spec.loader is not None, (
        f"failed to build module spec for {migration_path}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_and_down_revision(migration_module):
    assert migration_module.revision == "f7b9c2d4e8a1"
    assert migration_module.down_revision == "e1f2a3b4c5d6", (
        "must extend the current single head; a divergent parent makes "
        "``alembic upgrade head`` refuse to run (see SCALE_AUDIT.md note "
        "on the previous fork incident)."
    )


def test_upgrade_drops_index_before_column(migration_module):
    """``upgrade()`` issues both DROP statements via ``op.execute``;
    the *order* must put the index drop first so Postgres doesn't
    refuse to drop the column it still references."""
    with patch.object(migration_module, "op") as mock_op:
        migration_module.upgrade()

    execute_calls = [c.args[0] for c in mock_op.execute.call_args_list]
    assert len(execute_calls) == 2, (
        f"expected exactly two execute() calls (DROP INDEX + DROP COLUMN), "
        f"got: {execute_calls!r}"
    )
    assert "DROP INDEX" in execute_calls[0].upper()
    assert "ix_agents_status_online_agent_id" in execute_calls[0]
    assert "DROP COLUMN" in execute_calls[1].upper()
    assert "status" in execute_calls[1]


def test_upgrade_drops_use_if_exists(migration_module):
    """Idempotency guard: a half-applied or hand-poked database
    must not abort the upgrade. Mirrors the pattern from
    ``7ee2ed3a177c`` (the first migration that hit a 'object does
    not exist' replay panic)."""
    with patch.object(migration_module, "op") as mock_op:
        migration_module.upgrade()

    execute_calls = [c.args[0].upper() for c in mock_op.execute.call_args_list]
    assert all("IF EXISTS" in stmt for stmt in execute_calls), (
        f"every DROP statement must be IF EXISTS for replay safety; "
        f"got: {execute_calls!r}"
    )


def test_upgrade_does_not_drop_an_enum_type(migration_module):
    """``agents.status`` is ``String(32)``, not a PostgreSQL ENUM —
    there is no ``agentstatus`` TYPE to drop and emitting ``DROP TYPE``
    would crash with ``type ... does not exist`` on every fresh
    deploy. The pre-flight plan had this wrong; pinning it here
    keeps the next refactor honest."""
    with patch.object(migration_module, "op") as mock_op:
        migration_module.upgrade()

    execute_calls = [c.args[0].upper() for c in mock_op.execute.call_args_list]
    assert not any("DROP TYPE" in stmt for stmt in execute_calls), (
        f"unexpected DROP TYPE in upgrade(): {execute_calls!r}"
    )


def test_downgrade_recreates_column_and_index(migration_module):
    """``downgrade()`` must restore *both* the column and the partial
    index so a forward-then-back round-trip lands on schema
    equivalent to before the migration."""
    with patch.object(migration_module, "op") as mock_op:
        migration_module.downgrade()

    add_column_calls = mock_op.add_column.call_args_list
    assert len(add_column_calls) == 1, (
        f"downgrade must re-add exactly one column; got: {add_column_calls!r}"
    )
    table_name, column = add_column_calls[0].args
    assert table_name == "agents"
    assert column.name == "status"
    assert column.nullable is False, (
        "the original column was NOT NULL; downgrade must restore that "
        "shape or subsequent migrations that depend on the constraint break"
    )
    # ``server_default`` is required so the ADD COLUMN succeeds against
    # a non-empty agents table.
    assert column.server_default is not None
    default_text = (
        column.server_default.arg.text
        if hasattr(column.server_default.arg, "text")
        else str(column.server_default.arg)
    )
    assert "online" in default_text, (
        f"server_default should match the original 'online' literal; "
        f"got: {default_text!r}"
    )

    index_calls = mock_op.create_index.call_args_list
    assert len(index_calls) == 1
    assert index_calls[0].args[0] == "ix_agents_status_online_agent_id"
