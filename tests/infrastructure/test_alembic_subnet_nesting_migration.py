"""Alembic migration ``e1f2a3b4c5d6_add_subnet_nesting_fields`` regressions.

The full DDL is exercised against a real PostgreSQL in CI's
``alembic upgrade head`` job; this file pins the migration's
*structure* in unit-test land so contributors can catch obvious
mistakes (missing column, missing index, drop ordering) without
spinning up a database.

What we check
-------------
- ``revision`` / ``down_revision`` form a single-parent edge
  pointing at the previous head (``d0e1f2a3b4c5``).
- ``upgrade()`` adds the three columns *and* both partial indexes.
- ``downgrade()`` removes them in reverse order — indexes first
  (PostgreSQL refuses to drop a column an index still references)
  and ``parent_slug`` last (mirrors the ``upgrade`` order).
- The partial-index ``postgresql_where`` clauses target the right
  column (``parent_slug IS NOT NULL`` /
  ``linked_task_id IS NOT NULL``).
"""

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def migration_module():
    """Load the migration file as an isolated module.

    Alembic's ``versions/`` directory is not a Python package (no
    ``__init__.py``) — ``alembic`` loads each revision dynamically
    via ``ScriptDirectory``. Tests have to mirror that pattern via
    ``importlib.util.spec_from_file_location`` rather than importing
    by dotted name.
    """
    repo_root = Path(__file__).resolve().parents[2]
    migration_path = (
        repo_root
        / "alembic"
        / "versions"
        / "e1f2a3b4c5d6_add_subnet_nesting_fields.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_migration_e1f2a3b4c5d6", migration_path
    )
    assert spec is not None and spec.loader is not None, (
        f"failed to build module spec for {migration_path}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_and_down_revision(migration_module):
    assert migration_module.revision == "e1f2a3b4c5d6"
    assert migration_module.down_revision == "d0e1f2a3b4c5"


def test_upgrade_adds_three_columns_and_two_indexes(migration_module):
    """``upgrade()`` must add exactly the three nesting columns and
    create the two partial indexes — and nothing else (no spurious
    DDL slipping in)."""
    with (
        patch.object(migration_module, "op") as mock_op,
    ):
        migration_module.upgrade()

    column_calls = mock_op.add_column.call_args_list
    column_names = [
        call.args[1].name for call in column_calls
    ]
    assert column_names == [
        "parent_slug",
        "lifecycle",
        "linked_task_id",
    ], f"unexpected upgrade() column order: {column_names!r}"

    index_calls = mock_op.create_index.call_args_list
    index_names = [call.args[0] for call in index_calls]
    assert index_names == [
        "subnets_parent_idx",
        "subnets_linked_task_idx",
    ], f"unexpected upgrade() index order: {index_names!r}"


def test_upgrade_lifecycle_column_carries_server_default(migration_module):
    """``lifecycle`` ALTER must use ``server_default='persistent'`` so
    existing rows backfill in one statement — otherwise a NOT NULL
    column would fail to ALTER on a non-empty table."""
    with patch.object(migration_module, "op") as mock_op:
        migration_module.upgrade()

    lifecycle_call = next(
        c
        for c in mock_op.add_column.call_args_list
        if c.args[1].name == "lifecycle"
    )
    column = lifecycle_call.args[1]
    assert column.nullable is False, "lifecycle should be NOT NULL"
    # server_default is wrapped in sa.DefaultClause-shaped object — its
    # ``.arg`` attribute holds the literal text.
    assert column.server_default is not None
    default_text = (
        column.server_default.arg.text
        if hasattr(column.server_default.arg, "text")
        else str(column.server_default.arg)
    )
    assert default_text == "persistent", (
        f"lifecycle server_default mismatch: {default_text!r}"
    )


def test_upgrade_indexes_use_partial_where_on_correct_columns(migration_module):
    with patch.object(migration_module, "op") as mock_op:
        migration_module.upgrade()

    parent_idx_call = next(
        c
        for c in mock_op.create_index.call_args_list
        if c.args[0] == "subnets_parent_idx"
    )
    where_clause = parent_idx_call.kwargs["postgresql_where"]
    assert "parent_slug IS NOT NULL" in str(where_clause), (
        f"subnets_parent_idx WHERE clause wrong: {where_clause}"
    )

    linked_task_idx_call = next(
        c
        for c in mock_op.create_index.call_args_list
        if c.args[0] == "subnets_linked_task_idx"
    )
    where_clause = linked_task_idx_call.kwargs["postgresql_where"]
    assert "linked_task_id IS NOT NULL" in str(where_clause), (
        f"subnets_linked_task_idx WHERE clause wrong: {where_clause}"
    )


def test_downgrade_drops_indexes_before_columns(migration_module):
    """PostgreSQL refuses to drop a column that still has an index
    on it. ``downgrade()`` must therefore call ``drop_index`` for
    both nesting indexes *before* any ``drop_column``."""
    with patch.object(migration_module, "op") as mock_op:
        migration_module.downgrade()

    # Reconstruct the call order across drop_index / drop_column
    # so we can assert "all drop_index calls come before any
    # drop_column call".
    call_log = []
    for call in mock_op.drop_index.call_args_list:
        call_log.append(("drop_index", call.args[0]))
    for call in mock_op.drop_column.call_args_list:
        call_log.append(("drop_column", call.args[1]))
    # ``mock_calls`` preserves global ordering across both methods;
    # use it instead of the per-method lists above for the actual
    # ordering check.
    ordered = [
        (c[0].split(".")[-1], c[1][1] if c[0].split(".")[-1] == "drop_column" else c[1][0])
        for c in mock_op.mock_calls
        if c[0].split(".")[-1] in {"drop_index", "drop_column"}
    ]
    drop_index_idxs = [i for i, (verb, _) in enumerate(ordered) if verb == "drop_index"]
    drop_column_idxs = [i for i, (verb, _) in enumerate(ordered) if verb == "drop_column"]
    assert drop_index_idxs and drop_column_idxs, (
        f"missing drop_index or drop_column calls: {ordered!r}"
    )
    assert max(drop_index_idxs) < min(drop_column_idxs), (
        f"drop_index must precede drop_column; actual order: {ordered!r}"
    )


def test_downgrade_drops_columns_in_reverse_of_upgrade(migration_module):
    """Symmetric reversal of ``upgrade()`` — the last column added
    is the first dropped. Keeps the migration cleanly reversible
    in the rare case someone bisects through it."""
    with patch.object(migration_module, "op") as mock_op:
        migration_module.downgrade()

    column_calls = mock_op.drop_column.call_args_list
    column_names = [call.args[1] for call in column_calls]
    assert column_names == [
        "linked_task_id",
        "lifecycle",
        "parent_slug",
    ], f"unexpected downgrade() column order: {column_names!r}"
