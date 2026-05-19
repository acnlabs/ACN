"""Subnet Join Request Repository Interface (ADR-0004 Phase 2 Slice 2.1).

Storage contract for the three-in-one ``subnet_join_requests``
table. The interface is intentionally minimal at Slice 2.1: just
the CRUD primitives Slice 2.2's ``JoinFlowService`` needs to wire
the state machine on top. State-transition orchestration (CAS on
``status='pending'``, webhook emission, etc.) lives at the service
boundary, not here.

Why no ``upsert`` / ``transition`` primitive on the interface
-------------------------------------------------------------
Both backends (Postgres and Redis) need different atomic envelopes
to honour the "at most one pending per (subnet_id, agent_id)
across all kinds" invariant — Postgres uses the
``UNIQUE … WHERE status='pending'`` partial index; Redis uses a
Lua script around the SETNX+HSET pair (ADR §"Redis layout and
atomicity"). Hiding both under a single
``IRepo.transition_to_approved`` method would force one of the
two implementations to do something contorted. The service layer
calls a small set of repo primitives (``save_pending``,
``mark_decided``, ``find_pending_for``) and composes them; each
backend picks the appropriate atomic primitive internally.

Cascade contract
----------------
``delete_for_subnet`` is the cascade hook called from
``SubnetService.delete_subnet``. ADR §"Cascade deletion" requires
the delete to be atomic with the join_requests + allowlist +
subnet deletion in Postgres (single transaction) and best-effort
sequential in Redis (raise ``RuntimeError`` BEFORE touching the
subnet HASH on partial failure). The interface only exposes the
``delete_for_subnet`` primitive; the cascade orchestration is the
service's responsibility.
"""

from abc import ABC, abstractmethod
from typing import Any

from ..entities import SubnetJoinRequest


class ISubnetJoinRequestRepository(ABC):
    """Abstract contract for ``subnet_join_requests`` persistence."""

    @abstractmethod
    async def save(self, request: SubnetJoinRequest) -> None:
        """Insert a new row or overwrite an existing one by ``request_id``.

        Implementations MAY enforce the "at most one pending per
        (subnet_id, agent_id)" invariant at this layer (Postgres
        partial index raises ``IntegrityError``; Redis Lua raises
        a custom exception). The service layer wraps the raised
        error and surfaces ``409 JOIN_REQUEST_PENDING`` /
        ``409 INVITATION_PENDING`` accordingly.

        ``save`` is the only mutation primitive — there is no
        ``transition`` method because state transitions are
        modelled as "construct a new entity with the target
        status + decided_by + decided_at, then save". This keeps
        the entity layer the single source of truth for legal
        shapes; the repo never rewrites individual fields.
        """

    @abstractmethod
    async def find_by_id(self, request_id: str) -> SubnetJoinRequest | None:
        """Look up a single row by primary key.

        Returns ``None`` if no row exists. Does NOT filter on
        ``status`` — terminal rows remain queryable for audit.
        """

    @abstractmethod
    async def find_pending_for(
        self, subnet_id: str, agent_id: str
    ) -> SubnetJoinRequest | None:
        """Return the unique pending row for ``(subnet_id, agent_id)``, if any.

        The "at most one" invariant means this returns at most one
        row — Postgres enforces via the partial index, Redis via
        the reverse pending index
        ``acn:subnets:{s}:pending_by_agent:{a}``.

        Used by the §join branch table to detect
        "duplicate join request" / "pending invitation collides with
        self-join" / "join request collides with owner invitation"
        — branches 3, 5, and 6 in ADR §join.

        Returns ``None`` if no pending row exists (the agent is
        eligible to create a fresh request).
        """

    @abstractmethod
    async def list_by_subnet(
        self,
        subnet_id: str,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SubnetJoinRequest]:
        """List rows for one subnet, with optional ``kind`` / ``status`` filters.

        Used by the owner-facing
        ``GET /subnets/{s}/join-requests`` and
        ``GET /subnets/{s}/invitations`` endpoints. Both endpoints
        compose down to this single primitive — the route layer
        sets ``kind`` accordingly.

        Sort order: ``created_at DESC`` (most recent first). Pinned
        here so the SDK / CLI listing UI has a stable contract
        regardless of backend.
        """

    @abstractmethod
    async def list_pending_invitations_for_agent(
        self, agent_id: str
    ) -> list[SubnetJoinRequest]:
        """List the agent's pending invitations across all subnets.

        Powers the invitee-facing
        ``GET /agents/{a}/subnet-invitations`` endpoint. Filtered
        server-side to ``kind='invitation' AND status='pending'``
        — Postgres uses an index hint; Redis uses the dedicated
        per-agent SET ``acn:agents:{a}:subnet_invitations``.

        Sort order: ``created_at DESC``.
        """

    @abstractmethod
    async def delete_for_subnet(
        self, subnet_id: str, *, session: Any | None = None
    ) -> int:
        """Cascade-delete all rows for a subnet. Returns count deleted.

        Called by ``SubnetService.delete_subnet`` before deleting
        the subnet row itself. Postgres impl participates in the
        outer transaction; Redis impl iterates the listing index
        SET and deletes each request HASH + reverse-index key,
        raising ``RuntimeError`` on partial failure BEFORE the
        caller touches the subnet HASH.

        Returns the number of rows actually deleted (useful for
        audit logging; not used by the cascade control flow).

        Transaction participation
        -------------------------
        ``session`` is the opaque token yielded by
        :class:`IUnitOfWork`'s ``transaction()`` context manager
        (see ``acn/core/interfaces/unit_of_work.py``). Implementations
        that understand the token's runtime type MUST bind to it for
        the duration of the call (no internal commit, no internal
        close) so the outer Unit-of-Work owns the transaction
        boundary; implementations that don't understand the token
        (e.g. the Redis impl, which has no transaction model that
        composes with PG's ``AsyncSession``) MUST ignore it and
        keep their best-effort behaviour. Passing ``session=None``
        (the default) is the legacy path: the implementation opens
        and commits its own connection.
        """
