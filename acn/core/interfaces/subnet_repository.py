"""Subnet Repository Interface

Defines contract for subnet persistence operations.
"""

from abc import ABC, abstractmethod
from typing import Any

from ..entities import Subnet


class ISubnetRepository(ABC):
    """
    Abstract interface for Subnet persistence

    Infrastructure layer provides concrete implementation.
    """

    @abstractmethod
    async def save(self, subnet: Subnet) -> None:
        """
        Save or update a subnet

        Args:
            subnet: Subnet entity to save
        """
        pass

    @abstractmethod
    async def find_by_id(self, subnet_id: str) -> Subnet | None:
        """
        Find subnet by ID

        Args:
            subnet_id: Subnet identifier

        Returns:
            Subnet entity or None if not found
        """
        pass

    @abstractmethod
    async def find_all(self) -> list[Subnet]:
        """
        Find all subnets

        Returns:
            List of all subnet entities
        """
        pass

    @abstractmethod
    async def find_by_owner(self, owner: str) -> list[Subnet]:
        """
        Find all subnets owned by a user/system

        Args:
            owner: Subnet owner identifier

        Returns:
            List of subnets owned by the user
        """
        pass

    @abstractmethod
    async def find_public_subnets(self) -> list[Subnet]:
        """
        Find all public subnets

        Returns:
            List of public subnets
        """
        pass

    @abstractmethod
    async def delete(
        self, subnet_id: str, *, session: Any | None = None
    ) -> bool:
        """
        Delete a subnet

        Args:
            subnet_id: Subnet identifier
            session: Optional :class:`IUnitOfWork` token. When passed,
                Postgres impl binds to it (no internal commit / close)
                so the call participates in the outer transaction; the
                Redis impl ignores it. Default ``None`` is the legacy
                path with self-managed session + commit. See
                ``acn/core/interfaces/unit_of_work.py`` for the
                cross-cutting contract.

        Returns:
            True if deleted, False if not found
        """
        pass

    @abstractmethod
    async def delete_with_children(
        self,
        parent_id: str,
        child_ids: list[str],
        *,
        session: Any | None = None,
    ) -> bool:
        """
        Atomic-where-supported parent + children delete (ADR-0003 §A.4).

        Backend contract:

        - **Postgres**: single transaction. All child DELETEs and the parent
          DELETE run inside ``async with session.begin()``. Any DB error
          rolls back the whole batch — caller observes "nothing was
          deleted" instead of "some children orphaned".
        - **Redis**: sequential ``delete()`` calls (no MULTI/EXEC across
          Python method boundaries). On partial failure the implementation
          emits a ``delete_with_children_partial`` warning breadcrumb and
          raises ``RuntimeError`` BEFORE attempting the parent delete —
          the parent is preserved so an operator can retry the cascade.

        Both backends:

        - Return ``True`` when the parent row was actually removed AND
          every listed child id was processed (a child id that no
          longer exists is treated as already-deleted, not a failure —
          matches the idempotent semantics of plain ``delete()``).
        - Return ``False`` only when the parent itself was already gone.
          Listed children are still attempted; on PG they share the
          same transaction as the parent DELETE.
        - Raise on partial failure (PG: SQLAlchemy bubbles its error
          through the rolled-back ``session.begin()`` block; Redis:
          ``RuntimeError`` after the breadcrumb is logged, BEFORE the
          parent DELETE is attempted).

        Args:
            parent_id: Top-level subnet identifier to delete last.
            child_ids: Child subnet identifiers to delete first. May be
                empty — in that case behaves identically to
                ``delete(parent_id)``.
            session: Optional :class:`IUnitOfWork` token. When passed,
                Postgres impl binds to it and skips its own
                ``session.begin()`` (the outer Unit-of-Work already
                owns the transaction boundary, so a nested
                ``session.begin()`` would only create a SAVEPOINT and
                couple the cascade to PG-specific nesting semantics —
                avoided by design). Redis impl ignores it. Default
                ``None`` is the legacy self-managed path that still
                uses ``async with session.begin()`` internally for
                PG (single transaction across all child DELETEs).

        Returns:
            True on full cascade success, False when parent did not exist.
        """
        pass

    @abstractmethod
    async def exists(self, subnet_id: str) -> bool:
        """
        Check if subnet exists

        Args:
            subnet_id: Subnet identifier

        Returns:
            True if subnet exists
        """
        pass

    # ------------------------------------------------------------------
    # Nesting (ADR-0003)
    # ------------------------------------------------------------------
    # Shipped in Phase 1 so the persistence layer carries the indexes
    # and lookup methods atomically with the schema change. Service /
    # route consumers land in Phase 2 (parent lookup) and Phase 3
    # (task-state cascade). Implementations must keep these methods
    # consistent with ``save`` / ``delete`` — both maintain the
    # underlying secondary index in the same transaction / pipeline.

    @abstractmethod
    async def find_by_parent(self, parent_subnet_id: str) -> list[Subnet]:
        """
        Find all child subnets nested under a given parent.

        Returns the empty list when no children exist or the parent
        itself is unknown — callers that need to distinguish "no
        children" from "parent missing" should ``find_by_id`` the
        parent separately.

        Args:
            parent_subnet_id: Parent subnet identifier

        Returns:
            List of subnets whose ``parent_subnet_id`` equals the
            argument.
        """
        pass

    @abstractmethod
    async def find_by_linked_task(self, task_id: str) -> list[Subnet]:
        """
        Find all subnets bound to a given task via ``linked_task_id``.

        Used by the task-state-machine cascade hook (Phase 3) to
        dissolve ``task_scoped`` children when the linked task reaches
        a terminal state. Includes both ``task_scoped`` and (defensively)
        any ``persistent`` rows that still carry the field — callers
        should filter by ``lifecycle`` themselves when that matters.

        Args:
            task_id: Task identifier

        Returns:
            List of subnets whose ``linked_task_id`` equals the
            argument.
        """
        pass
