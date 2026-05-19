"""Redis implementation of ``ISubnetJoinRequestRepository``.

Key layout (verbatim from ADR-0004 §"Redis layout and atomicity"):

- ``acn:subnets:{subnet_id}:requests:{request_id}`` — HASH carrying
  the serialised ``SubnetJoinRequest`` fields.
- ``acn:subnets:{subnet_id}:pending_by_agent:{agent_id}`` — STRING
  whose value is the current pending ``request_id`` for that
  ``(subnet, agent)`` pair. The reverse pending index that
  enforces the "at most one pending per (subnet, agent) across
  all kinds" invariant Redis-side, equivalent to Postgres's
  ``UNIQUE … WHERE status='pending'`` partial index.
- ``acn:subnets:{subnet_id}:requests`` — SET of all
  ``request_id``s for that subnet, used by ``GET /subnets/{s}/
  join-requests`` and ``/invitations``.
- ``acn:agents:{agent_id}:subnet_invitations`` — SET of
  ``request_id``s with ``kind='invitation' AND status='pending'``,
  used by the invitee-facing
  ``GET /agents/{a}/subnet-invitations``.

The Lua atomic envelope
-----------------------
``create-pending`` MUST be atomic across the reverse-index SET and
the HASH (ADR §"Redis layout and atomicity"): a naive
``SETNX`` + ``HSET`` two-step leaks a deadlock if the HSET fails
— the reverse index points at a non-existent request and
subsequent ``SETNX`` attempts for the same ``(subnet, agent)``
fail forever. The ``CREATE_PENDING_LUA`` script below runs the
SETNX, the HSET, the listing-SET SADD, and (for invitations) the
per-agent SADD as one atomic unit.

The transition path (status → approved/rejected/withdrawn) also
runs through a Lua script (``DECIDE_LUA``) so the reverse-index
DEL and the HASH update commit as one unit; without that, a
crash between the two could leave a terminal-status row with a
dangling reverse-index pointer, blocking the agent's next legal
re-request forever.

Idempotency model
-----------------
- ``CREATE_PENDING_LUA`` returns the existing ``request_id`` on
  reverse-index collision; the caller compares it against the
  one it tried to write. Equal → idempotent re-save (no-op).
  Different → race lost; raise
  ``SubnetJoinRequestPendingError`` so the route layer surfaces
  409 with the stable reason token.
- ``DECIDE_LUA`` is unconditionally safe to replay; second call
  is a no-op DEL plus an HSET of the same fields.
"""

from __future__ import annotations

import logging
from typing import Any

import redis.asyncio as redis  # type: ignore[import-untyped]

from ....core.entities import SubnetJoinRequest
from ....core.interfaces import ISubnetJoinRequestRepository
from ..postgres.subnet_join_request_repository import (
    SubnetJoinRequestPendingError,
)

logger = logging.getLogger(__name__)

# Atomic "create pending request" envelope.
#
# KEYS[1] = pending_by_agent reverse index
# KEYS[2] = request HASH
# KEYS[3] = subnet listing SET
# KEYS[4] = agent invitations SET (only SADD'd if ARGV[2]='invitation')
#
# ARGV[1] = request_id (the value to SET into KEYS[1])
# ARGV[2] = kind discriminator ('invitation' or other)
# ARGV[3..N] = HSET field/value pairs (even count)
#
# Returns:
#   {'exists', <existing_request_id>}   on reverse-index collision
#   {'created', <request_id>}           on successful new creation
CREATE_PENDING_LUA = """
local existing = redis.call('GET', KEYS[1])
if existing then
    return {'exists', existing}
end
redis.call('SET', KEYS[1], ARGV[1])
for i = 3, #ARGV, 2 do
    redis.call('HSET', KEYS[2], ARGV[i], ARGV[i + 1])
end
redis.call('SADD', KEYS[3], ARGV[1])
if ARGV[2] == 'invitation' then
    redis.call('SADD', KEYS[4], ARGV[1])
end
return {'created', ARGV[1]}
"""

