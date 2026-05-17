"""Subnet Service

Business logic for subnet management.
"""

import dataclasses
from typing import Literal

import structlog  # type: ignore[import-untyped]

from ..core.entities import Subnet
from ..core.exceptions import SubnetNotFoundException
from ..core.interfaces import ISubnetRepository
from ..core.interfaces.task_repository import ITaskRepository

logger = structlog.get_logger()

# Sentinel error reasons surfaced to callers (route layer maps to
# ``INVALID_REQUEST`` / ``NOT_SUBNET_MEMBER`` with this exact string
# in ``details.reason``). Keep grep-able and stable — clients read
# them programmatically.
REASON_PARENT_NOT_FOUND = "parent_not_found"
REASON_PARENT_IS_RESERVED = "parent_is_reserved"
REASON_PARENT_IS_NESTED = "parent_is_nested"
REASON_TASK_SCOPED_REQUIRES_LINKED_TASK = "task_scoped_requires_linked_task"
REASON_LINKED_TASK_NOT_FOUND = "linked_task_not_found"
REASON_NOT_PARENT_MEMBER = "not_parent_member"


class SubnetNestingError(ValueError):
    """Raised by ``SubnetService`` when a nesting invariant rejects a
    request. Carries a stable ``reason`` string (one of the
    ``REASON_*`` constants above) that the route layer surfaces as
    ``details.reason``.

    Kept as a ``ValueError`` subclass so legacy callers that catch
    ``ValueError`` (e.g. ``routes/subnets.py::create_subnet``) keep
    working — they just see a richer payload than a bare message.
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


class SubnetService:
    """
    Subnet Service

    Orchestrates subnet-related business operations.
    Uses Repository pattern for persistence.

    Optionally accepts an :class:`ITaskRepository` so
    ``create_subnet`` can validate ``linked_task_id`` against the
    task store. Production wiring (``api.py`` lifespan) supplies
    one; legacy test fixtures that don't exercise nesting can omit
    it — the ``linked_task_not_found`` path falls back to "skip
    task existence check" when the repository is missing, which
    keeps non-nesting code paths green.
    """

    def __init__(
        self,
        subnet_repository: ISubnetRepository,
        task_repository: ITaskRepository | None = None,
    ):
        """
        Initialize Subnet Service

        Args:
            subnet_repository: Subnet repository implementation
            task_repository: Optional task repository — required only
                by the ``linked_task_not_found`` validation path in
                ``create_subnet``. Omit for legacy fixtures that
                don't exercise nesting.
        """
        self.repository = subnet_repository
        self.task_repository = task_repository

    async def create_subnet(
        self,
        subnet_id: str,
        name: str,
        owner: str,
        description: str | None = None,
        is_private: bool = False,
        security_config: dict | None = None,
        metadata: dict | None = None,
        parent_subnet_id: str | None = None,
        lifecycle: Literal["persistent", "task_scoped"] = "persistent",
        linked_task_id: str | None = None,
    ) -> Subnet:
        """
        Create a new subnet.

        ADR-0003 invariants enforced when nesting params are set:

        - ``parent_subnet_id`` must reference an existing subnet —
          ``parent_not_found``.
        - Parent must not be reserved (``public``/``system``) —
          ``parent_is_reserved``. Catches both reserved IDs and
          (defence in depth) any subnet whose owner is ``system``.
        - Parent's own ``parent_subnet_id`` must be ``None`` (single-
          layer cap) — ``parent_is_nested``.
        - ``lifecycle == "task_scoped"`` requires
          ``linked_task_id`` to be set — ``task_scoped_requires_linked_task``.
        - ``linked_task_id`` must reference an existing task when a
          ``task_repository`` is wired — ``linked_task_not_found``.

        Args:
            subnet_id: Subnet identifier
            name: Subnet name
            owner: Subnet owner
            description: Subnet description
            is_private: Whether subnet is private
            security_config: Security configuration
            metadata: Additional metadata
            parent_subnet_id: Optional parent subnet ID (ADR-0003)
            lifecycle: ``"persistent"`` (default) or ``"task_scoped"``
            linked_task_id: Required when ``lifecycle == "task_scoped"``

        Returns:
            Created subnet entity

        Raises:
            ValueError: If subnet already exists
            SubnetNestingError: On any of the five invariant rejections
        """
        # Check if subnet already exists
        if await self.repository.exists(subnet_id):
            raise ValueError(f"Subnet {subnet_id} already exists")

        # --- ADR-0003 nesting validations -----------------------------
        # Entity-layer already enforces lifecycle ↔ linked_task_id
        # pairing both directions, the reserved-ID rules, and lifecycle
        # value validity. We layer the *service-only* invariants on
        # top (parent existence + single-layer cap + linked task
        # existence) because they require repository lookups.
        if lifecycle == "task_scoped" and linked_task_id is None:
            # Surfaced with a stable reason so the route layer's
            # contract test can pin the exact error code without
            # depending on the (English) message text.
            raise SubnetNestingError(
                REASON_TASK_SCOPED_REQUIRES_LINKED_TASK,
                "lifecycle='task_scoped' requires linked_task_id",
            )

        if parent_subnet_id is not None:
            parent = await self.repository.find_by_id(parent_subnet_id)
            if parent is None:
                raise SubnetNestingError(
                    REASON_PARENT_NOT_FOUND,
                    f"Parent subnet '{parent_subnet_id}' does not exist",
                )
            # ADR-0003 §A invariant 5 — reserved subnets cannot be
            # parents. Catch by ID *and* (defensively) by owner: the
            # ``system`` owner literal is the platform escape hatch
            # and any subnet under it is treated as platform-owned.
            if parent_subnet_id in {"public", "system"} or parent.owner == "system":
                raise SubnetNestingError(
                    REASON_PARENT_IS_RESERVED,
                    f"Parent subnet '{parent_subnet_id}' is reserved",
                )
            # Single-layer cap — parent must itself be top-level.
            if parent.parent_subnet_id is not None:
                raise SubnetNestingError(
                    REASON_PARENT_IS_NESTED,
                    f"Parent subnet '{parent_subnet_id}' is itself nested; "
                    "single-layer cap enforced",
                )

        if linked_task_id is not None and self.task_repository is not None:
            # Skip the existence check when no task_repository is
            # wired — preserves backward-compat for test fixtures
            # that don't exercise nesting. Production composition
            # in ``api.py`` always supplies one.
            task_exists = await self.task_repository.exists(linked_task_id)
            if not task_exists:
                raise SubnetNestingError(
                    REASON_LINKED_TASK_NOT_FOUND,
                    f"Linked task '{linked_task_id}' does not exist",
                )

        subnet = Subnet(
            subnet_id=subnet_id,
            name=name,
            owner=owner,
            description=description,
            is_private=is_private,
            security_config=security_config or {},
            metadata=metadata or {},
            parent_subnet_id=parent_subnet_id,
            lifecycle=lifecycle,
            linked_task_id=linked_task_id,
        )
        # The owner is implicitly a member: every ACL that keys off
        # `subnet.member_agent_ids` (private GET /tasks/{id}, accept_task,
        # is_subnet_member, ...) otherwise 403s the owner from acting on the
        # subnet they just created. Mirror this in the entity at construction
        # time so harness bootstrap (create_subnet → register_harness →
        # create_task) works without a follow-up join_subnet hack.
        #
        # Child-subnet edge case: the membership-subset invariant
        # (``add_member`` rejects non-parent members) is *not* tripped
        # here because the owner-add bypasses ``SubnetService.add_member``
        # by calling the entity method directly. For a child subnet to
        # be useful, the owner must already be a parent member —
        # otherwise the only person who can do anything in the squad
        # (the owner) cannot widen its membership. We deliberately do
        # NOT enforce this at create-time: an owner who creates a
        # child subnet they're not in is a valid governance pattern
        # (delegated admin), and the membership-subset invariant
        # naturally limits what they can do afterward.
        subnet.add_member(owner)

        logger.info(
            "create_subnet",
            subnet_id=subnet_id,
            name=name,
            owner=owner,
            parent_subnet_id=parent_subnet_id,
            lifecycle=lifecycle,
            linked_task_id=linked_task_id,
        )
        await self.repository.save(subnet)
        return subnet

    async def get_subnet(self, subnet_id: str) -> Subnet:
        """
        Get subnet by ID

        Args:
            subnet_id: Subnet identifier

        Returns:
            Subnet entity

        Raises:
            SubnetNotFoundException: If subnet not found
        """
        subnet = await self.repository.find_by_id(subnet_id)
        if not subnet:
            raise SubnetNotFoundException(f"Subnet {subnet_id} not found")
        return subnet

    async def list_subnets(self, owner: str | None = None) -> list[Subnet]:
        """
        List subnets

        Args:
            owner: Optional owner filter

        Returns:
            List of subnets
        """
        if owner:
            return await self.repository.find_by_owner(owner)
        return await self.repository.find_all()

    async def list_public_subnets(self) -> list[Subnet]:
        """
        List all public subnets

        Returns:
            List of public subnets
        """
        return await self.repository.find_public_subnets()

    async def list_children(
        self,
        parent_subnet_id: str,
        *,
        requester_id: str | None = None,
    ) -> list[Subnet]:
        """
        Return the immediate children of a subnet (ADR-0003).

        ACL alignment with ``list_subnets``: private children where
        ``requester_id`` is not the owner and not a member are
        filtered out. Anonymous callers (``requester_id is None``)
        see only public children. Cross-tenant probes return the
        same empty-list shape as legitimate "no children" results —
        no existence leak.

        Args:
            parent_subnet_id: Parent subnet identifier
            requester_id: Authenticated caller's ``agent_id`` (or
                ``None`` for anonymous / pre-auth contexts)

        Returns:
            List of visible child subnets. Empty when no children
            exist, the parent is unknown, or every child is filtered
            by ACL.
        """
        children = await self.repository.find_by_parent(parent_subnet_id)

        def _visible(subnet: Subnet) -> bool:
            if not subnet.is_private:
                return True
            if requester_id is None:
                return False
            return (
                subnet.owner == requester_id
                or requester_id in subnet.member_agent_ids
            )

        return [c for c in children if _visible(c)]

    async def delete_subnet(self, subnet_id: str, owner: str) -> bool:
        """
        Delete a subnet, cascading to children when it has any
        (ADR-0003 §A invariant 4).

        Authorisation: the subnet's own ``owner`` may delete it; the
        ``"system"`` literal is the platform escape hatch (used by
        Phase 3's task-state cascade hook in ``task_service``).

        Cascade order: children are deleted first, then the parent.
        The repository's ``delete_with_children`` carries the
        backend-specific atomicity guarantee (ADR-0003 §A.4) —

        - **Postgres**: single transaction; any failure rolls back
          everything, leaving the original parent + all children in
          place. Caller observes a raised exception.
        - **Redis**: sequential best-effort; on partial failure the
          repository logs a breadcrumb and raises ``RuntimeError``
          *before* touching the parent. Children deleted prior to the
          failure stay deleted (Redis has no cross-call MULTI/EXEC).

        Both backends raise on partial failure, so callers cannot
        accidentally treat a partially-completed cascade as success.

        Args:
            subnet_id: Subnet identifier
            owner: Owner identifier (or ``"system"`` for cascade)

        Returns:
            True if deleted successfully

        Raises:
            SubnetNotFoundException: If subnet not found
            PermissionError: If owner doesn't match
            RuntimeError: On Redis partial-failure cascade
        """
        subnet = await self.get_subnet(subnet_id)

        # Authorization check — owner of the subnet, or platform.
        if subnet.owner != owner and owner != "system":
            raise PermissionError(f"Owner mismatch: {owner} != {subnet.owner}")

        # Prevent deletion of system subnets — they are platform
        # primitives. Cascade hook still uses ``owner="system"`` to
        # delete *child* subnets, which is fine (children are never
        # reserved per ADR-0003 §7).
        if subnet_id in ["public", "system"]:
            raise PermissionError(f"Cannot delete system subnet: {subnet_id}")

        # Cascade to children when this is a top-level subnet. Single-
        # layer cap guarantees ``find_by_parent`` doesn't itself need
        # to recurse. The repository's ``delete_with_children`` owns
        # the atomicity guarantee — service no longer loops over
        # individual ``delete()`` calls (issue #54).
        if subnet.parent_subnet_id is None:
            children = await self.repository.find_by_parent(subnet_id)
            if children:
                child_ids = [c.subnet_id for c in children]
                logger.info(
                    "delete_subnet_cascade",
                    parent_subnet_id=subnet_id,
                    child_subnet_ids=child_ids,
                    child_count=len(child_ids),
                )
                return await self.repository.delete_with_children(
                    subnet_id, child_ids
                )

        logger.info("delete_subnet", subnet_id=subnet_id)
        return await self.repository.delete(subnet_id)

    async def add_member(self, subnet_id: str, agent_id: str) -> Subnet:
        """
        Add an agent to a subnet.

        ADR-0003 §A invariant 2 — when the subnet is a child
        (``parent_subnet_id is not None``), the agent must already be
        a member of the parent. Surfaced with
        ``reason="not_parent_member"``.

        Args:
            subnet_id: Subnet identifier
            agent_id: Agent identifier

        Returns:
            Updated subnet entity

        Raises:
            SubnetNestingError: If subnet is a child and ``agent_id``
                is not a member of the parent.
        """
        subnet = await self.get_subnet(subnet_id)

        if subnet.parent_subnet_id is not None:
            parent = await self.repository.find_by_id(subnet.parent_subnet_id)
            # If the parent has been deleted while this child still
            # exists (orphan), reject the add — adding members to a
            # dangling child would silently bypass the subset
            # invariant. Ops should ``delete_subnet`` the orphan.
            if parent is None or agent_id not in parent.member_agent_ids:
                raise SubnetNestingError(
                    REASON_NOT_PARENT_MEMBER,
                    f"Agent '{agent_id}' is not a member of parent subnet "
                    f"'{subnet.parent_subnet_id}'",
                )

        subnet.add_member(agent_id)
        await self.repository.save(subnet)
        logger.info("subnet_member_added", subnet_id=subnet_id, agent_id=agent_id)
        return subnet

    async def remove_member(self, subnet_id: str, agent_id: str) -> Subnet:
        """
        Remove an agent from a subnet

        Args:
            subnet_id: Subnet identifier
            agent_id: Agent identifier

        Returns:
            Updated subnet entity
        """
        subnet = await self.get_subnet(subnet_id)
        subnet.remove_member(agent_id)
        await self.repository.save(subnet)
        logger.info("subnet_member_removed", subnet_id=subnet_id, agent_id=agent_id)
        return subnet

    async def get_member_count(self, subnet_id: str) -> int:
        """
        Get number of members in a subnet

        Args:
            subnet_id: Subnet identifier

        Returns:
            Number of members
        """
        subnet = await self.get_subnet(subnet_id)
        return subnet.get_member_count()

    async def exists(self, subnet_id: str) -> bool:
        """
        Check if subnet exists

        Args:
            subnet_id: Subnet identifier

        Returns:
            True if subnet exists
        """
        return await self.repository.exists(subnet_id)

    async def update_harness(
        self,
        subnet_id: str,
        owner: str,
        harness_url: str | None,
        harness_secret: str | None,
    ) -> Subnet:
        """
        Register or update the Org Harness webhook for a subnet.

        Only the subnet owner may register a harness. Pass ``harness_url=None``
        to unregister (subnet will fall back to platform-level webhook only).
        ``harness_secret`` is stored as-is and used to HMAC-sign outgoing
        webhook payloads delivered to ``harness_url``.

        Args:
            subnet_id: Subnet identifier
            owner: Authenticated agent making the request (for authz)
            harness_url: External webhook URL (or None to clear)
            harness_secret: HMAC secret (optional; None disables signing)

        Returns:
            Updated subnet entity

        Raises:
            SubnetNotFoundException: If subnet not found
            PermissionError: If owner mismatch
        """
        subnet = await self.get_subnet(subnet_id)

        if subnet.owner != owner and owner != "system":
            raise PermissionError(f"Owner mismatch: {owner} != {subnet.owner}")

        subnet.harness_url = harness_url
        subnet.harness_secret = harness_secret
        await self.repository.save(subnet)
        logger.info(
            "subnet_harness_updated",
            subnet_id=subnet_id,
            has_url=harness_url is not None,
            has_secret=harness_secret is not None,
        )
        return subnet

    async def promote_to_persistent(
        self,
        subnet_id: str,
        owner: str,
    ) -> Subnet:
        """
        Promote a ``task_scoped`` child subnet to ``persistent``
        (ADR-0003 semantic decision #2).

        Owner-only. **Idempotent** — calling on a subnet that is
        already ``persistent`` returns it unchanged (no error, no
        repository write). Per ADR semantic decision #4, the owner
        is *not* required to currently be a member of the parent
        subnet; promote is a pure field flip authorised by
        owner-only ACL.

        Side effects on success:
        - ``lifecycle`` → ``"persistent"``
        - ``linked_task_id`` → ``None``
        - Repository ``save`` re-emits the secondary index update
          (Redis pipeline SREM's the old ``by_linked_task`` entry).

        Args:
            subnet_id: Subnet identifier
            owner: Authenticated agent making the request

        Returns:
            Updated subnet entity (or the unchanged entity on
            idempotent invocation).

        Raises:
            SubnetNotFoundException: If subnet not found
            PermissionError: If owner mismatch
        """
        subnet = await self.get_subnet(subnet_id)

        if subnet.owner != owner and owner != "system":
            raise PermissionError(f"Owner mismatch: {owner} != {subnet.owner}")

        if subnet.lifecycle == "persistent":
            # Idempotent — return unchanged. No repository write so
            # callers can promote unconditionally without a
            # precondition check, per ADR semantic decision #2.
            logger.info(
                "subnet_promote_noop",
                subnet_id=subnet_id,
                owner=owner,
                reason="already_persistent",
            )
            return subnet

        # Build the promoted entity via ``dataclasses.replace`` so the
        # final state is re-validated by ``Subnet.__post_init__``
        # (catches any future invariant additions automatically).
        # Direct attribute assignment would silently bypass
        # ``__post_init__`` and only persist a half-valid state if a
        # bug ever set the fields out of order.
        promoted = dataclasses.replace(
            subnet,
            lifecycle="persistent",
            linked_task_id=None,
        )
        await self.repository.save(promoted)
        logger.info(
            "subnet_promoted_to_persistent",
            subnet_id=subnet_id,
            owner=owner,
        )
        return promoted
