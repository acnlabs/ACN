"""PostgreSQL implementation of ``ISubnetAllowlistRepository``.

Flat-set repo, no state machine — much thinner than
``PostgresSubnetJoinRequestRepository``. ``add`` is idempotent via
the composite PK ``(subnet_id, agent_id)``: ``INSERT ... ON
CONFLICT DO NOTHING`` collapses re-adds into a no-op and returns
``False`` so the route layer can pick 200 (re-add) vs 201 (new) per
ADR §HTTP status code conventions.

The route layer is expected to verify ``agent_id`` exists in
``agents`` BEFORE calling ``add`` (per ADR §SubnetAllowlist
"Allowlist add requires the target agent_id to already exist in
the agent registry"); the route returns ``404 AGENT_NOT_FOUND``
on the existence-check failure. We don't enforce that here because
there is no FK to ``agents`` (ADR §"Cascade deletion" chooses
manual cascade for observability).
"""

from contextlib import asynccontextmanager

from sqlalchemy import delete, desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ....core.entities import SubnetAllowlist
from ....core.interfaces import ISubnetAllowlistRepository
from .models import SubnetAllowlistModel


class PostgresSubnetAllowlistRepository(ISubnetAllowlistRepository):
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _session_scope(self, session: AsyncSession | None):
        """Yield ``session`` if passed (no commit / no close) — caller
        owns the transaction. Otherwise open + commit + close ourselves.

        Mirrors
        :meth:`PostgresSubnetJoinRequestRepository._session_scope` so
        both halves of the ADR-0004 cascade compose under the same
        outer ``IUnitOfWork`` transaction without either repo
        committing early.
        """
        if session is not None:
            yield session
            return
        async with self._session_factory() as own_session:
            yield own_session
            await own_session.commit()

    # ------------------------------------------------------------------
    # Mapping
    # ------------------------------------------------------------------

    def _model_to_entity(self, row: SubnetAllowlistModel) -> SubnetAllowlist:
        return SubnetAllowlist(
            slug=row.slug,
            agent_id=row.agent_id,
            added_by=row.added_by,
            added_at=row.added_at,
        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def add(self, entry: SubnetAllowlist) -> bool:
        """Insert with idempotent on-conflict no-op.

        Returns ``True`` if a new row was inserted; ``False`` if the
        ``(subnet_id, agent_id)`` pair already existed. The boolean
        return is the route layer's signal to pick 201 vs 200.

        ``ON CONFLICT DO NOTHING`` is preferred over a get-then-insert
        race-prone pattern: two concurrent owner-side adds on the
        same pair end up with one successful row and one no-op, both
        observing ``False`` from the duplicate side without ever
        raising an ``IntegrityError``. Matches the discipline
        ``PostgresAgentAllowlistRepository.add`` uses for the same
        composite-PK shape.

        The existing-row's ``added_by`` / ``added_at`` are
        intentionally **not** updated on conflict; the original
        audit attribution wins. An admin-side "force re-add with
        new attribution" path would need a separate
        ``upsert_with_new_attribution`` method (not part of Slice
        2.1's scope).
        """
        async with self._session_factory() as session:
            stmt = (
                pg_insert(SubnetAllowlistModel)
                .values(
                    slug=entry.slug,
                    agent_id=entry.agent_id,
                    added_by=entry.added_by,
                    added_at=entry.added_at,
                )
                .on_conflict_do_nothing(
                    index_elements=["slug", "agent_id"]
                )
                .returning(SubnetAllowlistModel.slug)
            )
            result = await session.execute(stmt)
            await session.commit()
            # ``.first()`` is ``None`` iff ON CONFLICT swallowed the
            # insert; presence of any row means a new entry was created.
            return result.first() is not None

    async def remove(self, slug: str, agent_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(SubnetAllowlistModel).where(
                    SubnetAllowlistModel.slug == slug,
                    SubnetAllowlistModel.agent_id == agent_id,
                )
            )
            await session.commit()
            return (result.rowcount or 0) > 0

    async def is_member(self, slug: str, agent_id: str) -> bool:
        """Cold-path membership check for the §join flow's branch 4.

        ``SELECT 1 WHERE ... LIMIT 1`` instead of ``SELECT *`` —
        the row contents are never consumed; we only need the
        existence bit. Avoids materialising the ``added_by`` /
        ``added_at`` columns that the hot path doesn't read.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(SubnetAllowlistModel.slug)
                .where(
                    SubnetAllowlistModel.slug == slug,
                    SubnetAllowlistModel.agent_id == agent_id,
                )
                .limit(1)
            )
            return result.scalar() is not None

    async def list_for_subnet(
        self, slug: str, *, limit: int = 100, offset: int = 0
    ) -> list[SubnetAllowlist]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(SubnetAllowlistModel)
                .where(SubnetAllowlistModel.slug == slug)
                .order_by(desc(SubnetAllowlistModel.added_at))
                .limit(limit)
                .offset(offset)
            )
            return [self._model_to_entity(r) for r in result.scalars().all()]

    async def delete_for_subnet(
        self, slug: str, *, session: AsyncSession | None = None
    ) -> int:
        """Cascade-delete all entries for a subnet. Returns count deleted.

        Called from ``SubnetService.delete_subnet`` — see
        :meth:`PostgresSubnetJoinRequestRepository.delete_for_subnet`
        for the symmetric outer-session contract (passing ``session``
        binds this DELETE to the caller's :class:`IUnitOfWork`
        transaction; ``None`` is the legacy self-managed path that
        commits independently).
        """
        async with self._session_scope(session) as sess:
            result = await sess.execute(
                delete(SubnetAllowlistModel).where(
                    SubnetAllowlistModel.slug == slug
                )
            )
            return result.rowcount or 0
