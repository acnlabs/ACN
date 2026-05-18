"""Unit Tests for SubnetService

Regression tests for the subnet lifecycle business logic.
"""

import pytest

from acn.services import SubnetService


class TestSubnetServiceCreate:
    """create_subnet adds the owner as a member by construction.

    Regression target: before this fix, ``Subnet`` was persisted with an
    empty ``member_agent_ids`` set, so every ACL keyed off member-list
    membership (e.g. private GET ``/api/v1/tasks/{id}``, ``accept_task``'s
    ``is_subnet_member`` check) rejected the owner from acting on the
    subnet they themselves had just created. This silently broke the
    canonical Org-Harness bootstrap flow (create_subnet → register_harness
    → create_task) and was only visible E2E.
    """

    @pytest.mark.asyncio
    async def test_create_subnet_seeds_owner_as_member(self, mock_subnet_repository):
        """The owner is implicitly a member at create time."""
        # No existing subnet so the create proceeds.
        mock_subnet_repository.exists.return_value = False

        service = SubnetService(mock_subnet_repository)
        subnet = await service.create_subnet(
            subnet_id="subnet-test",
            name="Test Subnet",
            owner="agent-owner-123",
        )

        assert "agent-owner-123" in subnet.member_agent_ids, (
            "owner must be a member of their own subnet at creation time; "
            "otherwise every member-list ACL (private GET task, accept_task, "
            "is_subnet_member) trivially 403s the owner."
        )
        mock_subnet_repository.save.assert_called_once()
        # save() received the SAME subnet instance with the member set; this
        # rules out a regression where the entity is mutated only AFTER save.
        saved_subnet = mock_subnet_repository.save.call_args.args[0]
        assert "agent-owner-123" in saved_subnet.member_agent_ids

    @pytest.mark.asyncio
    async def test_create_subnet_preserves_only_owner_at_member_set(
        self, mock_subnet_repository
    ):
        """No additional members are accidentally added."""
        mock_subnet_repository.exists.return_value = False

        service = SubnetService(mock_subnet_repository)
        subnet = await service.create_subnet(
            subnet_id="subnet-test",
            name="Test Subnet",
            owner="agent-owner-123",
        )

        assert subnet.member_agent_ids == {"agent-owner-123"}

    @pytest.mark.asyncio
    async def test_create_subnet_rejects_duplicate_id(self, mock_subnet_repository):
        """Pre-existing subnet causes ValueError; owner not silently overwritten."""
        mock_subnet_repository.exists.return_value = True

        service = SubnetService(mock_subnet_repository)
        with pytest.raises(ValueError, match="already exists"):
            await service.create_subnet(
                subnet_id="subnet-test",
                name="Test Subnet",
                owner="agent-owner-123",
            )
        mock_subnet_repository.save.assert_not_called()


class TestSubnetServiceADR0002:
    """ADR-0002: ``backend@internal`` is rejected as a subnet owner.

    The service-layer guard runs before the existence check so the
    rejection is unconditional — it fires regardless of whether a
    subnet with the same id already exists.
    """

    @pytest.mark.asyncio
    async def test_backend_internal_owner_is_rejected(self, mock_subnet_repository):
        """``backend@internal`` raises ValueError immediately."""
        service = SubnetService(mock_subnet_repository)
        with pytest.raises(ValueError, match="ADR-0002"):
            await service.create_subnet(
                subnet_id="ws-mirror-001",
                name="Workspace Mirror",
                owner="backend@internal",
            )
        mock_subnet_repository.exists.assert_not_called()
        mock_subnet_repository.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_registered_agent_owner_is_accepted(self, mock_subnet_repository):
        """A normal agent_id is not blocked by the ADR-0002 guard."""
        mock_subnet_repository.exists.return_value = False

        service = SubnetService(mock_subnet_repository)
        subnet = await service.create_subnet(
            subnet_id="ws-mirror-002",
            name="Workspace Mirror",
            owner="svc-backend-prod-agent-uuid",
        )
        assert subnet.owner == "svc-backend-prod-agent-uuid"
        mock_subnet_repository.save.assert_called_once()
