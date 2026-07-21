"""Redis implementation of IOrgRepository.

Assumes the shared production client is created with
``decode_responses=True`` (see ``acn/api.py`` lifespan) — all reads
below compare/compose raw ``str`` values.

Fence-binding atomicity (ADR-0014 "one Org per subnet")
-------------------------------------------------------
Postgres enforces the invariant with a unique index; Redis has no
unique constraint, so the ``acn:orgs:by_subnet:{slug}`` pointer is
claimed with ``SET NX`` inside :meth:`save_org`. A plain ``SET`` here
(the original implementation) let two concurrent ``create_org`` calls
both pass the service-level pre-check and both bind the same subnet —
last writer silently won.

Rules:

- New binding (new org, or ``subnet_id`` changed): ``SET NX``. On
  failure the current holder is inspected — a **dangling** pointer
  (org payload deleted) or a **dissolved** holder is released and the
  claim retried once; an active holder raises
  :class:`OrgSubnetBindingConflictError`.
- ``status="dissolved"`` saves release the pointer (only when it still
  points at this org), so a dissolved Org's subnet is immediately
  rebindable — mirroring the Postgres partial-unique-index semantics.
- :meth:`delete_org` only deletes the pointer when it points at the
  org being deleted (a stale-takeover may have re-pointed it).

The release-then-retry window on stale/dissolved takeover is not
atomic (no Lua — fakeredis in CI has no interpreter), but it can only
race another *takeover of an already-dead binding*; the fresh-create
vs fresh-create race that mattered is closed by ``SET NX``.
"""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as redis  # type: ignore[import-untyped]

from ....core.entities.org import Org, OrgMembership, OrgWorkItem
from ....core.exceptions import OrgSubnetBindingConflictError
from ....core.interfaces.org_repository import IOrgRepository


def _org_key(org_id: str) -> str:
    return f"acn:org:{org_id}"


def _org_steward_idx(steward_agent_id: str) -> str:
    return f"acn:orgs:by_steward:{steward_agent_id}"


def _org_subnet_idx(subnet_id: str) -> str:
    return f"acn:orgs:by_subnet:{subnet_id}"


def _members_key(org_id: str) -> str:
    return f"acn:org_members:{org_id}"


def _member_key(org_id: str, agent_id: str) -> str:
    return f"acn:org_member:{org_id}:{agent_id}"


def _work_set_key(org_id: str) -> str:
    return f"acn:org_work:{org_id}"


def _work_key(org_id: str, work_id: str) -> str:
    return f"acn:org_work_item:{org_id}:{work_id}"


