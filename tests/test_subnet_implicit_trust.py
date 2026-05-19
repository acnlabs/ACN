"""Tests for subnet co-membership implicit trust in communication policy.

When two agents share a non-reserved subnet (not "public" or "system"),
manifest and allowlist modes should route to inbox instead of manifest,
enabling direct delivery between subnet peers.
"""

import pytest

from acn.services.policy_service import PolicyCheckService, PolicyDecision


@pytest.fixture
def policy_service():
    return PolicyCheckService()


class TestSubnetImplicitTrust:
    """Verify that shared_subnet_ids bypasses manifest/allowlist divert."""

    async def test_manifest_mode_bypassed_with_shared_subnet(self, policy_service):
        decision = await policy_service.check_inbound(
            sender_id="agent-a",
            recipient_id="agent-b",
            recipient_policy={"mode": "manifest"},
            shared_subnet_ids={"my-subnet"},
        )
        assert decision.allow is True
        assert decision.route_to == "inbox"

    async def test_manifest_mode_not_bypassed_without_shared_subnet(self, policy_service):
        decision = await policy_service.check_inbound(
            sender_id="agent-a",
            recipient_id="agent-b",
            recipient_policy={"mode": "manifest"},
            shared_subnet_ids=None,
        )
        assert decision.allow is True
        assert decision.route_to == "manifest"

    async def test_manifest_mode_not_bypassed_with_empty_set(self, policy_service):
        decision = await policy_service.check_inbound(
            sender_id="agent-a",
            recipient_id="agent-b",
            recipient_policy={"mode": "manifest"},
            shared_subnet_ids=set(),
        )
        assert decision.allow is True
        assert decision.route_to == "manifest"

    async def test_allowlist_mode_bypassed_with_shared_subnet(self, policy_service):
        decision = await policy_service.check_inbound(
            sender_id="agent-a",
            recipient_id="agent-b",
            recipient_policy={"mode": "allowlist"},
            shared_subnet_ids={"team-subnet"},
        )
        assert decision.allow is True
        assert decision.route_to == "inbox"

    async def test_allowlist_mode_not_bypassed_without_shared_subnet(self, policy_service):
        async def fake_allowlist(recipient_id, sender_id):
            return False

        decision = await policy_service.check_inbound(
            sender_id="agent-a",
            recipient_id="agent-b",
            recipient_policy={"mode": "allowlist"},
            shared_subnet_ids=None,
            is_in_allowlist=fake_allowlist,
        )
        assert decision.allow is True
        assert decision.route_to == "manifest"

    async def test_open_mode_unaffected_by_shared_subnet(self, policy_service):
        decision = await policy_service.check_inbound(
            sender_id="agent-a",
            recipient_id="agent-b",
            recipient_policy={"mode": "open"},
            shared_subnet_ids={"my-subnet"},
        )
        assert decision.allow is True
        assert decision.route_to == "inbox"

    async def test_closed_mode_unaffected_by_shared_subnet(self, policy_service):
        decision = await policy_service.check_inbound(
            sender_id="agent-a",
            recipient_id="agent-b",
            recipient_policy={"mode": "closed"},
            shared_subnet_ids={"my-subnet"},
        )
        assert decision.allow is False
        assert decision.reason == "policy_closed"

    async def test_system_sender_still_bypasses_everything(self, policy_service):
        decision = await policy_service.check_inbound(
            sender_id="system:notifications",
            recipient_id="agent-b",
            recipient_policy={"mode": "manifest"},
            shared_subnet_ids=None,
        )
        assert decision.allow is True

    async def test_multiple_shared_subnets(self, policy_service):
        decision = await policy_service.check_inbound(
            sender_id="agent-a",
            recipient_id="agent-b",
            recipient_policy={"mode": "manifest"},
            shared_subnet_ids={"subnet-1", "subnet-2"},
        )
        assert decision.allow is True
        assert decision.route_to == "inbox"

    async def test_only_reserved_subnets_shared_no_bypass(self, policy_service):
        """If after removing reserved subnets the set is empty, no bypass."""
        decision = await policy_service.check_inbound(
            sender_id="agent-a",
            recipient_id="agent-b",
            recipient_policy={"mode": "manifest"},
            shared_subnet_ids=set(),  # caller already stripped reserved
        )
        assert decision.allow is True
        assert decision.route_to == "manifest"
