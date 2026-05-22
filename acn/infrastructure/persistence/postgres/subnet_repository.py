"""PostgreSQL Implementation of ISubnetRepository"""

from contextlib import asynccontextmanager

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ....core.entities.subnet import Subnet
from ....core.interfaces import ISubnetRepository
from .models import SubnetModel


class PostgresSubnetRepository(ISubnetRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # =========================================================================
    # Internal helpers
    # =========================================================================

    @asynccontextmanager
    async def _session_scope(self, session: AsyncSession | None):
        """Yield ``session`` if passed (no commit / no close) — caller
        owns the transaction. Otherwise open + commit + close ourselves.

        Used by ``delete`` and ``delete_with_children`` to participate
        in :class:`SubnetService.delete_subnet`'s outer
        :class:`IUnitOfWork` transaction (ADR-0004 §"Cascade deletion:
        Postgres"). Same shape as the other ``Postgres*`` subnet repos
        so the three cascade DELETEs (join_requests, allowlist, subnet)
        plug into one transaction without anyone committing early.

        Only ``delete`` / ``delete_with_children`` use this scope —
        the read-side methods (``find_by_id`` / ``find_all`` / …) and
        ``save`` keep their original self-managed shape because the
        cascade orchestration never threads a session through them.
        """
        if session is not None:
            yield session
            return
        async with self._session_factory() as own_session:
            yield own_session
            await own_session.commit()

    # =========================================================================
    # Mapping
    # =========================================================================

    def _model_to_subnet(self, row: SubnetModel) -> Subnet:
        meta = row.subnet_metadata or {}
        # ADR-0004 legacy compatibility: rows that landed before the
        # ``join_policy`` column existed read back with ``row.join_policy
        # is None`` on a fresh ORM session if the Alembic migration
        # somehow rolled back column defaults — fall through to the
        # entity-level default ("open"), then ``Subnet.from_dict``-style
        # auto-upgrade rules kick in via ``__post_init__``. Practically
        # speaking the ``server_default`` on the model makes this branch
        # unreachable on healthy deployments, but the explicit guard
        # keeps the mapper safe against migration mishaps and is cheap.
        join_policy = row.join_policy or (
            "approval" if row.is_private else "open"
        )
        return Subnet(
            subnet_id=row.subnet_id,
            id=row.id,
            name=row.name,
            owner=row.owner,
            description=row.description,
            is_private=row.is_private,
            security_config=row.security_config or {},
            member_agent_ids=set(row.member_agent_ids or []),
            created_at=row.created_at,
            metadata=meta,
            harness_url=row.harness_url,
            harness_secret=row.harness_secret,
            parent_subnet_id=row.parent_subnet_id,
            lifecycle=row.lifecycle,
            linked_task_id=row.linked_task_id,
            join_policy=join_policy,
        )

    def _subnet_to_model(self, subnet: Subnet) -> SubnetModel:
        from datetime import UTC
        created = subnet.created_at
        if created and not created.tzinfo:
            created = created.replace(tzinfo=UTC)
        return SubnetModel(
            subnet_id=subnet.subnet_id,
            id=subnet.id,
            name=subnet.name,
            owner=subnet.owner,
            description=subnet.description,
            is_private=subnet.is_private,
            security_config=subnet.security_config or None,
            member_agent_ids=list(subnet.member_agent_ids) if subnet.member_agent_ids else None,
            subnet_metadata=subnet.metadata or None,
            harness_url=subnet.harness_url,
            harness_secret=subnet.harness_secret,
            parent_subnet_id=subnet.parent_subnet_id,
            lifecycle=subnet.lifecycle,
            linked_task_id=subnet.linked_task_id,
            join_policy=subnet.join_policy,
            created_at=created,
        )

    # =========================================================================
    # CRUD
    # =========================================================================

    async def save(self, subnet: Subnet) -> None:
        model = self._subnet_to_model(subnet)
        async with self._session_factory() as session:
            existing = await session.get(SubnetModel, subnet.subnet_id)
            if existing:
                # Nesting fields are included in the UPDATE so promote
                # paths (Phase 2) and any future mutation can fall
                # through correctly. ``parent_subnet_id`` is immutable
                # per ADR-0003 §5 — included here only for defence in
                # depth (service layer rejects mismatched updates).
                await session.execute(
                    update(SubnetModel)
                    .where(SubnetModel.subnet_id == subnet.subnet_id)
                    .values(
                        name=model.name,
                        owner=model.owner,
                        description=model.description,
                        is_private=model.is_private,
                        security_config=model.security_config,
                        member_agent_ids=model.member_agent_ids,
                        subnet_metadata=model.subnet_metadata,
                        harness_url=model.harness_url,
                        harness_secret=model.harness_secret,
                        parent_subnet_id=model.parent_subnet_id,
                        lifecycle=model.lifecycle,
                        linked_task_id=model.linked_task_id,
                        join_policy=model.join_policy,
                    )
                )
            else:
                session.add(model)
            await session.commit()

    async def find_by_id(self, subnet_id: str) -> Subnet | None:
        async with self._session_factory() as session:
            row = await session.get(SubnetModel, subnet_id)
            return self._model_to_subnet(row) if row else None

    async def find_all(self) -> list[Subnet]:
        async with self._session_factory() as session:
            result = await session.execute(select(SubnetModel))
            return [self._model_to_subnet(r) for r in result.scalars().all()]

    async def find_by_owner(self, owner: str) -> list[Subnet]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(SubnetModel).where(SubnetModel.owner == owner)
            )
            return [self._model_to_subnet(r) for r in result.scalars().all()]

    async def find_public_subnets(self) -> list[Subnet]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(SubnetModel).where(SubnetModel.is_private.is_(False))
            )
            return [self._model_to_subnet(r) for r in result.scalars().all()]

    async def delete(
        self, subnet_id: str, *, session: AsyncSession | None = None
    ) -> bool:
        async with self._session_scope(session) as sess:
            result = await sess.execute(
                delete(SubnetModel).where(SubnetModel.subnet_id == subnet_id)
            )
            return result.rowcount > 0

    async def delete_with_children(
        self,
        parent_id: str,
        child_ids: list[str],
        *,
        session: AsyncSession | None = None,
    ) -> bool:
        """Delete parent + children atomically in one PG transaction.

        Children are deleted before the parent so observers that scan
        rows mid-transaction (in another session that doesn't see this
        uncommitted state — there shouldn't be one, but defence in
        depth) never observe a deleted parent with surviving children.

        Transaction shape splits on ``session``:

        - ``session=None`` (legacy + ADR-0003 contract): opens a fresh
          session and uses ``async with session.begin():`` — the
          SQLAlchemy idiom for "transaction with auto rollback on
          exception". The ctx manager calls ``commit()`` on a clean
          exit and ``rollback()`` on any in-block raise, then
          re-raises. That means a failing child DELETE leaves nothing
          committed — exactly what ADR-0003 §A.4 promises for the PG
          branch. Pinned by
          ``tests/infrastructure/test_postgres_subnet_repository_cascade.py``.
        - ``session=<outer>`` (ADR-0004 cascade UoW path,
          Slice 2.1.1 / issue #75): the caller
          (``SubnetService.delete_subnet`` via
          :meth:`IUnitOfWork.transaction`) already owns the outer
          transaction. We just thread the cascade DELETEs into it.
          We deliberately do NOT call ``session.begin()`` here in
          this branch — a nested ``begin()`` would only create a
          SAVEPOINT and conflate the cross-table cascade with
          PG-specific nesting semantics, and SQLAlchemy 2.x's
          autobegin model means the second ``begin()`` actually
          raises ``InvalidRequestError`` outside savepoint mode.
          The caller's outer ``commit``/``rollback`` decides the
          batch's fate together with the sibling join_requests +
          allowlist DELETEs (ADR-0004 §"Cascade deletion: Postgres").
        """
        if session is not None:
            for child_id in child_ids:
                await session.execute(
                    delete(SubnetModel).where(
                        SubnetModel.subnet_id == child_id
                    )
                )
            result = await session.execute(
                delete(SubnetModel).where(SubnetModel.subnet_id == parent_id)
            )
            return result.rowcount > 0
        async with self._session_factory() as own_session, own_session.begin():
            for child_id in child_ids:
                await own_session.execute(
                    delete(SubnetModel).where(
                        SubnetModel.subnet_id == child_id
                    )
                )
            result = await own_session.execute(
                delete(SubnetModel).where(SubnetModel.subnet_id == parent_id)
            )
            return result.rowcount > 0

    async def exists(self, subnet_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                select(SubnetModel.subnet_id).where(SubnetModel.subnet_id == subnet_id)
            )
            return result.scalar() is not None

    async def list_subnets_for_agent(self, agent_id: str) -> list[Subnet]:
        """Return all subnets where the agent is a member (member_agent_ids contains agent_id)."""
        async with self._session_factory() as session:
            # JSONB @> operator: member_agent_ids @> '["agent_id"]'
            result = await session.execute(
                select(SubnetModel).where(
                    SubnetModel.member_agent_ids.contains([agent_id])
                )
            )
            return [self._model_to_subnet(r) for r in result.scalars().all()]

    # ------------------------------------------------------------------
    # Nesting lookups (ADR-0003)
    # ------------------------------------------------------------------

    async def find_by_parent(self, parent_subnet_id: str) -> list[Subnet]:
        """Return all child subnets nested under a given parent.

        Hits the ``subnets_parent_idx`` partial index for an O(k)
        lookup (k = number of children). Returns the empty list when
        no children exist or the parent itself is unknown.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(SubnetModel).where(
                    SubnetModel.parent_subnet_id == parent_subnet_id
                )
            )
            return [self._model_to_subnet(r) for r in result.scalars().all()]

    async def find_by_linked_task(self, task_id: str) -> list[Subnet]:
        """Return all subnets bound to a given task via ``linked_task_id``.

        Hits the ``subnets_linked_task_idx`` partial index. Consumers
        filter by ``lifecycle`` themselves if they only want
        ``task_scoped`` rows (Phase 3 cascade hook does this).
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(SubnetModel).where(
                    SubnetModel.linked_task_id == task_id
                )
            )
            return [self._model_to_subnet(r) for r in result.scalars().all()]
