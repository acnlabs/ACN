"""Org Harness repository interface (ADR-0014)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..entities.org import Org, OrgMembership, OrgWorkItem


class IOrgRepository(ABC):
    """Persistence contract for Org, Membership, and minimal Work items."""

    # ----- Org -----

    @abstractmethod
    async def save_org(self, org: Org) -> None:
        """Insert or update an Org."""

    @abstractmethod
    async def find_org(self, org_id: str) -> Org | None:
        """Find Org by id."""

    @abstractmethod
    async def delete_org(self, org_id: str) -> bool:
        """Hard-delete Org row (memberships/work should be cleaned by service)."""

    @abstractmethod
    async def list_orgs_by_steward(self, steward_agent_id: str) -> list[Org]:
        """List Orgs whose steward_agent_id matches."""

    @abstractmethod
    async def find_org_by_subnet(self, subnet_id: str) -> Org | None:
        """Find Org bound to a subnet (1:1 fence invariant)."""

    # ----- Membership -----

    @abstractmethod
    async def upsert_membership(self, membership: OrgMembership) -> None:
        """Insert or update membership (keyed by org_id + agent_id)."""

    @abstractmethod
    async def find_membership(
        self, org_id: str, agent_id: str
    ) -> OrgMembership | None:
        """Find one membership."""

    @abstractmethod
    async def list_memberships(
        self, org_id: str, *, active_only: bool = True
    ) -> list[OrgMembership]:
        """List memberships for an Org."""

    @abstractmethod
    async def delete_memberships_for_org(self, org_id: str) -> int:
        """Delete all memberships for an Org. Returns count deleted."""

    # ----- Work -----

    @abstractmethod
    async def save_work(self, work: OrgWorkItem) -> None:
        """Insert or update a work item."""

    @abstractmethod
    async def find_work(self, org_id: str, work_id: str) -> OrgWorkItem | None:
        """Find one work item."""

    @abstractmethod
    async def list_work(
        self,
        org_id: str,
        *,
        open_only: bool = False,
    ) -> list[OrgWorkItem]:
        """List work items. open_only → status in {todo, in_progress}."""

    @abstractmethod
    async def delete_work_for_org(self, org_id: str) -> int:
        """Delete all work items for an Org. Returns count deleted."""
