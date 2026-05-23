"""Redis Implementation of Subnet Repository

Concrete implementation using Redis for subnet persistence.
"""

import json
import logging
from datetime import UTC, datetime

import redis.asyncio as redis  # type: ignore[import-untyped]

from ....core.entities import Subnet
from ....core.interfaces import ISubnetRepository

logger = logging.getLogger(__name__)


class RedisSubnetRepository(ISubnetRepository):
    """
    Redis-based Subnet Repository

    Implements ISubnetRepository using Redis as storage backend.
    """

    def __init__(self, redis_client: redis.Redis):
        """
        Initialize Redis Subnet Repository

        Args:
            redis_client: Redis async client instance
        """
        self.redis = redis_client

    async def save(self, subnet: Subnet) -> None:
        """Save or update a subnet in Redis.

        Writes the main HASH first with a direct ``HSET`` (preserves
        the legacy code path that existing tests pin), then batches
        all secondary index mutations into a single
        ``pipeline(transaction=False)``:

        - ``acn:subnets:by_owner:{owner}`` — owner → slug SET
        - ``acn:subnets:children:{parent_id}`` — parent → child SET
          (ADR-0003; absent when ``parent_slug is None``)
        - ``acn:subnets:by_linked_task:{task_id}`` — task → slug
          SET (ADR-0003; absent when ``linked_task_id is None``)

        On update, the old row is read first so stale entries get
        ``SREM``'d when ``parent_slug`` or ``linked_task_id``
        changes. ``parent_slug`` is immutable per ADR-0003 §5
        but ``linked_task_id`` flips to ``None`` when Phase 2's
        ``promote_to_persistent`` lands — defensive incremental
        update keeps the index honest either way.
        """
        subnet_key = f"acn:subnets:info:{subnet.slug}"

        # Read existing row (if any) so we know which stale index
        # entries to evict when nesting fields change.
        old_subnet = await self.find_by_id(subnet.slug)

        subnet_dict = subnet.to_dict(include_secret=True)
        subnet_dict["security_config"] = json.dumps(subnet_dict["security_config"])
        subnet_dict["metadata"] = json.dumps(subnet_dict["metadata"])
        subnet_dict["member_agent_ids"] = json.dumps(subnet_dict["member_agent_ids"])
        # Redis HSET cannot store None values
        if subnet_dict.get("harness_url") is None:
            subnet_dict["harness_url"] = ""
        if subnet_dict.get("harness_secret") is None:
            subnet_dict["harness_secret"] = ""
        if subnet_dict.get("description") is None:
            subnet_dict["description"] = ""
        # Nesting fields: empty string ≡ NULL, same convention as
        # description / harness_url. Parsed back to ``None`` in
        # ``_dict_to_subnet``.
        if subnet_dict.get("parent_slug") is None:
            subnet_dict["parent_slug"] = ""
        if subnet_dict.get("linked_task_id") is None:
            subnet_dict["linked_task_id"] = ""
        # ADR-0004: ``join_policy`` is always populated on the entity
        # (defaults to ``"open"``), so no None-to-empty translation is
        # needed here — but we coerce ``str(...)`` defensively in case a
        # caller smuggles a non-string in via ``to_dict`` overrides.
        subnet_dict["join_policy"] = str(
            subnet_dict.get("join_policy") or "open"
        )
        # redis-py refuses raw bool values for HSET — round-trip via the
        # "True"/"False" string form that `_dict_to_subnet` already parses
        # (see `is_private` parser).
        subnet_dict["is_private"] = str(bool(subnet_dict.get("is_private", False)))

        await self.redis.hset(subnet_key, mapping=subnet_dict)  # type: ignore[arg-type]

        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.sadd(f"acn:subnets:by_owner:{subnet.owner}", subnet.slug)
            # Maintain children index: SREM old → SADD new when changed.
            old_parent = old_subnet.parent_slug if old_subnet else None
            new_parent = subnet.parent_slug
            if old_parent and old_parent != new_parent:
                pipe.srem(
                    f"acn:subnets:children:{old_parent}", subnet.slug
                )
            if new_parent:
                pipe.sadd(
                    f"acn:subnets:children:{new_parent}", subnet.slug
                )
            # Maintain by_linked_task index identically.
            old_task = old_subnet.linked_task_id if old_subnet else None
            new_task = subnet.linked_task_id
            if old_task and old_task != new_task:
                pipe.srem(
                    f"acn:subnets:by_linked_task:{old_task}", subnet.slug
                )
            if new_task:
                pipe.sadd(
                    f"acn:subnets:by_linked_task:{new_task}", subnet.slug
                )
            await pipe.execute()

    async def find_by_id(self, slug: str) -> Subnet | None:
        """Find subnet by ID"""
        subnet_key = f"acn:subnets:info:{slug}"
        subnet_dict = await self.redis.hgetall(subnet_key)

        if not subnet_dict:
            return None

        return self._dict_to_subnet(subnet_dict)

    async def find_all(self) -> list[Subnet]:
        """Find all subnets"""
        subnets = []
        async for key in self.redis.scan_iter("acn:subnets:info:*"):
            subnet_dict = await self.redis.hgetall(key)
            if subnet_dict:
                subnets.append(self._dict_to_subnet(subnet_dict))
        return subnets

    async def find_by_owner(self, owner: str) -> list[Subnet]:
        """Find all subnets owned by a user"""
        subnet_ids = await self.redis.smembers(f"acn:subnets:by_owner:{owner}")
        subnets = []
        for slug in subnet_ids:
            subnet = await self.find_by_id(slug)
            if subnet:
                subnets.append(subnet)
        return subnets

    async def find_by_owners(self, owners: set[str]) -> list[Subnet]:
        """Find all subnets whose owner is in *owners* (union of per-owner sets)."""
        if not owners:
            return []
        seen: set[str] = set()
        subnets: list[Subnet] = []
        for owner in owners:
            subnet_ids = await self.redis.smembers(f"acn:subnets:by_owner:{owner}")
            for slug in subnet_ids:
                if slug in seen:
                    continue
                seen.add(slug)
                subnet = await self.find_by_id(slug)
                if subnet:
                    subnets.append(subnet)
        return subnets

    async def find_public_subnets(self) -> list[Subnet]:
        """Find all public subnets"""
        all_subnets = await self.find_all()
        return [s for s in all_subnets if s.is_public()]

    async def delete(
        self, slug: str, *, session: object | None = None
    ) -> bool:
        """Delete a subnet and all its secondary index entries.

        Reads the subnet first to know which index sets to clean up
        (owner, parent, linked-task). All deletions are then batched
        in a single pipeline.

        The ``session`` kwarg is part of the :class:`ISubnetRepository`
        contract (an :class:`IUnitOfWork` token used by the Postgres
        impl to participate in the outer cascade transaction). Redis
        ignores it — the per-call pipeline is the strongest atomicity
        primitive available here and it doesn't compose across method
        calls. See ADR-0004 §"Cascade deletion: Redis" for the
        asymmetric ordering contract the service layer relies on
        instead.
        """
        del session  # explicit ignore — see docstring
        subnet = await self.find_by_id(slug)
        if not subnet:
            return False

        subnet_key = f"acn:subnets:info:{slug}"
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.delete(subnet_key)
            pipe.srem(f"acn:subnets:by_owner:{subnet.owner}", slug)
            if subnet.parent_slug:
                pipe.srem(
                    f"acn:subnets:children:{subnet.parent_slug}", slug
                )
            if subnet.linked_task_id:
                pipe.srem(
                    f"acn:subnets:by_linked_task:{subnet.linked_task_id}", slug
                )
            await pipe.execute()

        return True

    async def delete_with_children(
        self,
        parent_id: str,
        child_ids: list[str],
        *,
        session: object | None = None,
    ) -> bool:
        """Sequential cascade delete with audit-log breadcrumb on
        partial failure (ADR-0003 §A.4 Redis branch).

        Redis has no cross-method MULTI/EXEC primitive that spans
        multiple ``delete()`` calls (each call already runs its own
        small pipeline for secondary-index cleanup). So the contract
        here is best-effort with a clear failure signal:

        - Each child is deleted in order. If any child delete returns
          ``False`` (e.g. concurrent dissolution by another caller),
          we log ``delete_with_children_partial`` and raise
          ``RuntimeError`` BEFORE touching the parent — leaving the
          parent in place gives ops a recoverable state (re-run the
          cascade once the offending child is reconciled).
        - On a fully successful child sweep, the parent delete is
          attempted last. Its return value propagates (False = parent
          already gone, but children were still cleaned).

        The ``session`` kwarg is part of the :class:`ISubnetRepository`
        contract — Redis ignores it for the same reason
        :meth:`delete` does (no transactional composition primitive).
        """
        del session  # explicit ignore — see docstring
        for child_id in child_ids:
            deleted = await self.delete(child_id)
            if not deleted:
                logger.warning(
                    "delete_with_children_partial",
                    extra={
                        "parent_slug": parent_id,
                        "child_subnet_id": child_id,
                        "reason": "child_delete_returned_false",
                    },
                )
                raise RuntimeError(
                    f"Cascade delete failed for child {child_id}; "
                    f"refusing to delete parent {parent_id}"
                )
        return await self.delete(parent_id)

    async def exists(self, slug: str) -> bool:
        """Check if subnet exists"""
        return await self.redis.exists(f"acn:subnets:info:{slug}") > 0

    async def find_by_parent(self, parent_slug: str) -> list[Subnet]:
        """Return all child subnets of a given parent.

        Reads the ``acn:subnets:children:{parent_id}`` index, then
        ``find_by_id`` each member. Empty list when no children exist
        or the parent itself is unknown.
        """
        subnet_ids = await self.redis.smembers(
            f"acn:subnets:children:{parent_slug}"
        )
        subnets: list[Subnet] = []
        for sid in subnet_ids:
            if isinstance(sid, bytes):
                sid = sid.decode()
            subnet = await self.find_by_id(sid)
            if subnet:
                subnets.append(subnet)
        return subnets

    async def find_by_linked_task(self, task_id: str) -> list[Subnet]:
        """Return all subnets bound to a given task via ``linked_task_id``.

        Reads the ``acn:subnets:by_linked_task:{task_id}`` index, then
        ``find_by_id`` each member. Consumers (Phase 3 cascade hook)
        filter by ``lifecycle`` themselves if they only want
        ``task_scoped`` rows.
        """
        subnet_ids = await self.redis.smembers(
            f"acn:subnets:by_linked_task:{task_id}"
        )
        subnets: list[Subnet] = []
        for sid in subnet_ids:
            if isinstance(sid, bytes):
                sid = sid.decode()
            subnet = await self.find_by_id(sid)
            if subnet:
                subnets.append(subnet)
        return subnets

    @staticmethod
    def _safe_loads(raw: str | None, default):
        """Safely parse a JSON string; return default on any error."""
        try:
            return json.loads(raw) if raw else default
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning(
                "subnet_repository: corrupted JSON field, using default",
                extra={"raw": str(raw)[:200]},
            )
            return default

    @staticmethod
    def _safe_fromisoformat(raw: str | None, default):
        """Safely parse an ISO datetime string; return default on any error."""
        if not raw:
            return default
        try:
            return datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            logger.warning(
                "subnet_repository: invalid datetime field, using default",
                extra={"raw": str(raw)[:50]},
            )
            return default

    @staticmethod
    def _normalize_redis_dict(raw: dict) -> dict[str, str]:
        """Coerce a Redis HASH dict to ``dict[str, str]`` regardless of
        the client's ``decode_responses`` setting.

        Production composition (``registry.py``) sets
        ``decode_responses=True`` so ``hgetall`` already returns
        ``dict[str, str]``; this normalisation is then a no-op. But
        external callers (the backfill script, ad-hoc scripts, future
        repo-level reuse) sometimes construct a client without that
        flag — ``hgetall`` then returns ``dict[bytes, bytes]`` and
        every ``subnet_dict.get("is_private")`` silently misses,
        defaulting ``is_private`` to ``False`` and corrupting the
        ``join_policy`` legacy auto-upgrade rule below.

        Normalising here is defence in depth: the repo's parsing
        logic stays self-contained instead of depending on the
        client's configuration being pinned correctly forever.
        """
        if not raw:
            return {}
        out: dict[str, str] = {}
        for k, v in raw.items():
            key = k.decode() if isinstance(k, bytes) else str(k)
            val = v.decode() if isinstance(v, bytes) else v
            out[key] = val
        return out

    def _dict_to_subnet(self, subnet_dict: dict) -> Subnet:
        """Convert Redis dict to Subnet entity.

        Empty strings stored in Redis mean NULL — translate back to
        ``None`` for the relevant fields (description, harness_url /
        secret, parent_slug, linked_task_id). Legacy rows that
        predate ADR-0003 don't carry the nesting keys at all; the
        ``.get("parent_slug") or None`` pattern handles both
        "missing key" and "empty string" identically.

        Input is normalised through :meth:`_normalize_redis_dict`
        first so byte-keyed dicts (``decode_responses=False`` client)
        and string-keyed dicts behave identically — protects every
        ``subnet_dict.get("...")`` call below from silently missing.
        """
        subnet_dict = self._normalize_redis_dict(subnet_dict)
        description = subnet_dict.get("description") or None
        harness_url = subnet_dict.get("harness_url") or None
        harness_secret = subnet_dict.get("harness_secret") or None
        parent_slug = subnet_dict.get("parent_slug") or None
        linked_task_id = subnet_dict.get("linked_task_id") or None
        # ``lifecycle`` defaults to "persistent" both when key absent
        # (legacy row) and when stored value is empty/falsy.
        lifecycle = subnet_dict.get("lifecycle") or "persistent"

        # ADR-0004: legacy rows predate the ``join_policy`` field. If
        # the key is missing and ``is_private`` is true, auto-upgrade to
        # ``"approval"`` so the entity invariant accepts the row even
        # before the Redis backfill script runs. Otherwise default to
        # ``"open"``. This mirrors ``Subnet.from_dict``'s auto-upgrade
        # rule (kept in sync with the entity for defence in depth — the
        # entity is the single source of truth, the repo's logic just
        # prevents the entity from having to special-case "missing key"
        # vs "empty string" twice).
        is_private = subnet_dict.get("is_private") == "True"
        join_policy_raw = subnet_dict.get("join_policy") or ""
        if join_policy_raw:
            join_policy = join_policy_raw
        elif is_private:
            join_policy = "approval"
        else:
            join_policy = "open"

        data = {
            "slug": subnet_dict["slug"],
            "name": subnet_dict["name"],
            "owner": subnet_dict["owner"],
            "description": description,
            "is_private": is_private,
            "security_config": self._safe_loads(subnet_dict.get("security_config", "{}"), {}),
            "member_agent_ids": set(
                self._safe_loads(subnet_dict.get("member_agent_ids", "[]"), [])
            ),
            "created_at": self._safe_fromisoformat(
                subnet_dict.get("created_at"), datetime.now(UTC)
            ),
            "metadata": self._safe_loads(subnet_dict.get("metadata", "{}"), {}),
            "harness_url": harness_url,
            "harness_secret": harness_secret,
            "parent_slug": parent_slug,
            "lifecycle": lifecycle,
            "linked_task_id": linked_task_id,
            "join_policy": join_policy,
        }
        # Forward the opaque UUID when present. Legacy rows that predate
        # the privacy column don't carry it; the entity's
        # ``id: str = field(default_factory=lambda: str(uuid4()))`` then
        # generates a fresh one on entity construction. The next save()
        # persists it back to Redis, so the ID stabilises after the first
        # read-then-write cycle. (For Postgres the ``server_default``
        # backfills on migration; this only matters for Redis-only
        # deployments.)
        opaque_id = subnet_dict.get("id")
        if opaque_id:
            data["id"] = opaque_id

        return Subnet(**data)
