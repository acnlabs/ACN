"""PostgresSubnetRepository — ADR-0003 nesting field regressions.

Three contracts pinned here:

1. ``_subnet_to_model`` / ``_model_to_subnet`` round-trip the three
   nesting fields without dropping or mangling them.
2. ``save()`` 's UPDATE branch carries the new columns — otherwise
   ``promote_to_persistent`` (Phase 2) would silently no-op on rows
   already in the DB.
3. ``find_by_parent`` / ``find_by_linked_task`` filter on the right
   column. We don't pin the exact SQL string (SQLAlchemy may
   rephrase between releases), only the column reference inside
   the WHERE clause.

The repository is exercised against an in-memory mock session — no
PostgreSQL needed. Schema correctness lives in the Alembic upgrade
test (``test_alembic_subnet_nesting_migration.py``).
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.sql import Select, Update

from acn.core.entities import Subnet
from acn.infrastructure.persistence.postgres.models import SubnetModel
from acn.infrastructure.persistence.postgres.subnet_repository import (
    PostgresSubnetRepository,
)


def _make_session_factory(execute_results: list | None = None):
    """Build a mock ``async_sessionmaker`` that yields a recorded session.

    ``execute_results`` lets a caller queue scripted return values for
    ``session.execute(...)`` — used by ``find_by_parent`` tests that
    need ``.scalars().all()`` to return a list.
    """
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None

    if execute_results is not None:
        session.execute.side_effect = execute_results

    factory = MagicMock(return_value=session)
    return factory, session


# ---------------------------------------------------------------------------
# 1. Mapper round-trip — entity ↔ model
# ---------------------------------------------------------------------------


def test_subnet_to_model_carries_nesting_fields():
    factory, _ = _make_session_factory()
    repo = PostgresSubnetRepository(session_factory=factory)
    subnet = Subnet(
        slug="subnet-child",
        name="child",
        owner="agent-owner",
        parent_slug="subnet-parent",
        lifecycle="task_scoped",
        linked_task_id="task-xyz",
    )

    model = repo._subnet_to_model(subnet)

    assert model.parent_slug == "subnet-parent"
    assert model.lifecycle == "task_scoped"
    assert model.linked_task_id == "task-xyz"


def test_model_to_subnet_carries_nesting_fields():
    factory, _ = _make_session_factory()
    repo = PostgresSubnetRepository(session_factory=factory)
    model = SubnetModel(
        slug="subnet-child",
        name="child",
        owner="agent-owner",
        description=None,
        is_private=False,
        security_config=None,
        member_agent_ids=None,
        subnet_metadata=None,
        harness_url=None,
        harness_secret=None,
        parent_slug="subnet-parent",
        lifecycle="task_scoped",
        linked_task_id="task-xyz",
        created_at=datetime.now(UTC),
    )

    subnet = repo._model_to_subnet(model)

    assert subnet.parent_slug == "subnet-parent"
    assert subnet.lifecycle == "task_scoped"
    assert subnet.linked_task_id == "task-xyz"


def test_model_to_subnet_defaults_for_legacy_row():
    """Existing rows pre-ADR-0003 carry the new columns as their DB
    defaults (``parent_slug=NULL``, ``lifecycle='persistent'``,
    ``linked_task_id=NULL``) — the mapper must surface them as
    "top-level persistent" semantics, not raise on missing fields."""
    factory, _ = _make_session_factory()
    repo = PostgresSubnetRepository(session_factory=factory)
    model = SubnetModel(
        slug="subnet-legacy",
        name="legacy",
        owner="agent-owner",
        description=None,
        is_private=False,
        security_config=None,
        member_agent_ids=None,
        subnet_metadata=None,
        harness_url=None,
        harness_secret=None,
        parent_slug=None,
        lifecycle="persistent",
        linked_task_id=None,
        created_at=datetime.now(UTC),
    )

    subnet = repo._model_to_subnet(model)

    assert subnet.parent_slug is None
    assert subnet.lifecycle == "persistent"
    assert subnet.linked_task_id is None


# ---------------------------------------------------------------------------
# 2. save() UPDATE branch — new columns are carried
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_update_path_includes_nesting_columns():
    """When the row already exists, ``save()`` issues an UPDATE. The
    set of columns mutated must include the three nesting fields so
    Phase 2's ``promote_to_persistent`` (which only flips
    ``lifecycle`` / ``linked_task_id``) actually persists through
    SQLAlchemy."""

    # First call is ``session.get()`` for the existence check — return
    # a sentinel model so the UPDATE branch fires.
    existing_model = MagicMock(spec=SubnetModel)
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    session.get.return_value = existing_model

    factory = MagicMock(return_value=session)
    repo = PostgresSubnetRepository(session_factory=factory)

    subnet = Subnet(
        slug="subnet-rt",
        name="rt",
        owner="agent-owner",
        parent_slug="subnet-parent",
        lifecycle="task_scoped",
        linked_task_id="task-xyz",
    )

    await repo.save(subnet)

    session.execute.assert_awaited_once()
    stmt = session.execute.await_args_list[0].args[0]
    assert isinstance(stmt, Update)
    # ``Update`` stores SET targets in ``_values`` (Column → BindParameter)
    set_columns = {col.name for col in stmt._values.keys()}  # type: ignore[attr-defined]
    assert "parent_slug" in set_columns
    assert "lifecycle" in set_columns
    assert "linked_task_id" in set_columns


# ---------------------------------------------------------------------------
# 3. find_by_parent / find_by_linked_task — column targeting
# ---------------------------------------------------------------------------


def _stmt_column_names(stmt: Select) -> set[str]:
    """Walk the WHERE clause of a SELECT and collect the column
    names it references. Robust against SQLAlchemy expression-tree
    rephrasing between releases."""
    names: set[str] = set()
    where_clause = stmt.whereclause
    if where_clause is None:
        return names

    def _visit(node):
        # Column / ColumnClause both expose ``.name``.
        if hasattr(node, "name") and not hasattr(node, "left"):
            names.add(node.name)
        for child in getattr(node, "get_children", lambda: [])():
            _visit(child)

    _visit(where_clause)
    return names


@pytest.mark.asyncio
async def test_find_by_parent_filters_on_parent_subnet_id_column():
    select_result = MagicMock()
    select_result.scalars.return_value.all.return_value = []
    factory, session = _make_session_factory(execute_results=[select_result])
    repo = PostgresSubnetRepository(session_factory=factory)

    rows = await repo.find_by_parent("subnet-parent")

    assert rows == []
    session.execute.assert_awaited_once()
    stmt = session.execute.await_args_list[0].args[0]
    assert isinstance(stmt, Select)
    assert "parent_slug" in _stmt_column_names(stmt), (
        f"WHERE clause did not target parent_slug; got {_stmt_column_names(stmt)!r}"
    )


@pytest.mark.asyncio
async def test_find_by_linked_task_filters_on_linked_task_id_column():
    select_result = MagicMock()
    select_result.scalars.return_value.all.return_value = []
    factory, session = _make_session_factory(execute_results=[select_result])
    repo = PostgresSubnetRepository(session_factory=factory)

    rows = await repo.find_by_linked_task("task-xyz")

    assert rows == []
    session.execute.assert_awaited_once()
    stmt = session.execute.await_args_list[0].args[0]
    assert isinstance(stmt, Select)
    assert "linked_task_id" in _stmt_column_names(stmt), (
        f"WHERE clause did not target linked_task_id; got {_stmt_column_names(stmt)!r}"
    )
