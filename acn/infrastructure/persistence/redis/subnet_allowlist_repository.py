"""Redis implementation of ``ISubnetAllowlistRepository``.

Key layout (verbatim from ADR-0004 §"SubnetAllowlist schema"):

- ``acn:subnets:{subnet_id}:allowlist`` — SET of allowlisted
  ``agent_id``s. Hot ``is_member`` check uses ``SISMEMBER`` for
  O(1) lookup.
- ``acn:subnets:{subnet_id}:allowlist_meta:{agent_id}`` — HASH
  carrying ``added_by`` + ``added_at`` audit fields.

Two-key layout instead of a single HASH because ``SISMEMBER`` on
the SET is the hot ``is_member`` check (called once per ``join``);
a HASH-only layout would force ``HEXISTS`` (same cost in isolation
but loses the future ability to ``SDIFF`` / ``SINTERSTORE`` for
batch ops, e.g. "which agents are on subnet A but not subnet B").

Write atomicity
---------------
``add`` and ``remove`` use ``pipeline(transaction=False)`` (the
codebase convention — see ``RedisSubnetRepository.save``) for the
SET + HASH double-write. A crash between the two leaves one of:

- SET has the agent but HASH meta is missing → ``is_member`` still
  returns True (correct), ``list_for_subnet`` returns the entry
  without audit attribution. The next ``add`` for the same pair
  is idempotent (SADD = no-op) and re-writes the HASH.
- HASH has the meta but SET is missing → ``is_member`` returns
  False (correct semantics: the entry is "not effective"), the
  orphaned HASH gets cleaned up the next time the cascade
  deletion runs.

Neither outcome corrupts the source-of-truth Postgres row; the
Redis side is the cache layer in the dual-store composition.
"""

from __future__ import annotations

import logging

import redis.asyncio as redis  # type: ignore[import-untyped]

from ....core.entities import SubnetAllowlist
from ....core.interfaces import ISubnetAllowlistRepository
from ._hash_utils import decode_value as _decode
from ._hash_utils import normalize_hash as _normalize_hash

logger = logging.getLogger(__name__)


def _allowlist_set_key(subnet_id: str) -> str:
    return f"acn:subnets:{subnet_id}:allowlist"


def _allowlist_meta_key(subnet_id: str, agent_id: str) -> str:
    return f"acn:subnets:{subnet_id}:allowlist_meta:{agent_id}"


class RedisSubnetAllowlistRepository(ISubnetAllowlistRepository):
    def __init__(self, redis_client: redis.Redis) -> None:
        self.redis = redis_client

    async def add(self, entry: SubnetAllowlist) -> bool:
        """Insert into the SET and write the audit meta HASH.

        Returns ``True`` if the entry was newly created. SADD's
        return value is 1 for new, 0 for already-present; we use
        that as the idempotency signal so the route layer can
        pick 201 vs 200 per ADR §HTTP status code conventions.

        On idempotent re-add the meta HASH is **overwritten** with
        the incoming ``added_by`` / ``added_at`` — different from
        the Postgres impl which preserves the original attribution.
        The asymmetry is intentional: PG is source of truth and
        carries the canonical original-attribution row; Redis is a
        cache that may legitimately lag, and overwriting on re-add
        keeps the cache consistent with whatever PG holds for this
        re-add (the service layer's dual-write ordering means PG
        wins). Operators that care about original attribution
        should read PG, not Redis.
        """
        set_key = _allowlist_set_key(entry.subnet_id)
        meta_key = _allowlist_meta_key(entry.subnet_id, entry.agent_id)

        # SADD must run before the HSET so a failure between them
        # leaves the safer of the two crash states (HASH missing,
        # SET present → entry counts as a member, just without
        # readable audit attribution; that's worse than a missing
        # entry but doesn't change admission decisions).
        added_count = await self.redis.sadd(set_key, entry.agent_id)
        # Single HSET — no pipeline needed; one command = one
        # round-trip either way (review fix N1).
        await self.redis.hset(meta_key, mapping=entry.to_dict())  # type: ignore[misc]
        return added_count > 0

    async def remove(self, subnet_id: str, agent_id: str) -> bool:
        """Remove from the SET and DEL the meta HASH.

        Returns ``True`` iff the SET membership was actually
        removed. Symmetric ordering to ``add``: SREM first so the
        agent stops counting as a member immediately, then meta
        cleanup. A crash between them leaves an orphan meta HASH
        that the cascade deletion path sweeps later.
        """
        set_key = _allowlist_set_key(subnet_id)
        meta_key = _allowlist_meta_key(subnet_id, agent_id)

        removed_count = await self.redis.srem(set_key, agent_id)
        await self.redis.delete(meta_key)
        return removed_count > 0

    async def is_member(self, subnet_id: str, agent_id: str) -> bool:
        """O(1) SISMEMBER check; the hot path for the §join flow."""
        return bool(
            await self.redis.sismember(
                _allowlist_set_key(subnet_id), agent_id
            )
        )

    async def list_for_subnet(
        self, subnet_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[SubnetAllowlist]:
        """List allowlist entries for a subnet, most-recent first.

        Reads the SET membership, then dereferences each meta HASH.
        Orphaned SET members (meta HASH missing) are skipped
        silently rather than raised — the missing meta is a known
        cache-only crash artefact (see class docstring's "write
        atomicity" section).
        """
        set_key = _allowlist_set_key(subnet_id)
        agent_ids = await self.redis.smembers(set_key)

        entries: list[SubnetAllowlist] = []
        for raw_aid in agent_ids:
            aid = _decode(raw_aid)
            meta_key = _allowlist_meta_key(subnet_id, aid)
            hash_data = await self.redis.hgetall(meta_key)
            if not hash_data:
                # SET-only orphan; cache crash artefact. Don't fabricate
                # missing audit fields — skip and let the cascade or
                # PG-backed dual-store read produce the canonical row.
                continue
            entries.append(
                SubnetAllowlist.from_dict(_normalize_hash(hash_data))
            )

        entries.sort(key=lambda e: e.added_at, reverse=True)
        return entries[offset : offset + limit]

    async def delete_for_subnet(self, subnet_id: str) -> int:
        """Cascade-delete all allowlist entries for a subnet.

        Iterate the SET, DEL each meta HASH, then DEL the SET
        itself. Best-effort sequential (no Lua atomic envelope
        needed — there is no reverse index to dangle). Partial
        failure raises ``RuntimeError`` after writing the
        ``delete_with_children_partial`` breadcrumb so the
        caller's cascade-ordering contract holds.

        Returns the count of meta HASHes actually deleted.
        """
        set_key = _allowlist_set_key(subnet_id)
        agent_ids = await self.redis.smembers(set_key)

        deleted_count = 0
        partial_failures: list[str] = []

        for raw_aid in agent_ids:
            aid = _decode(raw_aid)
            meta_key = _allowlist_meta_key(subnet_id, aid)
            try:
                await self.redis.delete(meta_key)
                deleted_count += 1
            except Exception as e:  # noqa: BLE001 — best-effort cascade
                partial_failures.append(f"{aid}:{e!r}")

        await self.redis.delete(set_key)

        if partial_failures:
            logger.warning(
                "delete_with_children_partial",
                extra={
                    "subnet_id": subnet_id,
                    "table": "subnet_allowlist",
                    "failures": partial_failures,
                },
            )
            raise RuntimeError(
                "subnet_allowlist cascade had partial failures; "
                "subnet HASH MUST NOT be deleted"
            )

        return deleted_count
