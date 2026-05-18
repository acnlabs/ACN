"""Real-PostgreSQL integration tests for PostgresSubnetRepository.delete_with_children.

The mock-based tests in
``tests/infrastructure/test_postgres_subnet_repository_cascade.py``
verify that the implementation *calls* ``session.begin()`` and that
``__aexit__`` is invoked with the exception on a mid-cascade failure —
i.e. the *idiom* is correct.  These tests verify the *end-to-end
behaviour*: that SQLAlchemy + asyncpg + real Postgres actually roll back
when the context manager receives an exception, and that a successful
cascade commits atomically.

**Gating**: tests are skipped silently unless
``ACN_INTEGRATION_PG_URL`` is set to an async-capable DSN, e.g.::

    postgresql+asyncpg://acn:acn@localhost:5432/acn_test

See ``docs/integration-testing.md`` for instructions on how to spin up
a local Postgres instance suitable for this suite.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from acn.infrastructure.persistence.postgres.models import SubnetModel
from acn.infrastructure.persistence.postgres.subnet_repository import (
    PostgresSubnetRepository,
)

# ---------------------------------------------------------------------------
# Module-level marker — skip silently when no real PG is available
# ---------------------------------------------------------------------------
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ACN_INTEGRATION_PG_URL"),
        reason="needs ACN_INTEGRATION_PG_URL pointing at a disposable async PG",
    ),
]


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def pg(request: Any):
    """Create engine + session factory, (re)create the subnets table.

    The table is DROPPED before each test and re-created fresh so tests
    are fully isolated without requiring a transaction rollback at the
    fixture level (which could mask the very rollback behaviour under
    test).
    """
    url = os.environ["ACN_INTEGRATION_PG_URL"]
    engine = create_async_engine(url, future=True, pool_pre_ping=True)

    async with engine.begin() as conn:
        # Drop dependent indexes first to avoid DDL errors on re-create.
        await conn.execute(
            text("DROP INDEX IF EXISTS subnets_parent_idx")
        )
        await conn.execute(
            text("DROP INDEX IF EXISTS subnets_linked_task_idx")
        )
        await conn.run_sync(
            lambda c: SubnetModel.__table__.drop(c, checkfirst=True)
        )
        await conn.run_sync(
            lambda c: SubnetModel.__table__.create(c, checkfirst=True)
        )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _row(
    subnet_id: str,
    parent_id: str | None = None,
) -> SubnetModel:
    """Build a minimal SubnetModel row suitable for direct INSERT."""
    return SubnetModel(
        subnet_id=subnet_id,
        name=f"subnet-{subnet_id}",
        owner="test-owner",
        description=None,
        is_private=False,
        security_config=None,
        member_agent_ids=None,
        subnet_metadata=None,
        harness_url=None,
        harness_secret=None,
        parent_subnet_id=parent_id,
        lifecycle="persistent",
        linked_task_id=None,
        created_at=datetime.now(UTC),
    )


async def _exists(factory: async_sessionmaker, subnet_id: str) -> bool:
    """Return True if a subnet row with *subnet_id* is present."""
    async with factory() as session:
        result = await session.execute(
            select(SubnetModel).where(SubnetModel.subnet_id == subnet_id)
        )
        return result.scalar_one_or_none() is not None


# =============================================================================
# 1. Positive path — full cascade commits atomically
# =============================================================================


@pytest.mark.asyncio
async def test_delete_with_children_happy_path(pg: async_sessionmaker) -> None:
    """All rows (parent + children) are removed in a single committed transaction.

    Verifies end-to-end:
    - Rows are inserted through a real PG session.
    - ``delete_with_children`` returns ``True``.
    - Every row (parent and all 3 children) is gone after the call.
    """
    parent_id = "pg-parent-happy"
    child_ids = ["pg-child-1", "pg-child-2", "pg-child-3"]

    async with pg() as session:
        async with session.begin():
            session.add(_row(parent_id))
            for cid in child_ids:
                session.add(_row(cid, parent_id=parent_id))

    repo = PostgresSubnetRepository(session_factory=pg)
    result = await repo.delete_with_children(parent_id, child_ids)

    assert result is True, "delete_with_children should return True when parent existed"

    assert not await _exists(pg, parent_id), "parent must be gone"
    for cid in child_ids:
        assert not await _exists(pg, cid), f"child {cid} must be gone"


# =============================================================================
# 2. Rollback path — mid-cascade execute raise preserves all rows
# =============================================================================


@pytest.mark.asyncio
async def test_delete_with_children_rollback_on_mid_cascade_failure(
    pg: async_sessionmaker,
) -> None:
    """A simulated error on the second DELETE causes a full ROLLBACK.

    This is the key regression guard: if SQLAlchemy's ``session.begin()``
    ever stopped calling ``ROLLBACK`` on a mid-block exception, the
    parent and any already-deleted children would be gone — violating the
    atomicity guarantee documented in ADR-0003 §A.4.

    Strategy: monkey-patch ``session.execute`` so the *second* call
    (i.e. after the first child DELETE succeeds) raises ``RuntimeError``.
    We patch at the ``async_sessionmaker`` level by wrapping the factory
    to inject the patched session.
    """
    parent_id = "pg-parent-rollback"
    child_ids = ["pg-child-r1", "pg-child-r2", "pg-child-r3"]

    async with pg() as session:
        async with session.begin():
            session.add(_row(parent_id))
            for cid in child_ids:
                session.add(_row(cid, parent_id=parent_id))

    # ---------------------------------------------------------------------------
    # Wrap the session factory so that the real session's ``execute`` method
    # raises on its second call (the second child DELETE).
    # ---------------------------------------------------------------------------
    original_factory = pg

    class _BombSession:
        """Thin async context-manager wrapper that injects the execute bomb."""

        def __init__(self) -> None:
            self._inner: AsyncSession | None = None
            self._call_count = 0

        async def __aenter__(self) -> _BombSession:
            self._inner = await original_factory().__aenter__()
            original_execute = self._inner.execute  # type: ignore[attr-defined]

            async def _execute_bomb(*args: Any, **kwargs: Any) -> Any:
                self._call_count += 1
                if self._call_count == 2:
                    raise RuntimeError("injected failure on second execute")
                return await original_execute(*args, **kwargs)

            self._inner.execute = _execute_bomb  # type: ignore[method-assign]
            return self

        async def __aexit__(self, *exc_info: Any) -> bool | None:
            assert self._inner is not None
            return await self._inner.__aexit__(*exc_info)

        # Proxy ``begin()`` to the real session so ``session.begin()`` works.
        def begin(self) -> Any:
            assert self._inner is not None
            return self._inner.begin()

    patched_factory = AsyncMock()
    patched_factory.return_value = _BombSession()

    repo = PostgresSubnetRepository(session_factory=patched_factory)

    with pytest.raises(RuntimeError, match="injected failure on second execute"):
        await repo.delete_with_children(parent_id, child_ids)

    # All rows must still exist — ROLLBACK was triggered by session.begin().__aexit__
    assert await _exists(pg, parent_id), "parent must survive the failed cascade"
    for cid in child_ids:
        assert await _exists(pg, cid), f"child {cid} must survive the failed cascade"


# =============================================================================
# 3. Idempotent — parent already absent returns False, no error
# =============================================================================


@pytest.mark.asyncio
async def test_delete_with_children_parent_absent(pg: async_sessionmaker) -> None:
    """Returns False and does not raise when the parent row does not exist.

    Children listed may also be absent (they are simply skipped).
    """
    repo = PostgresSubnetRepository(session_factory=pg)
    result = await repo.delete_with_children(
        "no-such-parent", ["no-such-child-1", "no-such-child-2"]
    )
    assert result is False
