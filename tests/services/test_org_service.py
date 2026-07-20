"""Unit tests for OrgService (ADR-0014 Kernel + minimal work + thin Loop)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from acn.core.entities.agent import Agent
from acn.core.entities.org import Org, OrgMembership, OrgOwner, OrgPrincipal
from acn.core.entities.subnet import Subnet
from acn.core.exceptions import SubnetNotFoundException
from acn.core.interfaces.org_repository import IOrgRepository
from acn.protocols.ap2 import WebhookEventType
from acn.services.org_service import (
    OrgConflictError,
    OrgPermissionError,
    OrgService,
)


@pytest.fixture
def mock_org_repo():
    repo = AsyncMock(spec=IOrgRepository)
    repo.find_org_by_subnet = AsyncMock(return_value=None)
    return repo


@pytest.fixture
def mock_subnet_service():
    svc = AsyncMock()
    svc.get_subnet = AsyncMock(side_effect=SubnetNotFoundException("missing"))
    svc.delete_subnet = AsyncMock(return_value=True)
    svc.transfer_owner = AsyncMock()
    return svc


@pytest.fixture
def mock_agent_service():
    svc = AsyncMock()
    # agent_id → human owner (None = unclaimed)
    ownership: dict[str, str | None] = {
        "agt_steward": "auth0|u",
        "agt_new": "auth0|u",
        "agt_victim": "auth0|other",
        "agt_worker": None,
    }

    async def _get_agent(agent_id: str) -> Agent:
        return Agent(
            agent_id=agent_id,
            name=agent_id,
            endpoint="https://example.com",
            owner=ownership.get(agent_id),
        )

    svc.get_agent = AsyncMock(side_effect=_get_agent)
    svc.join_subnet = AsyncMock()
    svc.leave_subnet = AsyncMock()
    return svc


@pytest.fixture
def mock_webhook():
    return AsyncMock()


@pytest.fixture
def org_service(mock_org_repo, mock_subnet_service, mock_agent_service, mock_webhook):
    return OrgService(
        org_repository=mock_org_repo,
        subnet_service=mock_subnet_service,
        agent_service=mock_agent_service,
        webhook_service=mock_webhook,
    )


def _stored_org(**overrides) -> Org:
    defaults = dict(
        org_id="org_test",
        display_name="Test Org",
        created_by=OrgPrincipal(kind="agent", subject="agt_steward"),
        subnet_id="org-test-abc",
        steward_agent_id="agt_steward",
        owner=OrgOwner(kind="none"),
    )
    defaults.update(overrides)
    return Org(**defaults)


class TestCreateOrg:
    async def test_agent_create_binds_subnet(
        self, org_service, mock_org_repo, mock_subnet_service, mock_webhook
    ):
        subnet = Subnet(
            slug="org-demo-1",
            name="Demo",
            owner="agt_steward",
            member_agent_ids={"agt_steward"},
            harness_url="https://hook.example/acn",
            harness_secret="sec",
        )
        # First lookup during create → miss; emit path → hit.
        mock_subnet_service.get_subnet = AsyncMock(
            side_effect=[SubnetNotFoundException("x"), subnet]
        )
        mock_subnet_service.create_subnet = AsyncMock(return_value=subnet)

        org = await org_service.create_org(
            display_name="Demo",
            caller_type="agent",
            caller_sub="agt_steward",
            subnet_id="org-demo-1",
        )

        assert org.owner.kind == "none"
        assert org.created_by.subject == "agt_steward"
        assert org.steward_agent_id == "agt_steward"
        assert org.subnet_id == "org-demo-1"
        mock_org_repo.save_org.assert_awaited()
        mock_org_repo.upsert_membership.assert_awaited()
        mock_webhook.send_to.assert_awaited()
        event = mock_webhook.send_to.await_args.kwargs["event"]
        assert event == WebhookEventType.ORG_CREATED

    async def test_human_create_requires_steward(self, org_service):
        with pytest.raises(ValueError, match="steward_agent_id"):
            await org_service.create_org(
                display_name="H",
                caller_type="human",
                caller_sub="auth0|user1",
            )

    async def test_human_cannot_use_unowned_steward(self, org_service):
        with pytest.raises(OrgPermissionError) as ei:
            await org_service.create_org(
                display_name="Hijack",
                caller_type="human",
                caller_sub="auth0|attacker",
                steward_agent_id="agt_victim",
            )
        assert ei.value.reason == "steward_not_owned"

    async def test_human_create_with_owned_steward(
        self, org_service, mock_org_repo, mock_subnet_service
    ):
        subnet = Subnet(
            slug="org-owned-1",
            name="Owned",
            owner="agt_steward",
            member_agent_ids={"agt_steward"},
        )
        mock_subnet_service.get_subnet = AsyncMock(
            side_effect=[SubnetNotFoundException("x"), subnet]
        )
        mock_subnet_service.create_subnet = AsyncMock(return_value=subnet)

        org = await org_service.create_org(
            display_name="Owned",
            caller_type="human",
            caller_sub="auth0|u",
            steward_agent_id="agt_steward",
            subnet_id="org-owned-1",
        )
        assert org.created_by.kind == "human"
        assert org.steward_agent_id == "agt_steward"

    async def test_subnet_already_bound_rejected(
        self, org_service, mock_org_repo, mock_subnet_service
    ):
        subnet = Subnet(slug="taken", name="T", owner="agt_steward")
        mock_subnet_service.get_subnet = AsyncMock(return_value=subnet)
        mock_org_repo.find_org_by_subnet.return_value = _stored_org(
            org_id="org_other", subnet_id="taken"
        )
        with pytest.raises(OrgConflictError) as ei:
            await org_service.create_org(
                display_name="Dup",
                caller_type="agent",
                caller_sub="agt_steward",
                subnet_id="taken",
            )
        assert ei.value.reason == "subnet_already_bound"

    async def test_create_rolls_back_new_subnet_on_save_fail(
        self, org_service, mock_org_repo, mock_subnet_service
    ):
        subnet = Subnet(slug="org-rb", name="RB", owner="agt_steward")
        mock_subnet_service.get_subnet = AsyncMock(
            side_effect=SubnetNotFoundException("x")
        )
        mock_subnet_service.create_subnet = AsyncMock(return_value=subnet)
        mock_org_repo.save_org.side_effect = RuntimeError("db down")

        with pytest.raises(RuntimeError, match="db down"):
            await org_service.create_org(
                display_name="RB",
                caller_type="agent",
                caller_sub="agt_steward",
                subnet_id="org-rb",
            )
        mock_subnet_service.delete_subnet.assert_awaited_once_with(
            "org-rb", "agt_steward"
        )


class TestMembership:
    async def test_add_member_joins_subnet_first(
        self, org_service, mock_org_repo, mock_subnet_service
    ):
        org = _stored_org()
        mock_org_repo.find_org.return_value = org
        mock_org_repo.find_membership.return_value = None
        subnet = Subnet(
            slug=org.subnet_id,
            name="T",
            owner="agt_steward",
            member_agent_ids={"agt_steward"},
        )
        mock_subnet_service.get_subnet = AsyncMock(return_value=subnet)
        mock_subnet_service.add_member = AsyncMock(return_value=subnet)

        m = await org_service.add_member(
            org.org_id,
            "agt_worker",
            caller_type="agent",
            caller_sub="agt_steward",
            role="worker",
        )
        assert m.agent_id == "agt_worker"
        mock_subnet_service.add_member.assert_awaited_once()
        mock_org_repo.upsert_membership.assert_awaited()

    async def test_add_member_compensate_on_upsert_fail(
        self, org_service, mock_org_repo, mock_subnet_service
    ):
        org = _stored_org()
        mock_org_repo.find_org.return_value = org
        mock_org_repo.find_membership.return_value = None
        mock_org_repo.upsert_membership.side_effect = RuntimeError("db down")
        subnet = Subnet(
            slug=org.subnet_id,
            name="T",
            owner="agt_steward",
            member_agent_ids={"agt_steward"},
        )
        mock_subnet_service.get_subnet = AsyncMock(return_value=subnet)
        mock_subnet_service.add_member = AsyncMock(return_value=subnet)
        mock_subnet_service.remove_member = AsyncMock(return_value=subnet)

        with pytest.raises(RuntimeError, match="db down"):
            await org_service.add_member(
                org.org_id,
                "agt_worker",
                caller_type="agent",
                caller_sub="agt_steward",
            )
        mock_subnet_service.remove_member.assert_awaited_once()


class TestOwnership:
    async def test_none_owner_only_created_by_can_claim(
        self, org_service, mock_org_repo, mock_subnet_service
    ):
        org = _stored_org()
        mock_org_repo.find_org.return_value = org
        mock_subnet_service.get_subnet = AsyncMock(
            return_value=Subnet(
                slug=org.subnet_id, name="T", owner="agt_steward"
            )
        )

        with pytest.raises(OrgPermissionError):
            await org_service.claim(
                org.org_id,
                caller_type="agent",
                caller_sub="agt_other",
            )

        claimed = await org_service.claim(
            org.org_id,
            caller_type="agent",
            caller_sub="agt_steward",
        )
        assert claimed.owner.kind == "agent"
        assert claimed.owner.subject == "agt_steward"

    async def test_claim_agent_transfers_subnet(
        self, org_service, mock_org_repo, mock_subnet_service
    ):
        org = _stored_org(
            created_by=OrgPrincipal(kind="human", subject="auth0|u"),
            steward_agent_id="agt_steward",
        )
        mock_org_repo.find_org.return_value = org
        mock_subnet_service.get_subnet = AsyncMock(
            return_value=Subnet(
                slug=org.subnet_id, name="T", owner="agt_steward"
            )
        )
        mock_subnet_service.transfer_owner = AsyncMock(
            return_value=Subnet(
                slug=org.subnet_id, name="T", owner="agt_new"
            )
        )

        result = await org_service.claim(
            org.org_id,
            caller_type="human",
            caller_sub="auth0|u",
            owner_kind="agent",
            owner_subject="agt_new",
        )
        assert result.owner.subject == "agt_new"
        mock_subnet_service.transfer_owner.assert_awaited_once()

    async def test_claim_cannot_designate_unowned_agent(
        self, org_service, mock_org_repo
    ):
        org = _stored_org(
            created_by=OrgPrincipal(kind="human", subject="auth0|u"),
            steward_agent_id="agt_steward",
        )
        mock_org_repo.find_org.return_value = org
        with pytest.raises(OrgPermissionError) as ei:
            await org_service.claim(
                org.org_id,
                caller_type="human",
                caller_sub="auth0|u",
                owner_kind="agent",
                owner_subject="agt_victim",
            )
        assert ei.value.reason == "owner_agent_not_owned"

    async def test_claim_compensates_subnet_on_save_fail(
        self, org_service, mock_org_repo, mock_subnet_service
    ):
        org = _stored_org(
            created_by=OrgPrincipal(kind="human", subject="auth0|u"),
            steward_agent_id="agt_steward",
        )
        mock_org_repo.find_org.return_value = org
        mock_org_repo.save_org.side_effect = RuntimeError("persist fail")
        mock_subnet_service.get_subnet = AsyncMock(
            return_value=Subnet(
                slug=org.subnet_id, name="T", owner="agt_steward"
            )
        )
        mock_subnet_service.transfer_owner = AsyncMock(
            return_value=Subnet(
                slug=org.subnet_id, name="T", owner="agt_new"
            )
        )

        with pytest.raises(RuntimeError, match="persist fail"):
            await org_service.claim(
                org.org_id,
                caller_type="human",
                caller_sub="auth0|u",
                owner_kind="agent",
                owner_subject="agt_new",
            )
        # transfer forward + compensate reverse
        assert mock_subnet_service.transfer_owner.await_count == 2
        second = mock_subnet_service.transfer_owner.await_args_list[1]
        assert second.kwargs["new_owner"] == "agt_steward"
        assert second.kwargs["current_owner"] == "agt_new"


class TestWorkAndLoop:
    async def test_create_work_and_tick(
        self, org_service, mock_org_repo, mock_subnet_service, mock_webhook
    ):
        org = _stored_org()
        mock_org_repo.find_org.return_value = org
        mock_subnet_service.get_subnet = AsyncMock(
            return_value=Subnet(
                slug=org.subnet_id,
                name="T",
                owner="agt_steward",
                harness_url="https://hook.example/acn",
                harness_secret="s",
            )
        )

        work = await org_service.create_work(
            org.org_id,
            title="Ship it",
            caller_type="agent",
            caller_sub="agt_steward",
            assignee_agent_id="agt_steward",
        )
        assert work.status == "todo"
        mock_org_repo.save_work.assert_awaited()

        mock_org_repo.list_work.return_value = [work]
        tick = await org_service.tick_loop(
            org.org_id,
            caller_type="agent",
            caller_sub="agt_steward",
        )
        assert tick["open_count"] == 1
        events = [c.kwargs["event"] for c in mock_webhook.send_to.await_args_list]
        assert WebhookEventType.ORG_WORK_CREATED in events
        assert WebhookEventType.ORG_LOOP_TICK in events


class TestUpdateAndMembersView:
    async def test_update_org_charter_and_plugins(
        self, org_service, mock_org_repo
    ):
        org = _stored_org()
        mock_org_repo.find_org.return_value = org
        updated = await org_service.update_org(
            org.org_id,
            caller_type="agent",
            caller_sub="agt_steward",
            display_name="Renamed",
            charter={"mission": "ship"},
            plugins={"loop": "heartbeat"},
        )
        assert updated.display_name == "Renamed"
        assert updated.charter["mission"] == "ship"
        assert updated.plugins["loop"] == "heartbeat"
        assert updated.plugins["work"] == "minimal"
        mock_org_repo.save_org.assert_awaited()

    async def test_list_members_marks_degraded(
        self, org_service, mock_org_repo, mock_subnet_service
    ):
        org = _stored_org()
        mock_org_repo.find_org.return_value = org
        mock_org_repo.list_memberships.return_value = [
            OrgMembership(
                org_id=org.org_id, agent_id="agt_steward", role="manager"
            ),
            OrgMembership(
                org_id=org.org_id, agent_id="agt_worker", role="worker"
            ),
        ]
        mock_subnet_service.get_subnet = AsyncMock(
            return_value=Subnet(
                slug=org.subnet_id,
                name="T",
                owner="agt_steward",
                member_agent_ids={"agt_steward"},  # worker missing from fence
            )
        )
        view = await org_service.list_members_view(org.org_id)
        assert view["degraded_count"] == 1
        by_id = {m["agent_id"]: m for m in view["members"]}
        assert by_id["agt_steward"]["acn"]["degraded"] is False
        assert by_id["agt_worker"]["acn"]["degraded"] is True
        assert by_id["agt_worker"]["acn"]["subnet_member"] is False

    async def test_create_registers_harness(
        self, org_service, mock_org_repo, mock_subnet_service
    ):
        subnet = Subnet(
            slug="org-h",
            name="H",
            owner="agt_steward",
            member_agent_ids={"agt_steward"},
        )
        mock_subnet_service.get_subnet = AsyncMock(
            side_effect=[SubnetNotFoundException("x"), subnet]
        )
        mock_subnet_service.create_subnet = AsyncMock(return_value=subnet)
        mock_subnet_service.update_harness = AsyncMock(return_value=subnet)

        await org_service.create_org(
            display_name="H",
            caller_type="agent",
            caller_sub="agt_steward",
            subnet_id="org-h",
            harness_url="https://hook.example/acn",
            harness_secret="s3cret",
        )
        mock_subnet_service.update_harness.assert_awaited_once()


class TestGovernanceGate:
    async def test_stranger_cannot_update(self, org_service, mock_org_repo):
        org = _stored_org()
        mock_org_repo.find_org.return_value = org
        with pytest.raises(OrgPermissionError):
            await org_service.create_work(
                org.org_id,
                title="x",
                caller_type="agent",
                caller_sub="agt_stranger",
            )

    async def test_already_member_conflict(
        self, org_service, mock_org_repo, mock_subnet_service
    ):
        org = _stored_org()
        mock_org_repo.find_org.return_value = org
        mock_org_repo.find_membership.return_value = OrgMembership(
            org_id=org.org_id, agent_id="agt_worker", status="active"
        )
        with pytest.raises(OrgConflictError):
            await org_service.add_member(
                org.org_id,
                "agt_worker",
                caller_type="agent",
                caller_sub="agt_steward",
            )
