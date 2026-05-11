"""Unit-of-Work Protocol — minimal transaction boundary abstraction.

Why this exists
---------------
``task_service`` orchestrates multi-repository writes (e.g. the saga
introduced in v0.1: CAS save on ``tasks`` + INSERT on
``settlement_outbox``, both required to commit-or-rollback together).
We refuse to let ``task_service`` directly ``import sqlalchemy`` for
two reasons:

1. ACN supports a Redis-only / in-memory mode (used by local dev,
   contract tests, and a future "lite" deployment shape) that never
   speaks SQL. Pulling SQLAlchemy into the service layer would make
   that mode silently impossible.
2. The service layer's job is to compose use-cases, not to know which
   storage tech is underneath. A transaction boundary is a
   storage-agnostic concept; the *implementation* of that boundary
   is storage-specific.

So this Protocol exposes exactly one thing: ``transaction()`` returns
an async context manager that yields an opaque session token. The
service layer passes that token into repository methods via their
``session=...`` parameter; repositories that understand the token's
type bind to it, others ignore it.

Concrete implementations
------------------------
- ``PostgresUnitOfWork`` (acn.infrastructure.persistence.postgres.unit_of_work)
  wraps an ``async_sessionmaker`` and yields a real ``AsyncSession``.
  Commits on clean exit, rolls back on exception.

- A null / Redis-only deployment simply does NOT inject a
  ``IUnitOfWork`` into the service; the service then short-circuits
  to its legacy (non-saga) code path. This is intentional — saga is
  a PG-mode feature in v0.1.

Why ``yield Any`` rather than a typed session
---------------------------------------------
Because the session type IS the implementation detail we're trying
to hide. The service layer treats the yielded value as opaque and
just shovels it into ``repo.something(..., session=token)``. Type
narrowing happens inside each repository's own implementation.

Caller contract
---------------
- Use as ``async with uow.transaction() as session:``.
- Repository calls inside the block MUST pass ``session=session``;
  any repo call that omits it falls back to its own session and
  WILL NOT participate in the outer transaction (silent bug).
- An exception inside the block triggers rollback; clean exit
  triggers commit. Callers MUST NOT call commit/rollback themselves
  — that's the Unit-of-Work's responsibility.
- The token returned by ``transaction()`` is NOT thread-safe and
  MUST NOT be passed between asyncio tasks.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class IUnitOfWork(Protocol):
    """Storage-agnostic transaction boundary.

    See module docstring for the design rationale and caller contract.
    """

    def transaction(self) -> AbstractAsyncContextManager[Any]:
        """Open a transaction, yielding an opaque session token.

        The token's runtime type depends on the concrete
        implementation: ``AsyncSession`` for ``PostgresUnitOfWork``,
        ``None`` or similar for hypothetical null backends.

        On successful exit the implementation commits; on exception
        it rolls back. Callers MUST NOT manually commit or rollback.
        """
        ...
