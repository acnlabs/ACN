"""Tests for PostgresAllowlistRepository capacity-trigger handling.

Phase 2 PR #2 v3 review P1-A1 fix: the database-side
``trg_agent_allowlist_capacity`` trigger ``RAISE``s SQLSTATE 23514
(check_violation) when the per-owner advisory lock confirms the
allowlist is full. The Postgres repo layer must translate that raw
``sqlalchemy.exc.IntegrityError`` into the domain
``AllowlistCapacityExceededError`` so the service / route layers see
exactly one exception type regardless of where the cap was enforced
(service-layer pre-check or trigger).

These tests exercise the repo in isolation with a mocked session
factory; the integration-level behaviour (advisory lock + count
+ RAISE chain inside Postgres) is covered by the migration's SQL —
which Alembic exercises against the real DB in CI.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from acn.core.exceptions import AllowlistCapacityExceededError
from acn.infrastructure.persistence.postgres.allowlist_repository import (
    PostgresAllowlistRepository,
)


def _session_yielding(execute_side_effect):
    """Build an async-context-manager session whose ``execute`` triggers ``side_effect``."""
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    session.execute.side_effect = execute_side_effect
    return session


def _make_pgcode_exception(pgcode: str) -> IntegrityError:
    """Construct an IntegrityError mimicking psycopg / asyncpg's pgcode shape."""
    orig = MagicMock()
    orig.pgcode = pgcode
    orig.sqlstate = pgcode
    return IntegrityError(statement="INSERT ...", params={}, orig=orig)


@pytest.mark.asyncio
async def test_add_translates_check_violation_to_capacity_error():
    """Trigger fires ``RAISE EXCEPTION ... USING ERRCODE='check_violation'``;
    repo must surface ``AllowlistCapacityExceededError`` to the service layer.
    """

    async def _execute(*_args, **_kwargs):
        raise _make_pgcode_exception("23514")

    session = _session_yielding(_execute)
    factory = MagicMock(return_value=session)
    repo = PostgresAllowlistRepository(session_factory=factory)

    with pytest.raises(AllowlistCapacityExceededError):
        await repo.add(owner_id="owner-1", target_id="alice")

    # Defensive check: the repo must roll back so the failed INSERT
    # doesn't hold a transaction open.
    session.rollback.assert_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_propagates_other_integrity_errors_unchanged():
    """A FK violation (e.g. owner_id deleted concurrently) must NOT be
    swallowed as a capacity error — the route layer would silently
    return 429 instead of the correct 5xx for an unrelated DB issue.
    """

    async def _execute(*_args, **_kwargs):
        # 23503 = foreign_key_violation
        raise _make_pgcode_exception("23503")

    session = _session_yielding(_execute)
    factory = MagicMock(return_value=session)
    repo = PostgresAllowlistRepository(session_factory=factory)

    with pytest.raises(IntegrityError):
        await repo.add(owner_id="owner-1", target_id="alice")
