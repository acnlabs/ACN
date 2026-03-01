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

    async def exists(self, subnet_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                select(SubnetModel.subnet_id).where(SubnetModel.subnet_id == subnet_id)
            )
            return result.scalar() is not None
