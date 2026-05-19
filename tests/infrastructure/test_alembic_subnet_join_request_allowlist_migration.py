"""Alembic migration ``a3b5c7d9e1f2_add_subnet_join_request_and_allowlist_tables`` regressions.

Pins the structural contract of the Phase 2 Slice 2.1 migration
without spinning up a real PostgreSQL — full DDL is exercised in
CI's ``alembic upgrade head`` job. The checks here catch the
common single-file mistakes (broken revision chain, missing
critical index, wrong drop ordering) that would silently corrupt
the chain or skip a defence-in-depth invariant.

What we check
-------------
- Revision chain points at the Phase 1 head (``f7b9c2d4e8a1``).
- ``upgrade()`` creates both tables and all four indexes.
- The unique partial index on ``(subnet_id, agent_id) WHERE
  status='pending'`` is present (THE invariant of the table; a
  missing one silently re-opens the two-pending race).
- ``downgrade()`` drops indexes before their tables (good DDL
  hygiene; also pins the symmetric reverse-order contract).
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
    that with ``importlib.util.spec_from_file_location``.
    """
    repo_root = Path(__file__).resolve().parents[2]
    migration_path = (
        repo_root
        / "alembic"
        / "versions"
        / "a3b5c7d9e1f2_add_subnet_join_request_and_allowlist_tables.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_migration_a3b5c7d9e1f2", migration_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain_points_at_phase_1_head(migration_module):
    """This migration sits directly on top of the ``drop_agents_status``
    revision (the current main HEAD as of Phase 2 Slice 2.1 start).
    Splitting onto a branch or pointing at a stale ancestor would
    create a multiple-head error or skip the migration entirely."""
    assert migration_module.revision == "a3b5c7d9e1f2"
    assert migration_module.down_revision == "f7b9c2d4e8a1"


# ---------------------------------------------------------------------------
# upgrade() — create_table + create_index calls
# ---------------------------------------------------------------------------


class TestUpgrade:
    def test_creates_both_tables(self, migration_module):
        with (
            patch.object(migration_module.op, "create_table") as mock_create,
            patch.object(migration_module.op, "create_index"),
        ):
            migration_module.upgrade()

        table_names = [c.args[0] for c in mock_create.call_args_list]
        assert "subnet_join_requests" in table_names
        assert "subnet_allowlist" in table_names

    def test_creates_unique_partial_pending_index(self, migration_module):
        """THE invariant. If a future "simplification" drops this
        unique partial index, the two-pending race re-opens silently
        — every concurrent self-join + invitation pair could end up
        with two ``status='pending'`` rows that both transition to
        ``approved``, materialising a duplicate membership."""
        with (
            patch.object(migration_module.op, "create_table"),
            patch.object(migration_module.op, "create_index") as mock_idx,
        ):
            migration_module.upgrade()

        pending_unique_calls = [
            c for c in mock_idx.call_args_list
            if c.args[0] == "subnet_join_requests_pending_unique"
        ]
        assert len(pending_unique_calls) == 1, (
            "missing the (subnet_id, agent_id) WHERE status='pending' "
            "unique partial index"
        )
        call_args = pending_unique_calls[0]
        assert call_args.args[1] == "subnet_join_requests"
        assert call_args.args[2] == ["subnet_id", "agent_id"]
        assert call_args.kwargs.get("unique") is True
        # The ``postgresql_where`` clause is the partial-index predicate;
        # SQLAlchemy ``TextClause`` doesn't compare equal cross-instance,
        # so we compare its compiled string.
        where_clause = call_args.kwargs.get("postgresql_where")
        assert where_clause is not None
        assert "status = 'pending'" in str(where_clause)

    def test_creates_agent_pending_invitations_partial_index(
        self, migration_module
    ):
        """Partial index sized proportional to in-flight invitations
        — drops it and the invitee dashboard scans the full audit
        log on every page-load."""
        with (
            patch.object(migration_module.op, "create_table"),
            patch.object(migration_module.op, "create_index") as mock_idx,
        ):
            migration_module.upgrade()

        names = [c.args[0] for c in mock_idx.call_args_list]
        assert "ix_subnet_join_requests_agent_pending_invitations" in names

    def test_creates_allowlist_reverse_lookup_index(self, migration_module):
        with (
            patch.object(migration_module.op, "create_table"),
            patch.object(migration_module.op, "create_index") as mock_idx,
        ):
            migration_module.upgrade()

        names = [c.args[0] for c in mock_idx.call_args_list]
        assert "ix_subnet_allowlist_agent_id" in names


# ---------------------------------------------------------------------------
# downgrade() — drop_index BEFORE drop_table, in reverse order
# ---------------------------------------------------------------------------


class TestDowngrade:
    def test_drops_indexes_before_their_tables(self, migration_module):
        """``DROP TABLE`` cascades indexes implicitly, but the
        migration spells out ``drop_index`` first as documentation
        + as a guard against any future code that depends on the
        explicit order (e.g. an audit trigger that watches index
        drops). Pin the ordering so a "refactor" can't reverse it."""
        operations: list[tuple[str, str]] = []

        def record_drop_index(name, table_name):
            operations.append(("drop_index", name))

        def record_drop_table(name):
            operations.append(("drop_table", name))

        with (
            patch.object(
                migration_module.op,
                "drop_index",
                side_effect=record_drop_index,
            ),
            patch.object(
                migration_module.op,
                "drop_table",
                side_effect=record_drop_table,
            ),
        ):
            migration_module.downgrade()

        # subnet_allowlist: drop its index, then the table.
        idx_a = operations.index(("drop_index", "ix_subnet_allowlist_agent_id"))
        tbl_a = operations.index(("drop_table", "subnet_allowlist"))
        assert idx_a < tbl_a

        # subnet_join_requests: drop all three indexes, then the table.
        tbl_jr = operations.index(("drop_table", "subnet_join_requests"))
        for idx_name in [
            "ix_subnet_join_requests_agent_pending_invitations",
            "ix_subnet_join_requests_subnet_id",
            "subnet_join_requests_pending_unique",
        ]:
            idx_pos = operations.index(("drop_index", idx_name))
            assert idx_pos < tbl_jr, (
                f"{idx_name} must be dropped before its table"
            )

    def test_downgrade_drops_both_tables(self, migration_module):
        with (
            patch.object(migration_module.op, "drop_index"),
            patch.object(migration_module.op, "drop_table") as mock_drop,
        ):
            migration_module.downgrade()

        names = [c.args[0] for c in mock_drop.call_args_list]
        assert "subnet_allowlist" in names
        assert "subnet_join_requests" in names
