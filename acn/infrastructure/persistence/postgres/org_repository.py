"""PostgreSQL implementation of IOrgRepository."""

from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ....core.entities.org import (
    Org,
    OrgMembership,
    OrgOwner,
    OrgPrincipal,
    OrgWorkItem,
)
from ....core.exceptions import OrgSubnetBindingConflictError
from ....core.interfaces.org_repository import IOrgRepository
from .models import OrgMembershipModel, OrgModel, OrgWorkItemModel


class PostgresOrgRepository(IOrgRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def _model_to_org(self, row: OrgModel) -> Org:
        return Org(
            org_id=row.org_id,
            display_name=row.display_name,
            charter=row.charter or {},
            owner=OrgOwner(kind=row.owner_kind, subject=row.owner_subject),  # type: ignore[arg-type]
            created_by=OrgPrincipal(
                kind=row.created_by_kind,  # type: ignore[arg-type]
                subject=row.created_by_subject,
            ),
            subnet_id=row.subnet_id,
            steward_agent_id=row.steward_agent_id,
            plugins=row.plugins or {"work": "minimal", "loop": "thin", "memory": "noop"},
            roles=list(row.roles or ["manager", "worker", "reviewer"]),
            status=row.status,  # type: ignore[arg-type]
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _org_to_values(self, org: Org) -> dict:
        return {
            "org_id": org.org_id,
            "display_name": org.display_name,
            "charter": org.charter or None,
            "owner_kind": org.owner.kind,
            "owner_subject": org.owner.subject,
            "created_by_kind": org.created_by.kind,
            "created_by_subject": org.created_by.subject,
            "subnet_id": org.subnet_id,
            "steward_agent_id": org.steward_agent_id,
            "plugins": org.plugins or None,
            "roles": list(org.roles) if org.roles else None,
            "status": org.status,
            "created_at": org.created_at,
            "updated_at": org.updated_at,
        }

    async def save_org(self, org: Org) -> None:
        values = self._org_to_values(org)
        async with self._session_factory() as session:
            existing = await session.get(OrgModel, org.org_id)
            if existing:
                await session.execute(
                    update(OrgModel)
                    .where(OrgModel.org_id == org.org_id)
                    .values(**{k: v for k, v in values.items() if k != "org_id"})
                )
            else:
                session.add(OrgModel(**values))
            try:
                await session.commit()
            except IntegrityError as e:
                # ``uq_orgs_subnet_id`` (one Org per fence, ADR-0014):
                # surface as the domain conflict instead of a bare 500 so
                # a create that loses the pre-check race gets a 409.
                await session.rollback()
                if "uq_orgs_subnet_id" not in str(e.orig or e):
                    raise
                holder = await session.execute(
                    select(OrgModel.org_id)
                    .where(OrgModel.subnet_id == org.subnet_id)
                    .limit(1)
                )
                bound_org_id = holder.scalar_one_or_none() or "(unknown)"
                raise OrgSubnetBindingConflictError(
                    org.subnet_id, bound_org_id
                ) from e

    async def find_org(self, org_id: str) -> Org | None:
        async with self._session_factory() as session:
            row = await session.get(OrgModel, org_id)
            return self._model_to_org(row) if row else None

    async def delete_org(self, org_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(OrgModel).where(OrgModel.org_id == org_id)
            )
            await session.commit()
            return (result.rowcount or 0) > 0

    async def list_orgs_by_steward(self, steward_agent_id: str) -> list[Org]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(OrgModel).where(OrgModel.steward_agent_id == steward_agent_id)
            )
            return [self._model_to_org(r) for r in result.scalars().all()]

    async def find_org_by_subnet(self, subnet_id: str) -> Org | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(OrgModel).where(OrgModel.subnet_id == subnet_id).limit(1)
            )
            row = result.scalar_one_or_none()
            return self._model_to_org(row) if row else None

    async def upsert_membership(self, membership: OrgMembership) -> None:
        async with self._session_factory() as session:
            existing = await session.get(
                OrgMembershipModel, (membership.org_id, membership.agent_id)
            )
            if existing:
                existing.role = membership.role
                existing.reports_to = membership.reports_to
                existing.status = membership.status
                existing.joined_at = membership.joined_at
            else:
                session.add(
                    OrgMembershipModel(
                        org_id=membership.org_id,
                        agent_id=membership.agent_id,
                        role=membership.role,
                        reports_to=membership.reports_to,
                        status=membership.status,
                        joined_at=membership.joined_at,
                    )
                )
            await session.commit()

    async def find_membership(
        self, org_id: str, agent_id: str
    ) -> OrgMembership | None:
        async with self._session_factory() as session:
            row = await session.get(OrgMembershipModel, (org_id, agent_id))
            if not row:
                return None
            return OrgMembership(
                org_id=row.org_id,
                agent_id=row.agent_id,
                role=row.role,
                reports_to=row.reports_to,
                status=row.status,  # type: ignore[arg-type]
                joined_at=row.joined_at,
            )

    async def list_memberships(
        self, org_id: str, *, active_only: bool = True
    ) -> list[OrgMembership]:
        async with self._session_factory() as session:
            stmt = select(OrgMembershipModel).where(
                OrgMembershipModel.org_id == org_id
            )
            if active_only:
                stmt = stmt.where(OrgMembershipModel.status == "active")
            result = await session.execute(stmt)
            return [
                OrgMembership(
                    org_id=r.org_id,
                    agent_id=r.agent_id,
                    role=r.role,
                    reports_to=r.reports_to,
                    status=r.status,  # type: ignore[arg-type]
                    joined_at=r.joined_at,
                )
                for r in result.scalars().all()
            ]

    async def delete_memberships_for_org(self, org_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(OrgMembershipModel).where(OrgMembershipModel.org_id == org_id)
            )
            await session.commit()
            return result.rowcount or 0

    async def save_work(self, work: OrgWorkItem) -> None:
        async with self._session_factory() as session:
            existing = await session.get(OrgWorkItemModel, work.work_id)
            if existing:
                existing.org_id = work.org_id
                existing.title = work.title
                existing.status = work.status
                existing.assignee_agent_id = work.assignee_agent_id
                existing.updated_at = work.updated_at
            else:
                session.add(
                    OrgWorkItemModel(
                        work_id=work.work_id,
                        org_id=work.org_id,
                        title=work.title,
                        status=work.status,
                        assignee_agent_id=work.assignee_agent_id,
                        created_at=work.created_at,
                        updated_at=work.updated_at,
                    )
                )
            await session.commit()

    async def find_work(self, org_id: str, work_id: str) -> OrgWorkItem | None:
        async with self._session_factory() as session:
            row = await session.get(OrgWorkItemModel, work_id)
            if not row or row.org_id != org_id:
                return None
            return OrgWorkItem(
                work_id=row.work_id,
                org_id=row.org_id,
                title=row.title,
                status=row.status,  # type: ignore[arg-type]
                assignee_agent_id=row.assignee_agent_id,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )

    async def list_work(
        self,
        org_id: str,
        *,
        open_only: bool = False,
    ) -> list[OrgWorkItem]:
        async with self._session_factory() as session:
            stmt = select(OrgWorkItemModel).where(OrgWorkItemModel.org_id == org_id)
            if open_only:
                stmt = stmt.where(
                    OrgWorkItemModel.status.in_(("todo", "in_progress"))
                )
            result = await session.execute(stmt)
            return [
                OrgWorkItem(
                    work_id=r.work_id,
                    org_id=r.org_id,
                    title=r.title,
                    status=r.status,  # type: ignore[arg-type]
                    assignee_agent_id=r.assignee_agent_id,
                    created_at=r.created_at,
                    updated_at=r.updated_at,
                )
                for r in result.scalars().all()
            ]

    async def delete_work_for_org(self, org_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(OrgWorkItemModel).where(OrgWorkItemModel.org_id == org_id)
            )
            await session.commit()
            return result.rowcount or 0
