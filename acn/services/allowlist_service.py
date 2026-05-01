"""Allowlist Service (Phase 2 PR #2).

Owns the dual-write ordering rule and capacity / idempotency rules
for ``communication_policy.mode=allowlist`` membership.

The repository layer (PG + Redis) is intentionally split: each one
is atomic for itself only. This service is the single place that
reasons about partial failure and decides which side to write
first. The rule is asymmetric on purpose:

* **add** → PG INSERT first, then Redis SADD.
  Worst case (PG ok, Redis fails): cache momentarily lacks the
  member; next ``is_member`` cache miss reloads from PG and the
  member appears. **Safety direction**: a brief gap where a
  trusted sender is *not* yet trusted on the cache → message
  diverts to manifest. Inconvenient, recoverable.

* **remove** → Redis SREM first, then PG DELETE.
  Worst case (Redis ok, PG fails): cache no longer has member;
  next ``is_member`` cache miss reloads from PG → member
  reappears in cache. The user-visible effect is "removal didn't
  stick". The route layer surfaces the PG failure as 5xx so the
  client retries. **Critical safety property**: at no point can
  the cache report a *removed* sender as still trusted — that
  would let a freshly-disinvited bad actor keep delivering for up
  to 30s of cache TTL. Reversing the order (PG first) would open
  exactly that hole.

Self-allowlisting is rejected in line with self-following: it has
no semantic meaning (sender == recipient always passes via the
"open" branch since it doesn't go through the network) and would
clutter the audit surface.

Existence checks for ``target_id`` mirror ``FollowService.follow``:
404 if the target isn't a registered agent. The privacy concern
("does adding a non-existent target leak agent existence?") is
mitigated by the owner-only auth on the API — only the owner can
add to their own allowlist, and they can't enumerate other
agents' existence by adding their own targets.
"""

from __future__ import annotations

import structlog  # type: ignore[import-untyped]

from ..core.exceptions import (
    AgentNotFoundException,
    AllowlistCapacityExceededError,
    SelfAllowlistError,
)
from ..core.interfaces import (
    AllowlistEntry,
    IAgentRepository,
    IAllowlistRepository,
)

logger = structlog.get_logger()


# Per-owner ceiling on allowlist size. The proposal documents 500
# (see "原型 PR #2 必验风险点 #5"); kept generous enough that
# legitimate trust lists (a few dozen partners + close friends) are
# nowhere near it, while bounding worst-case PG row count and
# Redis SET memory per agent. With 500 short-string members a SET
# is ~10 KB; SADD/SREM stay O(1).
#
# Defence-in-depth: a Postgres trigger (``trg_agent_allowlist_capacity``,
# migration ``f6a7b8c9d0e1``) re-applies the same cap inside a per-
# owner advisory lock so concurrent ``add()`` cannot race past the
# service-layer pre-check. If you bump this constant, also bump the
# ``cap`` literal inside the trigger body — the two MUST stay in sync.
MAX_ALLOWLIST_SIZE: int = 500

# ``reason`` field cap. Surfaced in owner UI listing; capped to
# avoid a giant note bloating PG rows or response payloads. Mirrors
# ``MAX_REJECT_REASON_LEN`` in policy_service.py — same UX tier
# (free-form note, owner-supplied).
MAX_REASON_LEN: int = 200


# Re-exported so existing call sites that historically imported from
# ``acn.services.allowlist_service`` keep working without touching
# every caller — the canonical home is ``acn.core.exceptions`` now.
__all__ = [
    "AllowlistCapacityExceededError",
    "AllowlistService",
    "MAX_ALLOWLIST_SIZE",
    "MAX_REASON_LEN",
    "SelfAllowlistError",
]


