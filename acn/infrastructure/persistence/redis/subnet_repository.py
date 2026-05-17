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

        - ``acn:subnets:by_owner:{owner}`` — owner → subnet_id SET
        - ``acn:subnets:children:{parent_id}`` — parent → child SET
          (ADR-0003; absent when ``parent_subnet_id is None``)
        - ``acn:subnets:by_linked_task:{task_id}`` — task → subnet_id
          SET (ADR-0003; absent when ``linked_task_id is None``)

        On update, the old row is read first so stale entries get
        ``SREM``'d when ``parent_subnet_id`` or ``linked_task_id``
        changes. ``parent_subnet_id`` is immutable per ADR-0003 §5
        but ``linked_task_id`` flips to ``None`` when Phase 2's
        ``promote_to_persistent`` lands — defensive incremental
        update keeps the index honest either way.
        """
        subnet_key = f"acn:subnets:info:{subnet.subnet_id}"

        # Read existing row (if any) so we know which stale index
        # entries to evict when nesting fields change.
        old_subnet = await self.find_by_id(subnet.subnet_id)

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
        if subnet_dict.get("parent_subnet_id") is None:
            subnet_dict["parent_subnet_id"] = ""
        if subnet_dict.get("linked_task_id") is None:
            subnet_dict["linked_task_id"] = ""
        # redis-py refuses raw bool values for HSET — round-trip via the
        # "True"/"False" string form that `_dict_to_subnet` already parses
        # (see `is_private` parser).
        subnet_dict["is_private"] = str(bool(subnet_dict.get("is_private", False)))

        await self.redis.hset(subnet_key, mapping=subnet_dict)  # type: ignore[arg-type]

        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.sadd(f"acn:subnets:by_owner:{subnet.owner}", subnet.subnet_id)
            # Maintain children index: SREM old → SADD new when changed.
            old_parent = old_subnet.parent_subnet_id if old_subnet else None
            new_parent = subnet.parent_subnet_id
            if old_parent and old_parent != new_parent:
                pipe.srem(
                    f"acn:subnets:children:{old_parent}", subnet.subnet_id
                )
            if new_parent:
                pipe.sadd(
                    f"acn:subnets:children:{new_parent}", subnet.subnet_id
                )
            # Maintain by_linked_task index identically.
            old_task = old_subnet.linked_task_id if old_subnet else None
            new_task = subnet.linked_task_id
            if old_task and old_task != new_task:
                pipe.srem(
                    f"acn:subnets:by_linked_task:{old_task}", subnet.subnet_id
                )
            if new_task:
                pipe.sadd(
                    f"acn:subnets:by_linked_task:{new_task}", subnet.subnet_id
                )
            await pipe.execute()

    async def find_by_id(self, subnet_id: str) -> Subnet | None:
        """Find subnet by ID"""
        subnet_key = f"acn:subnets:info:{subnet_id}"
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
        for subnet_id in subnet_ids:
            subnet = await self.find_by_id(subnet_id)
            if subnet:
                subnets.append(subnet)
        return subnets

    async def find_public_subnets(self) -> list[Subnet]:
        """Find all public subnets"""
        all_subnets = await self.find_all()
        return [s for s in all_subnets if s.is_public()]

    async def delete(self, subnet_id: str) -> bool:
        """Delete a subnet and all its secondary index entries.

        Reads the subnet first to know which index sets to clean up
        (owner, parent, linked-task). All deletions are then batched
        in a single pipeline.
        """
        subnet = await self.find_by_id(subnet_id)
        if not subnet:
            return False

        subnet_key = f"acn:subnets:info:{subnet_id}"
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.delete(subnet_key)
            pipe.srem(f"acn:subnets:by_owner:{subnet.owner}", subnet_id)
            if subnet.parent_subnet_id:
                pipe.srem(
                    f"acn:subnets:children:{subnet.parent_subnet_id}", subnet_id
                )
            if subnet.linked_task_id:
                pipe.srem(
                    f"acn:subnets:by_linked_task:{subnet.linked_task_id}", subnet_id
                )
            await pipe.execute()

        return True

    async def delete_with_children(
        self, parent_id: str, child_ids: list[str]
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
        """
        for child_id in child_ids:
            deleted = await self.delete(child_id)
            if not deleted:
                logger.warning(
                    "delete_with_children_partial",
                    extra={
                        "parent_subnet_id": parent_id,
                        "child_subnet_id": child_id,
                        "reason": "child_delete_returned_false",
                    },
                )
                raise RuntimeError(
                    f"Cascade delete failed for child {child_id}; "
                    f"refusing to delete parent {parent_id}"
                )
        return await self.delete(parent_id)

    async def exists(self, subnet_id: str) -> bool:
        """Check if subnet exists"""
        return await self.redis.exists(f"acn:subnets:info:{subnet_id}") > 0

    async def find_by_parent(self, parent_subnet_id: str) -> list[Subnet]:
        """Return all child subnets of a given parent.

        Reads the ``acn:subnets:children:{parent_id}`` index, then
        ``find_by_id`` each member. Empty list when no children exist
        or the parent itself is unknown.
        """
        subnet_ids = await self.redis.smembers(
            f"acn:subnets:children:{parent_subnet_id}"
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

    def _dict_to_subnet(self, subnet_dict: dict) -> Subnet:
        """Convert Redis dict to Subnet entity.

        Empty strings stored in Redis mean NULL — translate back to
        ``None`` for the relevant fields (description, harness_url /
        secret, parent_subnet_id, linked_task_id). Legacy rows that
        predate ADR-0003 don't carry the nesting keys at all; the
        ``.get("parent_subnet_id") or None`` pattern handles both
        "missing key" and "empty string" identically.
        """
        description = subnet_dict.get("description") or None
        harness_url = subnet_dict.get("harness_url") or None
        harness_secret = subnet_dict.get("harness_secret") or None
        parent_subnet_id = subnet_dict.get("parent_subnet_id") or None
        linked_task_id = subnet_dict.get("linked_task_id") or None
        # ``lifecycle`` defaults to "persistent" both when key absent
        # (legacy row) and when stored value is empty/falsy.
        lifecycle = subnet_dict.get("lifecycle") or "persistent"

        data = {
            "subnet_id": subnet_dict["subnet_id"],
            "name": subnet_dict["name"],
            "owner": subnet_dict["owner"],
            "description": description,
            "is_private": subnet_dict.get("is_private") == "True",
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
            "parent_subnet_id": parent_subnet_id,
            "lifecycle": lifecycle,
            "linked_task_id": linked_task_id,
        }

        return Subnet(**data)
