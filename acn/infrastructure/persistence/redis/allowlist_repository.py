"""Redis Implementation of the allowlist cache (Phase 2 PR #2).

Storage layout::

    SET acn:allowlist:{owner_id}    members={target_id, ...}    TTL=30s

Why this is *only* the cache:

* Allowlist membership is access-control source data — durability
  matters; Redis with TTL is not durable enough on its own. The
  Postgres ``agent_allowlist`` table is the canonical source.
* The 30-second TTL is a fail-safe, not a freshness budget. Write
  paths SADD/SREM the cache synchronously so the steady-state cache
  is always tip-of-truth; the TTL only matters when a write skips
  the cache (process crash between PG commit and SADD, network
  hiccup, etc.) — the next ``is_member`` miss triggers a
  read-through (``_read_through_from_pg``) that rebuilds the entire
  SET from PG and re-applies the TTL. After that one miss, the hot
  path is back on cache.
* "Why not 5 min for less PG load?" — allowlist removal is a
  hostile-actor scenario (the owner is **revoking** trust, often
  during an active abuse incident); a stale 5-minute window would
  let the disinvited sender keep posting. 30 seconds is the
  worst-case staleness budget the proposal is willing to accept,
  and it bounds PG fallback load: even a 1k-message/s recipient
  with a totally cold cache only hits PG once per 30s thanks to
  the rebuild.

This module does NOT enforce the dual-write ordering rule (add: PG
first → Redis; remove: Redis first → PG). That ordering is the
service layer's contract and lives in ``AllowlistService`` where
the consequences of partial failure are reasoned about. Repository
methods here are atomic w.r.t. their own layer only.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import redis.asyncio as redis  # type: ignore[import-untyped]

from ....core.interfaces import AllowlistEntry, IAllowlistRepository

logger = logging.getLogger(__name__)


# Worst-case staleness budget after a write skips the cache. See
# module docstring for the rationale; tuned in concert with the
# expected load on the PG fallback path.
DEFAULT_CACHE_TTL_SECONDS = 30

# Permanent sentinel member kept inside every materialised SET.
#
# The motivation: Redis auto-deletes empty SETs the moment their
# last member is SREM'd. That breaks the "EXISTS=1 ⇒ trust the
# cache" probe used by ``is_member`` — without a sentinel an empty
# allowlist would never persist past the rebuild and every check
# would re-fire the PG loader.
#
# We pick a name that no real ``agent_id`` could ever clash with:
# the ``__`` prefix and underscore-only characters are invalid in
# the agent registration validator (agent ids are required to be
# DNS-safe slugs). ``SISMEMBER`` against this value will return 1
# but no real check will ever ask for it; ``SCARD`` is corrected
# by ``count_for_owner`` (subtract 1 when present).
_EMPTY_SENTINEL = "__acn_allowlist_empty_sentinel__"


def _allowlist_key(owner_id: str) -> str:
    return f"acn:allowlist:{owner_id}"


class RedisAllowlistRepository(IAllowlistRepository):
    """Redis SET cache for allowlist membership.

    Only ``add`` / ``remove`` / ``is_member`` are implemented for
    real; the other ``IAllowlistRepository`` methods raise so the
    composing service is forced to call the Postgres repo for
    listing / counting (which is what we want — Redis SET listing
    is not stable and the cache may be partially populated).

    Args:
        redis_client: Redis async client.
        pg_loader: Async callable that returns the full target id
            list for an owner from Postgres. Injected (rather than
            holding a Postgres handle directly) so this layer stays
            thin and testable without spinning up PG.
        cache_ttl_seconds: TTL applied on every cache rebuild. The
            default is the proposal's 30s; tests override to a
            tighter value to verify expiry behaviour.
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        pg_loader: Callable[[str], Awaitable[list[str]]],
        *,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ):
        self.redis = redis_client
        self._pg_loader = pg_loader
        self.cache_ttl_seconds = cache_ttl_seconds

    async def add(
        self,
        owner_id: str,
        target_id: str,
        reason: str | None = None,  # noqa: ARG002 — Redis layer ignores reason
    ) -> bool:
        """Add ``target_id`` to ``owner_id``'s cache SET.

        Reason text is irrelevant for the cache (only PG carries it);
        accepted for interface symmetry. Returns ``True`` if the
        member is new in the SET, ``False`` if it was already
        present (matches PG ON CONFLICT DO NOTHING semantics, so
        the service can use either return value as the canonical
        "newly created?" signal — typically the PG return is used
        and Redis is just kept in sync best-effort).

        We co-add ``_EMPTY_SENTINEL`` so the SET is guaranteed
        materialised even if the recipient has never had a
        rebuild (idempotent: SADD on an already-present sentinel
        is a no-op). This keeps the EXISTS=1 invariant that
        ``is_member`` relies on.

        TTL handling: we extend the TTL on every add so an active
        list doesn't accidentally expire mid-day.
        """
        key = _allowlist_key(owner_id)
        # SADD returns the number of NEW members added. We pull
        # exactly one back from the count of new entries: the
        # sentinel may or may not have been new, but the caller
        # only cares about the target_id.
        before = await self.redis.sismember(key, target_id)
        await self.redis.sadd(key, _EMPTY_SENTINEL, target_id)
        await self.redis.expire(key, self.cache_ttl_seconds)
        return not before

    async def remove(self, owner_id: str, target_id: str) -> bool:
        """Drop ``target_id`` from ``owner_id``'s cache SET.

        We do NOT touch the TTL on remove — the SET continues with
        whatever TTL it had. This matches the "TTL is fail-safe,
        not freshness budget" framing: we only need cache
        invalidation, not eviction.
        """
        removed = await self.redis.srem(_allowlist_key(owner_id), target_id)
        return bool(removed)

    async def is_member(self, owner_id: str, target_id: str) -> bool:
        """Check membership; rebuild from PG on cache miss.

        Three-state probe:

        1. ``EXISTS key`` → 0 → cache MISS (TTL expired or never
           populated). Rebuild from PG, write the full SET back,
           apply the TTL. Then test membership against the rebuilt
           SET.
        2. ``EXISTS key`` → 1 + ``SISMEMBER`` → 1 → cached hit.
        3. ``EXISTS key`` → 1 + ``SISMEMBER`` → 0 → cached miss.
           This means "owner exists in cache, target is NOT in
           list" — return False without touching PG. Critically,
           owners with **empty** allowlists also land in this
           branch: EXISTS=1 (the SET is materialised but empty)
           after the first rebuild. Without this distinction every
           inbound check on an empty-allowlist agent would slam PG.

        Note that the rebuild path uses an injected ``pg_loader``
        rather than a direct repository handle so this layer stays
        Redis-only. The composing ``AllowlistService`` wires the
        loader to ``PostgresAllowlistRepository.list_target_ids``.

        Failure modes:
        - Redis read fails → exception propagates. The service
          catches and applies P0-3 fail-closed policy (divert to
          manifest).
        - PG read fails → exception propagates. Same handling.
        """
        key = _allowlist_key(owner_id)
        exists = await self.redis.exists(key)
        if exists:
            # Cache materialised — trust it (steady state).
            is_set_member = await self.redis.sismember(key, target_id)
            return bool(is_set_member)

        # Cache miss — rebuild from PG and check against fresh data.
        # This is the only path that loads PG on the hot inbound
        # check; with the 30s TTL it caps PG fallback load at
        # ~1 query per 30s per agent in worst case.
        target_ids = await self._pg_loader(owner_id)
        await self._rebuild(owner_id, target_ids)
        return target_id in target_ids

    async def _rebuild(self, owner_id: str, target_ids: list[str]) -> None:
        """Repopulate the cache SET from a freshly-read PG list.

        Uses a pipeline so SADD + EXPIRE are a single round-trip.
        We always include ``_EMPTY_SENTINEL`` in the SADD so the
        SET can never become empty — Redis would auto-delete it
        and the next ``is_member`` call would re-fire the loader.
        See the ``_EMPTY_SENTINEL`` constant docstring for why a
        permanent sentinel is the simpler choice over a "create
        empty SET" dance.

        Order: DEL → SADD(sentinel + members) → EXPIRE. The DEL
        clears any stale members from a previous incarnation
        (there is no "REPLACE SET" primitive).
        """
        key = _allowlist_key(owner_id)
        pipe = self.redis.pipeline()
        pipe.delete(key)
        # Always SADD the sentinel; if there are real members,
        # SADD them in the same call. SADD accepts variadic
        # members, so this is one round-trip regardless of size.
        members_to_add: list[str] = [_EMPTY_SENTINEL, *target_ids]
        pipe.sadd(key, *members_to_add)
        pipe.expire(key, self.cache_ttl_seconds)
        await pipe.execute()

    async def list_targets(
        self,
        owner_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AllowlistEntry]:
        """Not implemented on the cache layer.

        Cache values don't carry ``created_at`` or ``reason``, and
        ``SMEMBERS`` ordering is unstable. Listing must go through
        the Postgres repo. The service layer routes accordingly.
        """
        raise NotImplementedError(
            "RedisAllowlistRepository.list_targets — listings must go "
            "through PostgresAllowlistRepository (cache lacks created_at / reason)"
        )

    async def count_for_owner(self, owner_id: str) -> int:
        """``SCARD`` returns the SET cardinality.

        Only valid when the cache is materialised (EXISTS=1). On
        miss we delegate to the PG loader for an accurate count
        and rebuild as a side-effect — same shape as ``is_member``
        but using length instead of membership.

        We subtract 1 to exclude ``_EMPTY_SENTINEL`` which is
        always present in materialised SETs.
        """
        key = _allowlist_key(owner_id)
        exists = await self.redis.exists(key)
        if exists:
            scard = int(await self.redis.scard(key))
            # ``scard`` includes the permanent sentinel member; subtract
            # to expose the real member count to callers. ``max(0, ...)``
            # is defensive: if a buggy caller has somehow stripped the
            # sentinel, we'd rather report 0 than a negative count.
            return max(0, scard - 1)

        target_ids = await self._pg_loader(owner_id)
        await self._rebuild(owner_id, target_ids)
        return len(target_ids)
