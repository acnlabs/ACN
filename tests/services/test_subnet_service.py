"""Unit Tests for SubnetService

Regression tests for the subnet lifecycle business logic.
"""

import pytest

from acn.services import SubnetService
from acn.services.subnet_service import (
    REASON_VISIBILITY_POLICY_CONFLICT,
    SubnetInvariantError,
)


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
            slug="subnet-test",
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
            slug="subnet-test",
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
                slug="subnet-test",
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
                slug="ws-mirror-001",
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
            slug="ws-mirror-002",
            name="Workspace Mirror",
            owner="svc-backend-prod-agent-uuid",
        )
        assert subnet.owner == "svc-backend-prod-agent-uuid"
        mock_subnet_repository.save.assert_called_once()


class TestSubnetServiceADR0004JoinPolicy:
    """ADR-0004: ``SubnetService.create_subnet`` resolves ``join_policy``
    so the entity invariant (``is_private=True`` ⇒
    ``join_policy='approval'``) never trips through normal callers.

    Two cohorts of callers exist:

    1. **Legacy clients** — flip ``is_private=True`` without knowing
       ``join_policy`` exists. The service infers ``'approval'`` for
       them; their create succeeds with the post-ADR-0004 admission
       semantic in place. Without this inference, every existing
       ``POST /api/v1/subnets`` body with ``"is_private": true`` would
       500 on the entity-layer invariant.
    2. **Forward-aware clients** — pass ``join_policy`` explicitly.
       The service trusts the explicit value but raises a structured
       ``SubnetInvariantError(visibility_policy_conflict)`` if the
       caller knowingly sends the rejected combination, instead of
       letting the entity's bare ``ValueError`` bubble up as a
       free-form message.
    """

    @pytest.mark.asyncio
    async def test_omitted_join_policy_on_public_subnet_defaults_to_open(
        self, mock_subnet_repository
    ):
        mock_subnet_repository.exists.return_value = False
        service = SubnetService(mock_subnet_repository)

        subnet = await service.create_subnet(
            slug="subnet-pub-default",
            name="Public Default",
            owner="agent-1",
            is_private=False,
        )
        assert subnet.is_private is False
        assert subnet.join_policy == "open"

    @pytest.mark.asyncio
    async def test_omitted_join_policy_on_private_subnet_auto_upgrades_to_approval(
        self, mock_subnet_repository
    ):
        """**Critical backward-compat path.** Existing
        ``POST /api/v1/subnets`` callers send ``"is_private": true``
        without ``join_policy`` (the field didn't exist before this
        change). The service must auto-infer ``'approval'`` so the
        entity invariant accepts the row — otherwise every legacy
        caller breaks the day this lands."""
        mock_subnet_repository.exists.return_value = False
        service = SubnetService(mock_subnet_repository)

        subnet = await service.create_subnet(
            slug="subnet-priv-default",
            name="Private Default",
            owner="agent-1",
            is_private=True,
        )
        assert subnet.is_private is True
        assert subnet.join_policy == "approval"

    @pytest.mark.asyncio
    async def test_explicit_approval_on_public_subnet_accepted(
        self, mock_subnet_repository
    ):
        """Public + approval (curated community board) is one of the
        three combinations ADR-0004 explicitly permits."""
        mock_subnet_repository.exists.return_value = False
        service = SubnetService(mock_subnet_repository)

        subnet = await service.create_subnet(
            slug="subnet-pub-approval",
            name="Public Approval",
            owner="agent-1",
            is_private=False,
            join_policy="approval",
        )
        assert subnet.is_private is False
        assert subnet.join_policy == "approval"

    @pytest.mark.asyncio
    async def test_explicit_open_on_private_subnet_rejected_with_stable_reason(
        self, mock_subnet_repository
    ):
        """The ``visibility_policy_conflict`` rejection surfaces as a
        ``SubnetInvariantError`` with the stable reason token — clients
        / CLI / SDK parsers pin against ``details.reason`` and must
        not have to scrape a free-form message."""
        mock_subnet_repository.exists.return_value = False
        service = SubnetService(mock_subnet_repository)

        with pytest.raises(SubnetInvariantError) as exc_info:
            await service.create_subnet(
                slug="subnet-priv-open",
                name="Private Open",
                owner="agent-1",
                is_private=True,
                join_policy="open",
            )
        assert exc_info.value.reason == REASON_VISIBILITY_POLICY_CONFLICT
        # No subnet should have been persisted on the rejection path.
        mock_subnet_repository.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_explicit_approval_on_private_subnet_accepted(
        self, mock_subnet_repository
    ):
        mock_subnet_repository.exists.return_value = False
        service = SubnetService(mock_subnet_repository)

        subnet = await service.create_subnet(
            slug="subnet-priv-approval",
            name="Private Approval",
            owner="agent-1",
            is_private=True,
            join_policy="approval",
        )
        assert subnet.is_private is True
        assert subnet.join_policy == "approval"

    @pytest.mark.asyncio
    async def test_explicit_open_on_public_subnet_accepted(
        self, mock_subnet_repository
    ):
        """Sanity: explicit ``open`` on a public subnet behaves
        identically to the default. Pinned to catch a regression
        where the "explicit" branch accidentally short-circuits the
        accept path for public+open."""
        mock_subnet_repository.exists.return_value = False
        service = SubnetService(mock_subnet_repository)

        subnet = await service.create_subnet(
            slug="subnet-pub-open-explicit",
            name="Public Open Explicit",
            owner="agent-1",
            is_private=False,
            join_policy="open",
        )
        assert subnet.is_private is False
        assert subnet.join_policy == "open"
