"""Allowlist Repository Interface (Phase 2 PR #2).

Storage contract for ``communication_policy.mode=allowlist`` membership.
The allowlist is a per-recipient trust list: when the recipient's
policy is ``allowlist``, only senders whose ``agent_id`` appears in
``targets_of(recipient_id)`` reach the recipient's inbox; everyone
else is diverted into the manifest queue (the same divert path
established by PR #1).

Design notes:

* Two-layer persistence is intentional. Postgres is the source of
  truth (durable, foreign-keyed to ``agents.agent_id`` so cascade
  cleans up automatically); Redis SET is a 30-second cache for the
  hot ``is_member`` lookup that fires on every inbound check under
  allowlist mode. The 30s TTL is the fail-safe: write paths SADD/SREM
  the cache synchronously, but if a write skips the cache (process
  crash, network blip), the next miss will re-read PG and rebuild.

* "List" repos in this codebase typically split read/write across PG
  and Redis (see registry / billing); ``IAllowlistRepository``
  follows that pattern but is a single interface — concrete classes
  ``PostgresAllowlistRepository`` and ``RedisAllowlistRepository``
  each implement the subset that makes sense for them, and the
  service composes both. We do NOT define a ``Combined`` adapter at
  the interface layer — that complexity belongs in
  ``AllowlistService``, where the dual-write ordering rule lives.

* The ``is_member`` method reads cache-first / PG-fallback (the
  "read-through" behaviour). It is the **only** read path on the
  hot inbound check, so its performance dominates allowlist mode's
  throughput; PG fallback is the cold-start / TTL-expiry path
  with implicit cache rebuild as a side-effect.

* ``count_for_owner`` is split out from ``list_targets`` because the
  capacity check at write time only needs the count — keeping it
  separate lets us avoid a SELECT * round-trip on every add.
"""

from abc import ABC, abstractmethod
from datetime import datetime


class IAllowlistRepository(ABC):
    """Abstract contract for the per-recipient allowlist trust set."""

    @abstractmethod
    async def add(
        self,
        owner_id: str,
        target_id: str,
        reason: str | None = None,
    ) -> bool:
        """Insert (owner_id, target_id) into the trust list.

        Implementations are responsible for the storage primitive
        (PG INSERT ... ON CONFLICT DO NOTHING for the durable side;
        Redis SADD for the cache side). The composing
        ``AllowlistService`` orchestrates the dual-write ordering
        — this interface does NOT specify which layer to write
        first; that contract lives at the service boundary because
        it differs between add (PG → Redis) and remove (Redis → PG)
        for safety reasons (see ``AllowlistService`` docstring).

        Args:
            owner_id: Recipient agent that owns the trust list.
            target_id: Sender agent being added to ``owner_id``'s
                allowlist. The service has already verified
                ``target_id`` exists in ``agents``.
            reason: Optional free-form note (≤ 200 chars). Stored
                for the owner's UI listing only; never visible to
                ``target_id``.

        Returns:
            True if a NEW row/member was inserted; False if it
            already existed (idempotent path — the route layer
            still responds 200 and the service layer skips the
            capacity check).
        """

    @abstractmethod
    async def remove(self, owner_id: str, target_id: str) -> bool:
        """Drop (owner_id, target_id) from the trust list.

        Returns:
            True if a row/member was actually removed, False if it
            didn't exist (idempotent — repeat-DELETE returns 200).
        """

    @abstractmethod
    async def is_member(self, owner_id: str, target_id: str) -> bool:
        """Hot-path check: does ``target_id`` belong to ``owner_id``'s allowlist?

        Implementations MAY trigger a cache rebuild as a side-effect
        on miss (the Redis impl does — see
        ``RedisAllowlistRepository.is_member``); the PG impl does
        not because it has no cache to rebuild.

        Returns:
            True if member; False otherwise.

        Raises:
            Implementation-defined exceptions on durable storage
            failure. The composing service decides the fail policy
            (PR #2 plan P0-3: fail-closed → divert to manifest).
        """

    @abstractmethod
    async def list_targets(
        self,
        owner_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list["AllowlistEntry"]:
        """Return entries in ``owner_id``'s allowlist (most-recent first)."""

    @abstractmethod
    async def count_for_owner(self, owner_id: str) -> int:
        """Number of entries in ``owner_id``'s allowlist. O(1) on both layers."""


class AllowlistEntry:
    """A single allowlist row, returned by ``list_targets``.

    Plain attributes (not a Pydantic model) so the repository layer
    has no Pydantic dep — the service layer is free to wrap into a
    response shape if needed. Equality / ordering are not defined;
    callers iterate.
    """

    __slots__ = ("target_id", "created_at", "reason")

    def __init__(
        self,
        target_id: str,
        created_at: datetime,
        reason: str | None = None,
    ) -> None:
        self.target_id = target_id
        self.created_at = created_at
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover (cosmetic)
        return (
            f"AllowlistEntry(target_id={self.target_id!r}, "
            f"created_at={self.created_at!r}, reason={self.reason!r})"
        )
