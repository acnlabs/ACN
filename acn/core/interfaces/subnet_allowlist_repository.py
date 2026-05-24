"""Subnet Admission Allowlist Repository Interface (ADR-0004 Phase 2 Slice 2.1).

Storage contract for the per-subnet admission allowlist —
preauthorised ``(subnet_id, agent_id)`` pairs that the §join flow
checks (branch 4 in ADR §join) before falling through to the
request/invitation path.

Naming note
-----------
This is **distinct from** ``IAllowlistRepository`` in
``acn/core/interfaces/allowlist_repository.py``, which governs
agent-to-agent **communication** under
``communication_policy.mode=allowlist``. ADR-0004 retains the
"allowlist" term because both flows share the semantic shape
("preauthorised members of a trust set"), but the ``Subnet``
prefix makes the namespace unambiguous in code; error messages
should say "subnet allowlist" / "communication allowlist" to
keep operators clear.

Why no ``is_member`` hot-path optimisation here
-----------------------------------------------
``IAllowlistRepository`` (the agent-comm one) exposes a Redis-
cached ``is_member`` because every inbound message triggers a
check. The subnet allowlist is consulted only on the ``join``
endpoint — a cold path, hit once per ``(subnet, agent)`` in the
agent's entire lifetime. No cache layer needed; PG SELECT is
fast enough. The Redis implementation here is just a mirror for
the dual-store reads-from-either pattern the rest of ACN uses,
not a performance cache.
"""

from abc import ABC, abstractmethod
from typing import Any

from ..entities import SubnetAllowlist


class ISubnetAllowlistRepository(ABC):
    """Abstract contract for ``subnet_allowlist`` persistence."""

    @abstractmethod
    async def add(self, entry: SubnetAllowlist) -> bool:
        """Insert ``(subnet_id, agent_id)`` into the allowlist.

        Idempotent: re-adding an existing pair is a no-op. The
        boolean return distinguishes the two cases so the route
        layer can pick 201 (new) vs 200 (already present) per
        ADR §HTTP status code conventions:

        Returns:
            True if a new row was inserted.
            False if the pair already existed (idempotent path).
        """

    @abstractmethod
    async def remove(self, slug: str, agent_id: str) -> bool:
        """Drop ``(subnet_id, agent_id)`` from the allowlist.

        Idempotent: removing an absent pair is a no-op. Removing
        an entry does NOT evict an already-joined member — ADR
        §State machine edges ("Allowlist removal does not evict
        members") makes this an explicit non-invariant; the
        service layer must not call ``remove_member`` from this
        path.

        Returns:
            True if a row was actually removed.
            False if the pair didn't exist.
        """

    @abstractmethod
    async def is_member(self, slug: str, agent_id: str) -> bool:
        """Check whether ``agent_id`` is on ``subnet_id``'s allowlist.

        Cold-path consultation point for the §join flow's branch 4.
        Returns ``False`` for both "not on allowlist" and "subnet
        doesn't exist" — the route layer has already verified
        subnet existence by this point.
        """

    @abstractmethod
    async def list_for_subnet(
        self, slug: str, *, limit: int = 100, offset: int = 0
    ) -> list[SubnetAllowlist]:
        """List allowlist entries for one subnet, most-recent first.

        Powers ``GET /subnets/{s}/allowlist``. Returns an empty
        list if the subnet has no entries (or doesn't exist; the
        existence check is the route's responsibility).
        """

    @abstractmethod
    async def delete_for_subnet(
        self, slug: str, *, session: Any | None = None
    ) -> int:
        """Cascade-delete all entries for a subnet. Returns count deleted.

        Called by ``SubnetService.delete_subnet`` before deleting
        the subnet row itself. Symmetric with
        ``ISubnetJoinRequestRepository.delete_for_subnet`` — see
        that interface's docstring for the cascade ordering /
        atomicity contract.

        Transaction participation
        -------------------------
        ``session`` is the opaque :class:`IUnitOfWork` token. Same
        contract as :meth:`ISubnetJoinRequestRepository.delete_for_subnet`:
        Postgres impl binds to it (no internal commit / close);
        Redis impl ignores it; ``None`` (default) is the legacy
        path with self-managed session + commit.
        """
