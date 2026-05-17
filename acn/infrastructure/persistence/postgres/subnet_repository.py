"""PostgreSQL Implementation of ISubnetRepository"""

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ....core.entities.subnet import Subnet
from ....core.interfaces import ISubnetRepository
from .models import SubnetModel


class PostgresSubnetRepository(ISubnetRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # =========================================================================
    # Mapping
    # =========================================================================

    def _model_to_subnet(self, row: SubnetModel) -> Subnet:
        meta = row.subnet_metadata or {}
        return Subnet(
            subnet_id=row.subnet_id,
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
        )

    def _subnet_to_model(self, subnet: Subnet) -> SubnetModel:
        from datetime import UTC
        created = subnet.created_at
        if created and not created.tzinfo:
            created = created.replace(tzinfo=UTC)
        return SubnetModel(
            subnet_id=subnet.subnet_id,
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

    async def delete(self, subnet_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(SubnetModel).where(SubnetModel.subnet_id == subnet_id)
            )
            await session.commit()
            return result.rowcount > 0

    async def delete_with_children(
        self, parent_id: str, child_ids: list[str]
    ) -> bool:
        """Delete parent + children atomically in one PG transaction.

        Children are deleted before the parent so observers that scan
        rows mid-transaction (in another session that doesn't see this
        uncommitted state — there shouldn't be one, but defence in
        depth) never observe a deleted parent with surviving children.

        ``async with session.begin()`` is the SQLAlchemy idiom for
        "transaction with auto rollback on exception": the context
        manager calls ``commit()`` on a clean exit and ``rollback()`` on
        any in-block raise, then re-raises. That means a failing child
        DELETE leaves nothing committed — exactly what ADR-0003 §A.4
        promises for the PG branch.
        """
        async with self._session_factory() as session, session.begin():
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