class AllowlistService:
    """Orchestrates allowlist add/remove/list/membership-check.

    Args:
        pg_repo: Postgres source-of-truth repository.
        redis_repo: Redis cache repository — separate so the dual-
            write order is explicit. Kept as ``Optional`` (None) for
            tests that exercise PG-only paths, mirroring the
            policy_service / manifest_dispatcher rollout-opt-out
            pattern.
        agent_repository: For ``target_id`` existence checks
            (mirrors ``FollowService``).
    """

    def __init__(
        self,
        pg_repo: IAllowlistRepository,
        redis_repo: IAllowlistRepository | None,
        agent_repository: IAgentRepository,
    ):
        self.pg_repo = pg_repo
        self.redis_repo = redis_repo
        self.agent_repository = agent_repository

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def add(
        self,
        owner_id: str,
        target_id: str,
        reason: str | None = None,
    ) -> bool:
        """Add ``target_id`` to ``owner_id``'s allowlist.

        Order of operations (see module docstring for the why):
            1. Pre-flight checks (self-allowlist, target existence,
               capacity). Cheapest-first so a missing target never
               consumes a capacity slot.
            2. PG INSERT ... ON CONFLICT DO NOTHING.
            3. Redis SADD + EXPIRE refresh (best-effort; logged on
               failure, never raised — the next is_member miss
               will rebuild).

        Returns:
            True if a new edge was created; False if it already
            existed (idempotent path).

        Raises:
            SelfAllowlistError: ``owner_id == target_id``.
            AgentNotFoundException: ``target_id`` doesn't exist.
            AllowlistCapacityExceededError: owner already at
                ``MAX_ALLOWLIST_SIZE`` and target is NOT already on
                the list.
        """
        if owner_id == target_id:
            raise SelfAllowlistError(
                "An agent cannot add itself to its own allowlist"
            )

        if not await self.agent_repository.exists(target_id):
            # Same shape as FollowService: 404 if target unknown.
            # Privacy is preserved because this endpoint is
            # owner-only; an attacker can only enumerate against
            # *their own* allowlist additions, which doesn't gain
            # them the existence info they couldn't already get.
            raise AgentNotFoundException(
                f"Agent {target_id} not found"
            )

        # Cap reason BEFORE persistence so the PG row stays small
        # even if the route layer somehow forgets to clip.
        if reason is not None:
            reason = reason[:MAX_REASON_LEN]

        # Capacity check — done BEFORE the INSERT so we never accept
        # the 501-th edge and then race to remove it. Idempotent
        # re-add path bypasses the cap (already-existing target
        # doesn't grow the list).
        already_member = await self.pg_repo.is_member(owner_id, target_id)
        if not already_member:
            current = await self.pg_repo.count_for_owner(owner_id)
            if current >= MAX_ALLOWLIST_SIZE:
                raise AllowlistCapacityExceededError(
                    f"Allowlist capacity reached ({MAX_ALLOWLIST_SIZE}); "
                    f"remove some entries first"
                )

        created = await self.pg_repo.add(owner_id, target_id, reason=reason)

        # Redis SADD is best-effort — log + carry on. If it fails,
        # the next is_member cache miss will rebuild from PG and
        # the member will appear. Never raise to the caller for
        # cache-side failures: PG is already committed, the user
        # has the durable guarantee.
        if self.redis_repo is not None:
            try:
                await self.redis_repo.add(owner_id, target_id, reason=reason)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "allowlist_cache_sync_failed",
                    op="add",
                    owner_id=owner_id,
                    target_id=target_id,
                    error=str(exc),
                )

        if created:
            logger.info(
                "allowlist_added",
                owner_id=owner_id,
                target_id=target_id,
            )
        return created

    async def remove(self, owner_id: str, target_id: str) -> bool:
        """Drop ``target_id`` from ``owner_id``'s allowlist.

        Order of operations is the **inverse** of ``add`` — see
        module docstring for the safety reasoning. Briefly: cache
        SREM first ensures no window where a freshly-revoked
        sender keeps getting trusted via stale cache.

            1. Redis SREM (best-effort; cache already inconsistent
               post-step 2 only matters if step 2 fails — see below).
            2. PG DELETE ... WHERE.

        Returns:
            True if a row was actually removed, False if it didn't
            exist (idempotent path — repeat-DELETE returns 200).

        Failure handling:
        - SREM fails before DELETE → the cache may still have the
          member; we still try DELETE so the durable side commits.
          Next is_member miss will reload from PG (post-delete) and
          drop the cache entry. Brief safety window: until the next
          miss, cache says "trusted" while PG says "removed". The
          30s TTL caps that window.
        - DELETE fails after SREM succeeded → cache no longer has
          the member but PG still does. Next is_member rebuild will
          re-add it. The route surfaces the PG failure to the
          client; user retries. Safety property holds: at no point
          is a "real" removed sender treated as trusted via cache,
          because cache no longer has the member.
        """
        if self.redis_repo is not None:
            try:
                await self.redis_repo.remove(owner_id, target_id)
            except Exception as exc:  # noqa: BLE001
                # Best-effort: continue to PG DELETE so the durable
                # side wins. The 30s TTL is the safety net for a
                # stuck cache.
                logger.warning(
                    "allowlist_cache_remove_failed",
                    owner_id=owner_id,
                    target_id=target_id,
                    error=str(exc),
                )

        removed = await self.pg_repo.remove(owner_id, target_id)
        if removed:
            logger.info(
                "allowlist_removed",
                owner_id=owner_id,
                target_id=target_id,
            )
        return removed

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def is_member(self, owner_id: str, target_id: str) -> bool:
        """Hot-path inbound check. Cache-first, PG-fallback on miss.

        Used by ``PolicyCheckService.check_inbound`` under
        ``mode=allowlist``. The wiring layer
        (``MessageRouter`` / ``SubnetManager``) injects this method
        as a lazy callback so the policy service stays a pure
        function (PR #2 plan P0-2 decision).

        Falls back to PG directly when ``redis_repo`` is None
        (rollout-opt-out / test fixture path) — this is the same
        defensive pattern as policy_service.py = None.

        Failure modes (relevant to PR #2 plan P0-3):
        - Redis or PG read raises → exception propagates. The
          caller (router) catches and applies fail-closed: divert
          to manifest. We do NOT swallow here — consistent with
          policy_service's "raise on configuration error" stance,
          fail-closed is enforced at the router layer where the
          decision actually matters.
        """
        if self.redis_repo is not None:
            return await self.redis_repo.is_member(owner_id, target_id)
        return await self.pg_repo.is_member(owner_id, target_id)

    async def list_targets(
        self,
        owner_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AllowlistEntry]:
        """Owner-only listing — always served from PG (canonical)."""
        return await self.pg_repo.list_targets(owner_id, limit=limit, offset=offset)

    async def count(self, owner_id: str) -> int:
        """Capacity gauge — served from PG for accuracy."""
        return await self.pg_repo.count_for_owner(owner_id)
