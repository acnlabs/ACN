"""Redis implementation of IOrgRepository."""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as redis  # type: ignore[import-untyped]

from ....core.entities.org import Org, OrgMembership, OrgWorkItem
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

    async def save_org(self, org: Org) -> None:
        old = await self.find_org(org.org_id)
        payload = json.dumps(org.to_dict())
        await self.redis.set(_org_key(org.org_id), payload)
        if old and old.steward_agent_id != org.steward_agent_id:
            await self.redis.srem(
                _org_steward_idx(old.steward_agent_id), org.org_id
            )
        await self.redis.sadd(_org_steward_idx(org.steward_agent_id), org.org_id)
        if old and old.subnet_id != org.subnet_id:
            await self.redis.delete(_org_subnet_idx(old.subnet_id))
        await self.redis.set(_org_subnet_idx(org.subnet_id), org.org_id)

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
        await self.redis.delete(_org_subnet_idx(org.subnet_id))
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