# Atomic "transition out of pending" envelope.
#
# KEYS[1] = pending_by_agent reverse index (DEL'd)
# KEYS[2] = request HASH (HSET updated)
# KEYS[3] = agent invitations SET (SREM'd if kind='invitation')
#
# ARGV[1] = request_id (to SREM from KEYS[3] if invitation)
# ARGV[2] = kind discriminator ('invitation' or other)
# ARGV[3..N] = HSET field/value pairs (even count)
#
# Returns 1 always (no failure mode worth distinguishing — replay is safe).
DECIDE_LUA = """
redis.call('DEL', KEYS[1])
for i = 3, #ARGV, 2 do
    redis.call('HSET', KEYS[2], ARGV[i], ARGV[i + 1])
end
if ARGV[2] == 'invitation' then
    redis.call('SREM', KEYS[3], ARGV[1])
end
return 1
"""


def _request_hash_key(subnet_id: str, request_id: str) -> str:
    return f"acn:subnets:{subnet_id}:requests:{request_id}"


def _pending_by_agent_key(subnet_id: str, agent_id: str) -> str:
    return f"acn:subnets:{subnet_id}:pending_by_agent:{agent_id}"


def _subnet_listing_key(subnet_id: str) -> str:
    return f"acn:subnets:{subnet_id}:requests"


def _agent_invitations_key(agent_id: str) -> str:
    return f"acn:agents:{agent_id}:subnet_invitations"


