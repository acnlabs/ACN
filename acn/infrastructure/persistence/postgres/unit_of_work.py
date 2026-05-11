"""PostgreSQL implementation of IUnitOfWork.

Wraps an ``async_sessionmaker`` and yields a real ``AsyncSession`` so
the service layer can pass it down to repositories via their
``session=...`` parameter without the service layer itself knowing
about SQLAlchemy.

Semantics
---------
``async with uow.transaction() as session`` opens a fresh
``AsyncSession`` from the factory. On clean exit it commits; on any
exception it rolls back and re-raises. The yielded ``AsyncSession``
is bound to a single connection borrowed from the pool for the
lifetime of the ``with`` block.

This is intentionally a thin wrapper — there is no batched repository
collection, no event-bus, no per-aggregate cache. The Unit-of-Work
abstraction in v0.1 exists for exactly one reason: pin a single
``AsyncSession`` across the saga's CAS-save + outbox-enqueue pair.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ....core.interfaces.unit_of_work import IUnitOfWork


class PostgresUnitOfWork(IUnitOfWork):
    """Concrete UoW backed by ``async_sessionmaker``.

    Constructor reuses the same factory the repositories were built
    against, so the session yielded here lives in the same pool —
    queries inside the transaction don't accidentally race a
    second connection.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                # ``async with self._session_factory()`` will close the
                # session on exit, but we still need an explicit
                # rollback before re-raise to release any row-level
                # locks the transaction acquired. ``compare_and_save``
                # runs an atomic ``UPDATE ... WHERE task_id=? AND
                # status=?`` which PostgreSQL implements with a
                # row-level exclusive lock; the lock is held until
                # commit OR rollback. Letting close-without-commit
                # rollback implicitly works on asyncpg but is not
                # contractually guaranteed across drivers, so we
                # rollback explicitly to avoid surprises if the
                # underlying driver ever changes.
                await session.rollback()
                raise
