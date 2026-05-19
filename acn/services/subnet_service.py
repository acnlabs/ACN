"""Subnet Service

Business logic for subnet management.
"""

import dataclasses
from typing import Literal

import structlog  # type: ignore[import-untyped]

from ..core.entities import Subnet
from ..core.exceptions import SubnetNotFoundException
from ..core.interfaces import (
    IAgentRepository,
    ISubnetAllowlistRepository,
    ISubnetJoinRequestRepository,
    ISubnetRepository,
    IUnitOfWork,
)
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
# ADR-0004: explicit ``is_private=True`` + ``join_policy='open'`` combination
# (the historical "private but joinable by anyone who knows the id" gap that
# ADR-0004 closes). When the caller doesn't pass ``join_policy`` we silently
# default to ``'approval'`` on private subnets, so this reason only ever
# surfaces when the caller knowingly sends the conflicting combination.
REASON_VISIBILITY_POLICY_CONFLICT = "visibility_policy_conflict"


class SubnetInvariantError(ValueError):
    """Raised by ``SubnetService`` when a subnet construction-time
    invariant rejects a request. Carries a stable ``reason`` string
    (one of the ``REASON_*`` constants above) that the route layer
    surfaces as ``details.reason`` for client / CLI / SDK parsers.

    Previously named ``SubnetNestingError`` (ADR-0003 only raised
    nesting-related rejections through it). ADR-0004 broadened its
    scope to also carry ``visibility_policy_conflict``; future ADRs
    are expected to keep piling stable reasons onto this same class
    rather than fork it, so the route layer can keep a single
    ``_invariant_error_to_acn`` switch. The legacy name is still
    re-exported via the module ``__getattr__`` below with a
    ``DeprecationWarning``.

    Kept as a ``ValueError`` subclass so legacy callers that catch
    ``ValueError`` (e.g. ``routes/subnets.py::create_subnet``) keep
    working — they just see a richer payload than a bare message.
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


def __getattr__(name: str):
    """Module-level ``__getattr__`` for the deprecated
    ``SubnetNestingError`` alias (PEP 562).

    Resolving ``acn.services.subnet_service.SubnetNestingError`` —
    whether at import time (``from .subnet_service import
    SubnetNestingError``) or as an attribute access — returns the
    renamed :class:`SubnetInvariantError` and emits a
    ``DeprecationWarning`` so out-of-tree consumers see a one-cycle
    migration window before the alias is dropped.
    """
    if name == "SubnetNestingError":
        import warnings

        warnings.warn(
            "SubnetNestingError is deprecated; use SubnetInvariantError "
            "instead. The legacy alias will be removed in a future "
            "release.",
            DeprecationWarning,
            stacklevel=2,
        )
        return SubnetInvariantError
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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

    Optionally accepts an :class:`IAgentRepository` so
    ``delete_subnet`` can clear ``agent.subnet_ids`` back-references
    on every member before tearing down the subnet record (issue
    #56). Production wiring supplies one; legacy fixtures that omit
    it get the pre-#56 behaviour (stale agent-side dust survives
    the delete) — best-effort and backward-compatible.

    Optionally accepts an :class:`IUnitOfWork` so ``delete_subnet``
    can run its three-table cascade (``subnet_join_requests``,
    ``subnet_allowlist``, ``subnets``) inside one Postgres
    transaction (issue #75 / ADR-0004 §"Cascade deletion: Postgres").
    Slice 2.1.1 (this) wires the UoW machinery so the cascade is
    genuinely atomic the moment both
    ``subnet_join_request_repository`` and
    ``subnet_allowlist_repository`` are also wired — currently the
    cascade repos themselves stay unwired in production until
    Slice 2.2 lands the route surface that creates rows. Until
    then the UoW is opened around just the subnet DELETE itself
    (the cascade sweeps short-circuit when their repos are
    absent), exercising the atomicity machinery end-to-end before
    real rows show up. When the UoW is omitted (Redis-only
    deployments, legacy fixtures), the cascade falls back to the
    Slice-2.1 sequential-commit shape: each repo commits
    independently. ADR-0004 explicitly accepts the Redis
    asymmetry; the legacy fallback exists only so out-of-tree
    code that builds a bare ``SubnetService`` keeps working.
    """

    def __init__(
        self,
        subnet_repository: ISubnetRepository,
        task_repository: ITaskRepository | None = None,
        agent_repository: IAgentRepository | None = None,
        subnet_join_request_repository: ISubnetJoinRequestRepository | None = None,
        subnet_allowlist_repository: ISubnetAllowlistRepository | None = None,
        unit_of_work: IUnitOfWork | None = None,
    ):
        """
        Initialize Subnet Service

        Args:
            subnet_repository: Subnet repository implementation
            task_repository: Optional task repository — required only
                by the ``linked_task_not_found`` validation path in
                ``create_subnet``. Omit for legacy fixtures that
                don't exercise nesting.
            agent_repository: Optional agent repository — used by
                ``delete_subnet`` to remove the deleted subnet's id
                from each member's ``agent.subnet_ids`` set. Omit
                for legacy fixtures; production must supply one to
                avoid agent-side stale dust (issue #56).
            subnet_join_request_repository: Optional — used by
                ``delete_subnet`` to cascade-delete pending and
                terminal join-requests / invitations when a subnet
                is dissolved (ADR-0004 §"Cascade deletion"). Omit
                for legacy fixtures that predate Slice 2.1; the
                cascade silently skips when absent, leaving the
                join_requests rows as dust against a no-longer-
                existing subnet (cleaned by a future ops sweep).
                Production composition must wire it once Slice 2.2
                lands the route surface that actually creates rows.
            subnet_allowlist_repository: Optional — used by
                ``delete_subnet`` to cascade-delete admission
                allowlist entries. Same opt-in pattern as
                ``subnet_join_request_repository``.
            unit_of_work: Optional — when present, ``delete_subnet``
                runs the three cascade DELETEs (join_requests,
                allowlist, subnet) inside a single
                :meth:`IUnitOfWork.transaction` block, threading the
                yielded session token through each repo's ``session=``
                kwarg. When ``None``, ``delete_subnet`` falls back to
                the Slice-2.1 sequential-commit shape (each repo
                commits independently — see class docstring for the
                acceptable use cases of that fallback).
        """
        self.repository = subnet_repository
        self.task_repository = task_repository
        self.agent_repository = agent_repository
        self.join_request_repository = subnet_join_request_repository
        self.allowlist_repository = subnet_allowlist_repository
        self.unit_of_work = unit_of_work

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
        join_policy: Literal["open", "approval"] | None = None,
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

        ADR-0004 invariant on ``join_policy``:

        - When ``join_policy`` is omitted (the common case — legacy
          callers and clients that don't yet know about the field),
          the service infers it from ``is_private``: ``'approval'``
          for private subnets, ``'open'`` for public ones. This
          preserves backward compatibility and closes the
          ``private + open`` gap automatically for callers that just
          flip ``is_private=True``.
        - When the caller passes ``join_policy='open'`` together
          with ``is_private=True`` knowingly, the service rejects
          with ``visibility_policy_conflict`` rather than letting
          the entity's bare ``ValueError`` bubble up — the route
          layer's ``_invariant_error_to_acn`` already maps it to
          ``INVALID_REQUEST 400 {"details": {"reason":
          "visibility_policy_conflict"}}``.
        - The other three combinations (``public+open``,
          ``public+approval``, ``private+approval``) are accepted
          as-is and persisted on the entity.

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
            join_policy: ``"open"`` / ``"approval"`` / ``None``
                (default — inferred from ``is_private``)

        Returns:
            Created subnet entity

        Raises:
            ValueError: If subnet already exists
            SubnetInvariantError: On any of the five ADR-0003 invariant
                rejections, or the ADR-0004
                ``visibility_policy_conflict`` rejection.
        """
        # ADR-0002: reject the internal-service placeholder as owner.
        # Defence-in-depth — the route layer already enforces AgentApiKeyDep
        # so this guard should never fire in production; it catches internal
        # callers or future code changes that bypass the route layer.
        if owner == "backend@internal":
            raise ValueError(
                "ADR-0002: 'backend@internal' is not a valid subnet owner; "
                "register a service-account agent and create subnets through "
                "that agent's api key."
            )

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
            raise SubnetInvariantError(
                REASON_TASK_SCOPED_REQUIRES_LINKED_TASK,
                "lifecycle='task_scoped' requires linked_task_id",
            )

        if parent_subnet_id is not None:
            parent = await self.repository.find_by_id(parent_subnet_id)
            if parent is None:
                raise SubnetInvariantError(
                    REASON_PARENT_NOT_FOUND,
                    f"Parent subnet '{parent_subnet_id}' does not exist",
                )
            # ADR-0003 §A invariant 5 — reserved subnets cannot be
            # parents. Catch by ID *and* (defensively) by owner: the
            # ``system`` owner literal is the platform escape hatch
            # and any subnet under it is treated as platform-owned.
            if parent_subnet_id in {"public", "system"} or parent.owner == "system":
                raise SubnetInvariantError(
                    REASON_PARENT_IS_RESERVED,
                    f"Parent subnet '{parent_subnet_id}' is reserved",
                )
            # Single-layer cap — parent must itself be top-level.
            if parent.parent_subnet_id is not None:
                raise SubnetInvariantError(
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
                raise SubnetInvariantError(
                    REASON_LINKED_TASK_NOT_FOUND,
                    f"Linked task '{linked_task_id}' does not exist",
                )

        # --- ADR-0004 join_policy resolution ---------------------------
        # When the caller omits ``join_policy`` we infer it from
        # ``is_private`` — this preserves backward compatibility for
        # every existing client (CLI / SDK / tests) that doesn't yet
        # know about the field, and closes the ``private + open`` gap
        # automatically.
        #
        # When the caller explicitly passes the conflicting combination
        # we raise a structured ``SubnetInvariantError`` with a stable
        # ``reason`` token rather than letting the entity's bare
        # ``ValueError`` bubble up through the route's generic catch —
        # the route's existing ``_invariant_error_to_acn`` switch already
        # maps unknown reasons to ``INVALID_REQUEST 400``, so this gives
        # clients a clean parseable token instead of a free-form message.
        if join_policy is None:
            effective_join_policy: Literal["open", "approval"] = (
                "approval" if is_private else "open"
            )
        else:
            effective_join_policy = join_policy
            if is_private and effective_join_policy == "open":
                raise SubnetInvariantError(
                    REASON_VISIBILITY_POLICY_CONFLICT,
                    "is_private=True requires join_policy='approval' "
                    "(omit join_policy to let the service default it)",
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
            join_policy=effective_join_policy,
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
            join_policy=effective_join_policy,
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

        Join-policy artifact cascade (ADR-0004 §"Cascade deletion")
        runs immediately before the subnet record(s) are dropped:

        - When :class:`IUnitOfWork` was injected at construction
          (Postgres production wiring), the three cascade DELETEs
          (``subnet_join_requests``, ``subnet_allowlist``, and
          ``subnets``) are threaded through one
          :meth:`IUnitOfWork.transaction` block so they commit or
          roll back as a unit (issue #75 closed by Slice 2.1.1).
        - When no UoW is wired (Redis-only deployments, legacy
          fixtures), the cascade falls back to the Slice-2.1
          sequential-commit shape — each repo commits independently.
          ADR-0004 explicitly accepts the Redis asymmetry; the
          fallback is what test fixtures predating Slice 2.1.1 rely
          on, and it stays byte-for-byte the same as before so
          out-of-tree callers keep working.

        Agent back-reference cleanup (issue #56) deliberately stays
        OUTSIDE the transaction in both modes — agent-side
        ``subnet_ids`` mutations are best-effort by design (ADR-0001
        §dual-store membership) and folding them into the cascade
        transaction would couple agent-store reachability to subnet
        delete success.

        Args:
            subnet_id: Subnet identifier
            owner: Owner identifier (or ``"system"`` for cascade)

        Returns:
            True if deleted successfully

        Raises:
            SubnetNotFoundException: If subnet not found
            PermissionError: If owner doesn't match
            RuntimeError: Redis cascade path raised this after the
                ``delete_with_children_partial`` breadcrumb when a
                child delete returned ``False`` (parent preserved).
            sqlalchemy.exc.SQLAlchemyError: PG cascade path bubbles
                any DB error out of the UoW transaction after the
                whole transaction is rolled back — parent + every
                child preserved. Caller treats both backend-specific
                exception types as "cascade aborted, retry-safe".
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
        #
        # Issue #56: before removing any subnet record, clear the
        # ``subnet_id`` back-reference from each member's
        # ``agent.subnet_ids`` set. Doing this **before** the delete
        # means a partial-failure cascade leaves agents pointing at
        # subnets that still exist (recoverable) rather than at
        # subnets that have already been deleted (orphan dust).
        if subnet.parent_subnet_id is None:
            children = await self.repository.find_by_parent(subnet_id)
            if children:
                # Clear back-references for every child first, then
                # the parent. Order mirrors the cascade delete order
                # the repository will execute next.
                # Stays outside the UoW transaction by design — see
                # method docstring §"Agent back-reference cleanup".
                for child in children:
                    await self._clear_agent_back_references(
                        child.subnet_id, child.member_agent_ids
                    )
                await self._clear_agent_back_references(
                    subnet_id, subnet.member_agent_ids
                )
                child_ids = [c.subnet_id for c in children]
                logger.info(
                    "delete_subnet_cascade",
                    parent_subnet_id=subnet_id,
                    child_subnet_ids=child_ids,
                    child_count=len(child_ids),
                )
                return await self._run_cascade_delete(
                    subnet_id, subnet_to_delete_with=children
                )

        # No-cascade path: child subnet direct-delete OR top-level
        # subnet with zero children. Either way, clean up this one
        # subnet's back-references then drop the record.
        await self._clear_agent_back_references(
            subnet_id, subnet.member_agent_ids
        )
        logger.info("delete_subnet", subnet_id=subnet_id)
        return await self._run_cascade_delete(subnet_id, subnet_to_delete_with=None)

    async def _run_cascade_delete(
        self,
        subnet_id: str,
        *,
        subnet_to_delete_with: list[Subnet] | None,
    ) -> bool:
        """Run the join-policy + subnet DELETE batch.

        Branches on :attr:`unit_of_work` injection:

        - **UoW wired (PG production)**: opens one
          :meth:`IUnitOfWork.transaction`, threads the yielded
          session into the join_requests + allowlist + subnet
          DELETEs via their ``session=`` kwarg, and lets the UoW
          commit-on-clean-exit / rollback-on-exception envelope
          decide the batch's fate. This is the ADR-0004 §"Cascade
          deletion: Postgres" promise.
        - **UoW absent (Redis or legacy)**: calls each cascade
          method without a session — the Slice-2.1 sequential-commit
          path. Redis impls ignore the ``session`` kwarg either way;
          legacy fixtures get the pre-Slice-2.1.1 behaviour
          unchanged.

        ``subnet_to_delete_with`` distinguishes the two delete
        shapes:

        - ``None`` → single subnet (no children, OR a child subnet
          deleted directly). Final DELETE is
          ``self.repository.delete(subnet_id, session=...)``.
        - non-empty list → parent with children. Final DELETE is
          ``self.repository.delete_with_children(parent_id,
          [c.subnet_id for c in children], session=...)``. The
          join-policy artifact sweep runs for every child AND the
          parent inside the same transaction.
        """
        if self.unit_of_work is not None:
            async with self.unit_of_work.transaction() as session:
                return await self._cascade_delete_body(
                    subnet_id, subnet_to_delete_with, session=session
                )
        return await self._cascade_delete_body(
            subnet_id, subnet_to_delete_with, session=None
        )

    async def _cascade_delete_body(
        self,
        subnet_id: str,
        subnet_to_delete_with: list[Subnet] | None,
        *,
        session: object | None,
    ) -> bool:
        """Inner body shared by atomic and legacy cascade paths.

        Order — strictly join_requests → allowlist → subnets, for
        every cascaded subnet (children first, then parent). The
        order is cosmetic inside a single PG transaction (everything
        commits together) but it matters in Redis-only / legacy
        paths: if the join_request cascade raises ``RuntimeError``
        (Redis partial failure), the subnet records stay in place so
        the cascade can be retried.
        """
        if subnet_to_delete_with is not None:
            children = subnet_to_delete_with
            child_ids = [c.subnet_id for c in children]
            for child in children:
                await self._cascade_join_policy_artifacts(
                    child.subnet_id, session=session
                )
            await self._cascade_join_policy_artifacts(subnet_id, session=session)
            return await self.repository.delete_with_children(
                subnet_id, child_ids, session=session
            )
        await self._cascade_join_policy_artifacts(subnet_id, session=session)
        return await self.repository.delete(subnet_id, session=session)

    async def _cascade_join_policy_artifacts(
        self, subnet_id: str, *, session: object | None = None
    ) -> None:
        """Sweep ADR-0004 join_requests + allowlist for ``subnet_id``.

        Called from ``_cascade_delete_body`` BEFORE the subnet
        HASH / row delete. Either repository raising
        ``RuntimeError`` (Redis partial failure) propagates here
        and aborts the caller's subnet delete — exactly the
        behaviour ADR §"Cascade deletion" requires so a
        half-cascade isn't treated as success.

        Silent no-op when either repo wasn't wired (legacy
        fixtures predating Slice 2.1) — same opt-in pattern
        ``_clear_agent_back_references`` uses for
        ``agent_repository``. Production composition wires both
        once Slice 2.2 starts creating rows; until then,
        ``delete_subnet`` retains its pre-Slice-2.1 behaviour.

        Order: join_requests first, allowlist second. The order
        matches the ADR §"Cascade deletion: Postgres" example
        statement order, though for the PG-with-UoW path it's
        cosmetic (both DELETEs commit together with the subnet
        DELETE on UoW exit); in Redis or legacy paths they're
        independent passes and the order matters for retry
        recoverability.

        ``session``: the opaque :class:`IUnitOfWork` token threaded
        through from ``_run_cascade_delete``. ``None`` for legacy
        / Redis paths (each repo manages its own session and
        commits independently); a real token (currently
        ``AsyncSession`` from ``PostgresUnitOfWork``) for the
        atomic PG path — bound to ``session=`` of both repo
        methods so they join the outer transaction.
        """
        if self.join_request_repository is not None:
            deleted = await self.join_request_repository.delete_for_subnet(
                subnet_id, session=session
            )
            if deleted:
                logger.info(
                    "delete_subnet_cascade_join_requests",
                    subnet_id=subnet_id,
                    deleted_count=deleted,
                )
        if self.allowlist_repository is not None:
            deleted = await self.allowlist_repository.delete_for_subnet(
                subnet_id, session=session
            )
            if deleted:
                logger.info(
                    "delete_subnet_cascade_allowlist",
                    subnet_id=subnet_id,
                    deleted_count=deleted,
                )

    async def _clear_agent_back_references(
        self,
        subnet_id: str,
        member_agent_ids: set[str],
    ) -> None:
        """Remove ``subnet_id`` from each member's
        ``agent.subnet_ids`` set (issue #56).

        Best-effort with a structured summary log: per-agent failure
        is logged at ``warning`` level and **does not** abort the
        caller (the subnet delete still proceeds). This matches the
        existing weak-atomicity profile of the dual-store membership
        invariant (ADR-0001) — agent-side dust is "harmless" but
        amplifies under cascade, so we clean it on a best-effort
        basis instead of layering a distributed transaction.

        Silent no-op when:

        - ``agent_repository`` was not wired (legacy test fixtures)
        - ``member_agent_ids`` is empty (orphan or just-created subnet)
        - An individual agent no longer exists (already deleted)
        - The agent doesn't carry ``subnet_id`` in its ``subnet_ids``
          (already cleaned by an earlier explicit ``leave_subnet``)

        Args:
            subnet_id: Subnet being deleted; this id will be removed
                from every visited agent's ``subnet_ids``.
            member_agent_ids: Snapshot of the subnet's members at
                the moment of delete. May contain ids whose agents
                no longer exist — they're skipped.
        """
        if self.agent_repository is None or not member_agent_ids:
            return

        cleaned = 0
        failed = 0
        for agent_id in member_agent_ids:
            try:
                agent = await self.agent_repository.find_by_id(agent_id)
                if agent is None:
                    continue
                if subnet_id not in agent.subnet_ids:
                    continue
                agent.remove_from_subnet(subnet_id)
                await self.agent_repository.save(agent)
                cleaned += 1
            except Exception as exc:  # noqa: BLE001 — best-effort
                # Per-agent failure must not abort the cascade. Log
                # enough context that ops can re-run a manual
                # ``leave_subnet`` for the affected pair.
                logger.warning(
                    "subnet_back_reference_cleanup_failed",
                    subnet_id=subnet_id,
                    agent_id=agent_id,
                    error=str(exc),
                )
                failed += 1

        logger.info(
            "subnet_back_reference_cleanup",
            subnet_id=subnet_id,
            member_count=len(member_agent_ids),
            cleaned=cleaned,
            failed=failed,
        )

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
            SubnetInvariantError: If subnet is a child and ``agent_id``
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
                raise SubnetInvariantError(
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
