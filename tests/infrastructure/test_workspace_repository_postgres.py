"""PostgresWorkspaceRepository — unique-slot conflict mapping (D15).

``uq_exec_workspaces_active_task`` / ``uq_exec_workspaces_active_org``
(alembic ``c0d1e2f3a4b5``) enforce one active workspace per task / org;
``save_workspace`` must translate that IntegrityError into the domain
``WorkspaceAlreadyActiveError`` so a create that loses the pre-check
race surfaces as a 409, not a bare 500.

Mock-session style mirrors ``test_org_repository_postgres.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from acn.core.entities.workspace import Workspace
from acn.core.exceptions import WorkspaceAlreadyActiveError
from acn.infrastructure.persistence.postgres.workspace_repository import (
    PostgresWorkspaceRepository,
)

pytestmark = pytest.mark.asyncio


def _make_session_factory():
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    factory = MagicMock(return_value=session)
    return factory, session


def _ws(*, admit: str = "task", task_id: str | None = "task-1", org_id: str | None = None) -> Workspace:
    return Workspace(
        workspace_id="ws_loser",
        owner_agent_id="agt_owner",
        display_name="Loser",
        execution_env={"kind": "git", "uri": "https://github.com/acme/s.git"},
        admit=admit,  # type: ignore[arg-type]
        org_id=org_id,
        task_id=task_id,
        status="active",
        created_at=datetime(2026, 8, 28, tzinfo=UTC),
        updated_at=datetime(2026, 8, 28, tzinfo=UTC),
    )


def _integrity_error(message: str) -> IntegrityError:
    return IntegrityError("INSERT INTO exec_workspaces ...", {}, Exception(message))


async def test_unique_task_violation_maps_to_domain_conflict():
    factory, session = _make_session_factory()
    session.get.return_value = None
    session.commit.side_effect = _integrity_error(
        'duplicate key value violates unique constraint "uq_exec_workspaces_active_task"'
    )
    holder_result = MagicMock()
    holder_result.scalar_one_or_none.return_value = "ws_winner"
    session.execute.return_value = holder_result

    repo = PostgresWorkspaceRepository(factory)
    with pytest.raises(WorkspaceAlreadyActiveError) as ei:
        await repo.save_workspace(_ws())

    assert ei.value.bind_kind == "task"
    assert ei.value.bind_id == "task-1"
    assert ei.value.existing_workspace_id == "ws_winner"
    session.rollback.assert_awaited_once()


async def test_unique_org_violation_maps_to_domain_conflict():
    factory, session = _make_session_factory()
    session.get.return_value = None
    session.commit.side_effect = _integrity_error(
        'duplicate key value violates unique constraint "uq_exec_workspaces_active_org"'
    )
    holder_result = MagicMock()
    holder_result.scalar_one_or_none.return_value = "ws_winner"
    session.execute.return_value = holder_result

    repo = PostgresWorkspaceRepository(factory)
    with pytest.raises(WorkspaceAlreadyActiveError) as ei:
        await repo.save_workspace(_ws(admit="org", org_id="org_1", task_id=None))

    assert ei.value.bind_kind == "org"
    assert ei.value.bind_id == "org_1"
    assert ei.value.existing_workspace_id == "ws_winner"


async def test_unrelated_integrity_error_is_not_swallowed():
    factory, session = _make_session_factory()
    session.get.return_value = None
    session.commit.side_effect = _integrity_error(
        'null value in column "display_name" violates not-null constraint'
    )

    repo = PostgresWorkspaceRepository(factory)
    with pytest.raises(IntegrityError):
        await repo.save_workspace(_ws())
