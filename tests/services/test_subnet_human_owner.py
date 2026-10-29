"""Human subnet owners are governance identities, not members."""

from __future__ import annotations

import pytest

from acn.services.subnet_service import SubnetService


class _MemRepo:
    def __init__(self) -> None:
        self.rows = {}

    async def exists(self, slug: str) -> bool:
        return slug in self.rows

    async def save(self, subnet) -> None:
        self.rows[subnet.slug] = subnet

    async def find_by_id(self, slug: str):
        return self.rows.get(slug)


@pytest.mark.asyncio
async def test_human_owner_is_not_a_member():
    repo = _MemRepo()
    service = SubnetService(repo)
    subnet = await service.create_subnet(
        slug="chat-abc",
        name="Group",
        owner="auth0|neil",
        owner_kind="human",
        include_owner_as_member=False,
        is_private=True,
        join_policy="approval",
    )
    assert subnet.owner_kind == "human"
    assert subnet.owner == "auth0|neil"
    assert "auth0|neil" not in subnet.member_agent_ids


@pytest.mark.asyncio
async def test_agent_owner_stays_a_member():
    repo = _MemRepo()
    service = SubnetService(repo)
    subnet = await service.create_subnet(
        slug="plain",
        name="Plain",
        owner="agt_1",
    )
    assert subnet.owner_kind == "agent"
    assert "agt_1" in subnet.member_agent_ids