class RedisOrgRepository(IOrgRepository):
    def __init__(self, redis_client: redis.Redis) -> None:
        self.redis = redis_client

    async def _claim_subnet_binding(self, org: Org) -> None:
        """Claim ``acn:orgs:by_subnet`` for ``org`` with SET NX semantics.

        Raises ``OrgSubnetBindingConflictError`` when another **active**
        Org already holds the pointer. Dangling (payload gone) and
        dissolved holders are evicted, then the claim is retried once.
        """
        idx_key = _org_subnet_idx(org.subnet_id)
        for _ in range(2):
            claimed = await self.redis.set(idx_key, org.org_id, nx=True)
            if claimed:
                return
            holder_id = await self.redis.get(idx_key)
            if holder_id is None:
                continue  # holder vanished between SET NX and GET — retry
            if holder_id == org.org_id:
                return  # already ours (idempotent re-save)
            holder = await self.find_org(holder_id)
            if holder is None or holder.status == "dissolved":
                # Dead binding — release and retry the NX claim once.
                await self.redis.delete(idx_key)
                continue
            raise OrgSubnetBindingConflictError(org.subnet_id, holder_id)
        # Second NX attempt also lost: a concurrent claimer won the race.
        holder_id = await self.redis.get(idx_key)
        if holder_id and holder_id != org.org_id:
            raise OrgSubnetBindingConflictError(org.subnet_id, holder_id)

    async def save_org(self, org: Org) -> None:
        old = await self.find_org(org.org_id)

        # Claim the fence pointer BEFORE persisting the org payload so a
        # lost race leaves no orphan org row behind.
        binding_is_new = old is None or old.subnet_id != org.subnet_id
        if binding_is_new and org.status != "dissolved":
            await self._claim_subnet_binding(org)

        payload = json.dumps(org.to_dict())
        await self.redis.set(_org_key(org.org_id), payload)
        if old and old.steward_agent_id != org.steward_agent_id:
            await self.redis.srem(
                _org_steward_idx(old.steward_agent_id), org.org_id
            )
        await self.redis.sadd(_org_steward_idx(org.steward_agent_id), org.org_id)
        if old and old.subnet_id != org.subnet_id:
            await self._release_subnet_binding(old.subnet_id, org.org_id)
        if org.status == "dissolved":
            # Dissolution releases the fence immediately so the subnet can
            # be rebound (service allows rebinding over dissolved orgs).
            await self._release_subnet_binding(org.subnet_id, org.org_id)

    async def _release_subnet_binding(self, subnet_id: str, org_id: str) -> None:
        """Delete the subnet pointer only if it still points at ``org_id``."""
        idx_key = _org_subnet_idx(subnet_id)
        holder = await self.redis.get(idx_key)
        if holder == org_id:
            await self.redis.delete(idx_key)

    async def find_org(self, org_id: str) -> Org | None:
        raw = await self.redis.get(_org_key(org_id))
        if not raw:
            return None
        data: dict[str, Any] = json.loads(raw)
        return Org.from_dict(data)

    async def delete_org(self, org_id: str) -> bool:
        org = await self.find_org(org_id)
        if not org:
            return False
        await self.redis.delete(_org_key(org_id))
        await self.redis.srem(_org_steward_idx(org.steward_agent_id), org_id)
        # Guarded release — the pointer may have been legitimately taken
        # over by another org after this one dissolved.
        await self._release_subnet_binding(org.subnet_id, org_id)
        return True

    async def list_orgs_by_steward(self, steward_agent_id: str) -> list[Org]:
        ids = await self.redis.smembers(_org_steward_idx(steward_agent_id))
        out: list[Org] = []
        for org_id in ids:
            org = await self.find_org(org_id)
            if org:
                out.append(org)
        return out

    async def find_org_by_subnet(self, subnet_id: str) -> Org | None:
        org_id = await self.redis.get(_org_subnet_idx(subnet_id))
        if not org_id:
            return None
        return await self.find_org(org_id)

    async def upsert_membership(self, membership: OrgMembership) -> None:
        await self.redis.set(
            _member_key(membership.org_id, membership.agent_id),
            json.dumps(membership.to_dict()),
        )
        await self.redis.sadd(_members_key(membership.org_id), membership.agent_id)

    async def find_membership(
        self, org_id: str, agent_id: str
    ) -> OrgMembership | None:
        raw = await self.redis.get(_member_key(org_id, agent_id))
        if not raw:
            return None
        return OrgMembership.from_dict(json.loads(raw))

    async def list_memberships(
        self, org_id: str, *, active_only: bool = True
    ) -> list[OrgMembership]:
        agent_ids = await self.redis.smembers(_members_key(org_id))
        out: list[OrgMembership] = []
        for agent_id in agent_ids:
            m = await self.find_membership(org_id, agent_id)
            if m is None:
                continue
            if active_only and m.status != "active":
                continue
            out.append(m)
        return out

    async def delete_memberships_for_org(self, org_id: str) -> int:
        agent_ids = await self.redis.smembers(_members_key(org_id))
        count = 0
        for agent_id in agent_ids:
            deleted = await self.redis.delete(_member_key(org_id, agent_id))
            count += int(deleted or 0)
        await self.redis.delete(_members_key(org_id))
        return count

    async def save_work(self, work: OrgWorkItem) -> None:
        await self.redis.set(
            _work_key(work.org_id, work.work_id),
            json.dumps(work.to_dict()),
        )
        await self.redis.sadd(_work_set_key(work.org_id), work.work_id)

    async def find_work(self, org_id: str, work_id: str) -> OrgWorkItem | None:
        raw = await self.redis.get(_work_key(org_id, work_id))
        if not raw:
            return None
        return OrgWorkItem.from_dict(json.loads(raw))

    async def list_work(
        self,
        org_id: str,
        *,
        open_only: bool = False,
    ) -> list[OrgWorkItem]:
        work_ids = await self.redis.smembers(_work_set_key(org_id))
        out: list[OrgWorkItem] = []
        for work_id in work_ids:
            w = await self.find_work(org_id, work_id)
            if w is None:
                continue
            if open_only and w.status not in ("todo", "in_progress"):
                continue
            out.append(w)
        return out

    async def delete_work_for_org(self, org_id: str) -> int:
        work_ids = await self.redis.smembers(_work_set_key(org_id))
        count = 0
        for work_id in work_ids:
            deleted = await self.redis.delete(_work_key(org_id, work_id))
            count += int(deleted or 0)
        await self.redis.delete(_work_set_key(org_id))
        return count