def _decode(value) -> str:
    """Coerce bytes/str/None from redis-py to ``str`` ('' for None).

    Mirrors the same ``decode_responses``-tolerant pattern
    ``RedisSubnetRepository._normalize_redis_dict`` uses — the repo
    layer shouldn't depend on the client's configuration flag being
    pinned correctly forever."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


class RedisSubnetJoinRequestRepository(ISubnetJoinRequestRepository):
    def __init__(self, redis_client: redis.Redis) -> None:
        self.redis = redis_client
        # Lazy-registered Lua scripts. The redis-py ``Script`` object
        # uses SCRIPT LOAD + EVALSHA on first use (falling back to
        # EVAL on NOSCRIPT), which is the same lifecycle
        # ``RedisTaskRepository`` uses for ``LUA_JOIN_TASK`` etc.
        # Lazy init keeps ``__init__`` cheap and avoids a SCRIPT LOAD
        # round-trip on repos that only ever call read paths.
        self._create_pending_script: Any | None = None
        self._decide_script: Any | None = None

    def _get_create_pending_script(self) -> Any:
        if self._create_pending_script is None:
            self._create_pending_script = self.redis.register_script(
                CREATE_PENDING_LUA
            )
        return self._create_pending_script

    def _get_decide_script(self) -> Any:
        if self._decide_script is None:
            self._decide_script = self.redis.register_script(DECIDE_LUA)
        return self._decide_script

    async def save(self, request: SubnetJoinRequest) -> None:
        """Dispatch on ``is_pending`` to the appropriate Lua envelope.

        - Pending row: ``CREATE_PENDING_LUA`` runs SETNX semantics
          on the reverse index; on collision raises
          ``SubnetJoinRequestPendingError`` so the route layer
          mirrors the Postgres 409 surface.
        - Terminal row (approved / rejected / withdrawn):
          ``DECIDE_LUA`` runs DEL + HSET atomically so a crash
          between the two can't leave a stale reverse-index
          pointer.
        """
        data = request.to_dict()
        hash_pairs: list[str] = []
        for k, v in data.items():
            hash_pairs.append(k)
            hash_pairs.append(v)

        if request.is_pending:
            await self._create_pending(request, hash_pairs)
        else:
            await self._decide(request, hash_pairs)

    async def _create_pending(
        self, request: SubnetJoinRequest, hash_pairs: list[str]
    ) -> None:
        keys = [
            _pending_by_agent_key(request.subnet_id, request.agent_id),
            _request_hash_key(request.subnet_id, request.request_id),
            _subnet_listing_key(request.subnet_id),
            _agent_invitations_key(request.agent_id),
        ]
        args = [request.request_id, request.kind, *hash_pairs]
        script = self._get_create_pending_script()
        result = await script(keys=keys, args=args, client=self.redis)
        # Lua returns a 2-element table; redis-py renders it as a list
        # of bytes/str depending on decode_responses.
        outcome = _decode(result[0])
        existing_id = _decode(result[1])
        if outcome == "exists" and existing_id != request.request_id:
            raise SubnetJoinRequestPendingError(
                request.subnet_id, request.agent_id
            )
        # 'exists' + same id is idempotent re-save (no-op for the
        # service layer — it's safe to retry create on transient
        # network failures); 'created' is the happy path.

    async def _decide(
        self, request: SubnetJoinRequest, hash_pairs: list[str]
    ) -> None:
        keys = [
            _pending_by_agent_key(request.subnet_id, request.agent_id),
            _request_hash_key(request.subnet_id, request.request_id),
            _agent_invitations_key(request.agent_id),
        ]
        args = [request.request_id, request.kind, *hash_pairs]
        script = self._get_decide_script()
        await script(keys=keys, args=args, client=self.redis)

    async def find_by_id(self, request_id: str) -> SubnetJoinRequest | None:
        """Look up by request_id.

        Redis layout indexes by ``(subnet_id, request_id)``; a
        request_id-only lookup requires scanning the listing SETs.
        Heavy operation — only used by admin / debugging paths;
        the route layer prefers ``find_pending_for(subnet, agent)``
        for the hot collision-check path.
        """
        async for key in self.redis.scan_iter(
            "acn:subnets:*:requests:*"
        ):
            key_str = _decode(key)
            if key_str.endswith(f":{request_id}"):
                hash_data = await self.redis.hgetall(key_str)
                if hash_data:
                    return SubnetJoinRequest.from_dict(
                        _normalize_hash(hash_data)
                    )
        return None

    async def find_pending_for(
        self, subnet_id: str, agent_id: str
    ) -> SubnetJoinRequest | None:
        pending_key = _pending_by_agent_key(subnet_id, agent_id)
        request_id = _decode(await self.redis.get(pending_key))
        if not request_id:
            return None
        hash_data = await self.redis.hgetall(
            _request_hash_key(subnet_id, request_id)
        )
        if not hash_data:
            # Reverse index points at a non-existent HASH — the very
            # deadlock state the Lua envelope is supposed to prevent.
            # Surface as a warning (this should never happen on
            # healthy deployments) but return None so callers see
            # "no pending row" rather than a 500.
            logger.warning(
                "subnet_join_request: dangling reverse index "
                "(reverse points at missing HASH)",
                extra={
                    "subnet_id": subnet_id,
                    "agent_id": agent_id,
                    "request_id": request_id,
                },
            )
            return None
        return SubnetJoinRequest.from_dict(_normalize_hash(hash_data))

    async def list_by_subnet(
        self,
        subnet_id: str,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SubnetJoinRequest]:
        listing_key = _subnet_listing_key(subnet_id)
        request_ids = await self.redis.smembers(listing_key)
        rows: list[SubnetJoinRequest] = []
        for raw_rid in request_ids:
            rid = _decode(raw_rid)
            hash_data = await self.redis.hgetall(
                _request_hash_key(subnet_id, rid)
            )
            if not hash_data:
                continue
            req = SubnetJoinRequest.from_dict(_normalize_hash(hash_data))
            if kind is not None and req.kind != kind:
                continue
            if status is not None and req.status != status:
                continue
            rows.append(req)
        # Sort then paginate — matches the contract pinned in
        # ``ISubnetJoinRequestRepository.list_by_subnet``: most-recent
        # first. The slice is O(n log n) on the per-subnet listing,
        # acceptable because the listing path is cold (operator
        # dashboard, not hot inbound).
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[offset : offset + limit]

    async def list_pending_invitations_for_agent(
        self, agent_id: str
    ) -> list[SubnetJoinRequest]:
        """List pending invitations across all subnets.

        Reads the per-agent SET and dereferences each request_id
        against its subnet HASH. The SET membership is maintained
        by ``CREATE_PENDING_LUA`` (SADD on kind='invitation') and
        ``DECIDE_LUA`` (SREM on transition out), so it always
        reflects the actual pending invitation set without a
        separate filter pass.

        Quadratic-ish in the SET size — but the per-agent invitation
        backlog is bounded by sensible UX (humans don't invite the
        same agent into hundreds of subnets simultaneously); no
        secondary pagination needed at Slice 2.1.
        """
        invitations_key = _agent_invitations_key(agent_id)
        request_ids = await self.redis.smembers(invitations_key)
        rows: list[SubnetJoinRequest] = []
        for raw_rid in request_ids:
            rid = _decode(raw_rid)
            # Have to find the subnet — the SET stores only request_ids.
            req = await self.find_by_id(rid)
            if req is not None and req.kind == "invitation" and req.is_pending:
                rows.append(req)
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows

    async def delete_for_subnet(self, subnet_id: str) -> int:
        """Cascade-delete all rows for a subnet.

        Best-effort sequential per ADR §"Cascade deletion: Redis":
        iterate the listing SET, delete each request HASH + reverse
        index + per-agent invitation SET membership, then finally
        DEL the listing SET. Any partial failure raises
        ``RuntimeError`` after writing the
        ``delete_with_children_partial`` breadcrumb — caller
        (``SubnetService.delete_subnet``) MUST surface the error
        BEFORE touching the subnet HASH so a half-cascade isn't
        treated as success.

        Returns the count of request HASHes actually deleted (for
        audit log; not gated on by the cascade control flow).
        """
        listing_key = _subnet_listing_key(subnet_id)
        request_ids = await self.redis.smembers(listing_key)

        deleted_count = 0
        partial_failures: list[str] = []

        for raw_rid in request_ids:
            rid = _decode(raw_rid)
            hash_key = _request_hash_key(subnet_id, rid)
            try:
                # Need to read kind + agent_id before deleting the HASH
                # so we know which secondary indexes to clean up.
                hash_data = await self.redis.hgetall(hash_key)
                if hash_data:
                    norm = _normalize_hash(hash_data)
                    agent_id = norm.get("agent_id", "")
                    kind = norm.get("kind", "")
                    async with self.redis.pipeline(transaction=False) as pipe:
                        pipe.delete(hash_key)
                        if agent_id:
                            pipe.delete(
                                _pending_by_agent_key(subnet_id, agent_id)
                            )
                            if kind == "invitation":
                                pipe.srem(
                                    _agent_invitations_key(agent_id), rid
                                )
                        await pipe.execute()
                    deleted_count += 1
            except Exception as e:  # noqa: BLE001 — best-effort cascade
                partial_failures.append(f"{rid}:{e!r}")

        # DEL the listing SET last (membership might still be needed by
        # observers between the per-row deletes and now; in practice
        # ``delete_for_subnet`` is the cascade's only caller and runs
        # under the service's RuntimeError-guarded contract).
        await self.redis.delete(listing_key)

        if partial_failures:
            logger.warning(
                "delete_with_children_partial",
                extra={
                    "subnet_id": subnet_id,
                    "table": "subnet_join_requests",
                    "failures": partial_failures,
                },
            )
            raise RuntimeError(
                "subnet_join_request cascade had partial failures; "
                "subnet HASH MUST NOT be deleted"
            )

        return deleted_count


def _normalize_hash(raw: dict) -> dict[str, str]:
    """Coerce a Redis HASH dict to ``dict[str, str]``.

    Mirrors ``RedisSubnetRepository._normalize_redis_dict`` — guards
    against the ``decode_responses=False`` client configuration that
    backfill scripts and ad-hoc tooling sometimes use."""
    if not raw:
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = k.decode() if isinstance(k, bytes) else str(k)
        val = v.decode() if isinstance(v, bytes) else v
        out[key] = val
    return out
