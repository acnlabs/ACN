"""PG repo ``_session_scope`` dual-mode contract for ADR-0004 cascade.

What this pins
--------------
Slice 2.1.1 / issue #75 closed a gap where the three Postgres
cascade DELETEs (``subnet_join_requests`` / ``subnet_allowlist`` /
``subnets``) each opened and committed their own session. Production
PG mode now threads a single :class:`AsyncSession` through all three
via :meth:`IUnitOfWork.transaction`, so any failure rolls the whole
batch back.

The dual-mode contract each cascade-participating repo method must
honour:

- ``session=None`` (default): open a fresh session via the
  ``session_factory``, run the DELETE, ``await own_session.commit()``,
  let the ``async with`` close the connection. This is the legacy /
  Redis-only / out-of-tree path.
- ``session=<outer AsyncSession>``: bind to the caller's session,
  run the DELETE, **do NOT commit, do NOT close**. The outer
  Unit-of-Work owns commit-on-clean-exit and rollback-on-exception;
  early commit here would shatter the saga.

These tests check both branches for:

- ``PostgresSubnetJoinRequestRepository.delete_for_subnet``
- ``PostgresSubnetAllowlistRepository.delete_for_subnet``
- ``PostgresSubnetRepository.delete``
- ``PostgresSubnetRepository.delete_with_children`` (special-cased —
  see test docstring; the self-managed branch keeps its
  ``async with session.begin():`` envelope for explicit rollback
  semantics, while the outer-session branch skips ``begin()`` so it
  composes cleanly inside the caller's ``IUnitOfWork`` transaction).

The mock shape mirrors the one in
``tests/infrastructure/test_postgres_subnet_repository_cascade.py`` —
a ``MagicMock`` factory that returns an ``AsyncMock`` session whose
``__aenter__`` / ``__aexit__`` are set up so ``async with
session_factory() as own:`` works. The point of mocking is to make
assertions about ``commit`` / ``close`` / ``begin`` call counts
without spinning up Postgres.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.infrastructure.persistence.postgres.subnet_allowlist_repository import (
    PostgresSubnetAllowlistRepository,
)
from acn.infrastructure.persistence.postgres.subnet_join_request_repository import (
    PostgresSubnetJoinRequestRepository,
)
from acn.infrastructure.persistence.postgres.subnet_repository import (
    PostgresSubnetRepository,
)

# ---------------------------------------------------------------------------
# Mock plumbing
# ---------------------------------------------------------------------------


def _make_own_session_factory(rowcount: int = 0):
    """Build a factory whose ``__call__`` yields an AsyncMock session.

    The session's ``__aenter__`` returns itself (so
    ``async with factory() as sess: sess.execute(...)`` works), and
    ``execute`` returns a mock result carrying the requested
    ``rowcount``.
    """
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None

    result = MagicMock()
    result.rowcount = rowcount
    session.execute.return_value = result

    factory = MagicMock(return_value=session)
    return factory, session


def _make_external_session(rowcount: int = 0):
    """Build a stand-in for an outer ``AsyncSession`` already opened
    by ``IUnitOfWork.transaction()``.

    No ``__aenter__`` / ``__aexit__`` — the cascade method receives
    the session directly, so it never enters a context manager on it.
    """
    session = AsyncMock()
    result = MagicMock()
    result.rowcount = rowcount
    session.execute.return_value = result
    return session


# ---------------------------------------------------------------------------
# PostgresSubnetJoinRequestRepository.delete_for_subnet
# ---------------------------------------------------------------------------


class TestJoinRequestRepoSessionScope:
    @pytest.mark.asyncio
    async def test_self_managed_opens_session_and_commits(self):
        """``session=None`` (legacy / Redis-only path): factory is
        called exactly once, DELETE runs on the borrowed session,
        ``commit`` is awaited on it before the ``async with`` exits."""
        factory, own_session = _make_own_session_factory(rowcount=5)
        repo = PostgresSubnetJoinRequestRepository(session_factory=factory)

        rows = await repo.delete_for_subnet("subnet-1")

        assert rows == 5
        factory.assert_called_once()
        own_session.execute.assert_awaited_once()
        own_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_external_session_skips_factory_and_commit(self):
        """``session=<outer>``: factory is NOT called (we reuse the
        outer session), DELETE runs on the outer session, and we MUST
        NOT commit — the outer UoW owns the transaction boundary."""
        factory, _ = _make_own_session_factory()  # built to detect leakage
        outer = _make_external_session(rowcount=7)
        repo = PostgresSubnetJoinRequestRepository(session_factory=factory)

        rows = await repo.delete_for_subnet("subnet-1", session=outer)

        assert rows == 7
        factory.assert_not_called()
        outer.execute.assert_awaited_once()
        # Critical: no early commit. The outer Unit-of-Work decides
        # commit / rollback when ITS context exits.
        outer.commit.assert_not_awaited()
        outer.close.assert_not_awaited()


# ---------------------------------------------------------------------------
# PostgresSubnetAllowlistRepository.delete_for_subnet
# ---------------------------------------------------------------------------


class TestAllowlistRepoSessionScope:
    @pytest.mark.asyncio
    async def test_self_managed_opens_session_and_commits(self):
        factory, own_session = _make_own_session_factory(rowcount=2)
        repo = PostgresSubnetAllowlistRepository(session_factory=factory)

        rows = await repo.delete_for_subnet("subnet-1")

        assert rows == 2
        factory.assert_called_once()
        own_session.execute.assert_awaited_once()
        own_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_external_session_skips_factory_and_commit(self):
        factory, _ = _make_own_session_factory()
        outer = _make_external_session(rowcount=3)
        repo = PostgresSubnetAllowlistRepository(session_factory=factory)

        rows = await repo.delete_for_subnet("subnet-1", session=outer)

        assert rows == 3
        factory.assert_not_called()
        outer.execute.assert_awaited_once()
        outer.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# PostgresSubnetRepository.delete
# ---------------------------------------------------------------------------


class TestSubnetRepoDeleteSessionScope:
    @pytest.mark.asyncio
    async def test_self_managed_opens_session_and_commits(self):
        factory, own_session = _make_own_session_factory(rowcount=1)
        repo = PostgresSubnetRepository(session_factory=factory)

        deleted = await repo.delete("subnet-1")

        assert deleted is True
        factory.assert_called_once()
        own_session.execute.assert_awaited_once()
        own_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_external_session_skips_factory_and_commit(self):
        factory, _ = _make_own_session_factory()
        outer = _make_external_session(rowcount=1)
        repo = PostgresSubnetRepository(session_factory=factory)

        deleted = await repo.delete("subnet-1", session=outer)

        assert deleted is True
        factory.assert_not_called()
        outer.execute.assert_awaited_once()
        outer.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_self_managed_returns_false_when_rowcount_zero(self):
        """Smoke: the rowcount → bool mapping survives the session
        scope change. ``rowcount=0`` ⇒ ``False`` (idempotent
        already-gone path)."""
        factory, _ = _make_own_session_factory(rowcount=0)
        repo = PostgresSubnetRepository(session_factory=factory)

        assert await repo.delete("missing-subnet") is False


# ---------------------------------------------------------------------------
# PostgresSubnetRepository.delete_with_children
# ---------------------------------------------------------------------------


class TestSubnetRepoDeleteWithChildrenSessionScope:
    """``delete_with_children`` is the only PG cascade method that
    splits on ``session`` *outside* a shared ``_session_scope``
    helper: the self-managed branch keeps its explicit
    ``async with session.begin():`` envelope (the ADR-0003 §A.4
    contract — pinned by
    ``test_postgres_subnet_repository_cascade.py``); the outer-session
    branch must NOT call ``begin()`` because the caller's
    ``IUnitOfWork.transaction()`` already opened the transaction and
    a nested ``begin()`` either creates a savepoint (wrong semantics
    for a cross-table cascade) or raises ``InvalidRequestError`` (in
    SQLAlchemy 2.x autobegin mode).
    """

    @pytest.mark.asyncio
    async def test_external_session_does_not_begin_or_commit(self):
        outer = _make_external_session(rowcount=1)
        # Spy: ``begin`` must not be called on the outer session.
        outer.begin = MagicMock()
        factory, _ = _make_own_session_factory()
        repo = PostgresSubnetRepository(session_factory=factory)

        await repo.delete_with_children(
            "parent-1", ["child-1", "child-2"], session=outer
        )

        # Factory not consulted — we reused the outer session.
        factory.assert_not_called()
        # Two child DELETEs + one parent DELETE.
        assert outer.execute.await_count == 3
        # No fresh transaction layer — outer UoW owns it.
        outer.begin.assert_not_called()
        outer.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_self_managed_still_uses_begin_block(self):
        """Regression guard: don't accidentally let the
        ``session=None`` path collapse into the no-begin shape of
        the outer-session branch — that would weaken the legacy
        ADR-0003 §A.4 atomicity contract."""
        factory, own_session = _make_own_session_factory(rowcount=1)
        begin_ctx = AsyncMock()
        begin_ctx.__aenter__.return_value = begin_ctx
        begin_ctx.__aexit__.return_value = None
        own_session.begin = MagicMock(return_value=begin_ctx)
        repo = PostgresSubnetRepository(session_factory=factory)

        await repo.delete_with_children(
            "parent-1", ["child-1"], session=None
        )

        # Single explicit transaction envelope; commit handled by
        # the ctx manager — NO explicit ``own_session.commit()``.
        own_session.begin.assert_called_once()
        begin_ctx.__aenter__.assert_awaited_once()
        begin_ctx.__aexit__.assert_awaited_once()
        own_session.commit.assert_not_awaited()
