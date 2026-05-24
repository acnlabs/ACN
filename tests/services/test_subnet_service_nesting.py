"""SubnetService nesting unit tests (ADR-0003 Phase 2).

Pin the five invariant rejections + membership-subset enforcement +
``promote_to_persistent`` idempotency / owner-only ACL at the service
layer. Repository is mocked so these are pure service-logic tests.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from acn.core.entities import Subnet
from acn.core.interfaces import ISubnetRepository
from acn.core.interfaces.task_repository import ITaskRepository
from acn.services.subnet_service import (
    REASON_LINKED_TASK_NOT_FOUND,
    REASON_NOT_PARENT_MEMBER,
    REASON_PARENT_IS_NESTED,
    REASON_PARENT_IS_RESERVED,
    REASON_PARENT_NOT_FOUND,
    REASON_TASK_SCOPED_REQUIRES_LINKED_TASK,
    SubnetInvariantError,
    SubnetService,
)


def _make_parent_subnet(
    slug: str = "parent",
    owner: str = "alice",
    members: set[str] | None = None,
    parent_slug: str | None = None,
) -> Subnet:
    """Build a parent ``Subnet`` entity for repository mocks.

    Defaults to a top-level subnet owned by ``alice`` with just the
    owner as a member. Override ``members`` to seed extra agents.
    """
    members = members or {owner}
    return Subnet(
        slug=slug,
        name=f"Subnet {slug}",
        owner=owner,
        member_agent_ids=members,
        parent_slug=parent_slug,
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# create_subnet — five invariant rejections
# ---------------------------------------------------------------------------


class TestCreateSubnetNestingInvariants:
    @pytest.mark.asyncio
    async def test_parent_not_found(self, mock_subnet_repository: ISubnetRepository):
        mock_subnet_repository.exists.return_value = False
        mock_subnet_repository.find_by_id.return_value = None
        service = SubnetService(mock_subnet_repository)

        with pytest.raises(SubnetInvariantError) as exc:
            await service.create_subnet(
                slug="child-1",
                name="Squad",
                owner="alice",
                parent_slug="missing-parent",
            )
        assert exc.value.reason == REASON_PARENT_NOT_FOUND
        mock_subnet_repository.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_parent_is_reserved_public(
        self, mock_subnet_repository: ISubnetRepository
    ):
        mock_subnet_repository.exists.return_value = False
        # Even if ``find_by_id`` returns *something*, the ID-based
        # check rejects ``public``/``system`` parents up-front so
        # we don't need to mock a reserved subnet entity.
        service = SubnetService(mock_subnet_repository)
        # Wire find_by_id to return a stub so the ID check is what
        # rejects, not the existence check.
        mock_subnet_repository.find_by_id.return_value = _make_parent_subnet(
            slug="public", owner="system"
        )

        with pytest.raises(SubnetInvariantError) as exc:
            await service.create_subnet(
                slug="child-1",
                name="Squad",
                owner="alice",
                parent_slug="public",
            )
        assert exc.value.reason == REASON_PARENT_IS_RESERVED

    @pytest.mark.asyncio
    async def test_parent_is_reserved_by_owner(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """Defence-in-depth: any subnet owned by ``system`` is treated
        as a reserved parent even if its ID isn't on the literal
        ``public``/``system`` list (future reserved subnets)."""
        mock_subnet_repository.exists.return_value = False
        mock_subnet_repository.find_by_id.return_value = _make_parent_subnet(
            slug="custom-system-subnet", owner="system"
        )
        service = SubnetService(mock_subnet_repository)

        with pytest.raises(SubnetInvariantError) as exc:
            await service.create_subnet(
                slug="child-1",
                name="Squad",
                owner="alice",
                parent_slug="custom-system-subnet",
            )
        assert exc.value.reason == REASON_PARENT_IS_RESERVED

    @pytest.mark.asyncio
    async def test_parent_is_nested(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """Single-layer cap — the parent must itself be top-level."""
        mock_subnet_repository.exists.return_value = False
        mock_subnet_repository.find_by_id.return_value = _make_parent_subnet(
            slug="mid-level",
            owner="alice",
            parent_slug="grand-parent",
        )
        service = SubnetService(mock_subnet_repository)

        with pytest.raises(SubnetInvariantError) as exc:
            await service.create_subnet(
                slug="grandchild",
                name="Too deep",
                owner="alice",
                parent_slug="mid-level",
            )
        assert exc.value.reason == REASON_PARENT_IS_NESTED

    @pytest.mark.asyncio
    async def test_task_scoped_requires_linked_task(
        self, mock_subnet_repository: ISubnetRepository
    ):
        mock_subnet_repository.exists.return_value = False
        service = SubnetService(mock_subnet_repository)

        with pytest.raises(SubnetInvariantError) as exc:
            await service.create_subnet(
                slug="task-squad",
                name="Task Squad",
                owner="alice",
                lifecycle="task_scoped",
                linked_task_id=None,
            )
        assert exc.value.reason == REASON_TASK_SCOPED_REQUIRES_LINKED_TASK
        # Service rejects before any repository write.
        mock_subnet_repository.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_linked_task_not_found(
        self, mock_subnet_repository: ISubnetRepository
    ):
        mock_subnet_repository.exists.return_value = False
        # We allow parent_slug=None for this case — the
        # linked_task_id check is independent of parenting.
        mock_task_repository = AsyncMock(spec=ITaskRepository)
        mock_task_repository.exists.return_value = False
        service = SubnetService(
            mock_subnet_repository, task_repository=mock_task_repository
        )

        with pytest.raises(SubnetInvariantError) as exc:
            await service.create_subnet(
                slug="task-squad",
                name="Task Squad",
                owner="alice",
                lifecycle="task_scoped",
                linked_task_id="task-missing",
            )
        assert exc.value.reason == REASON_LINKED_TASK_NOT_FOUND
        mock_task_repository.exists.assert_awaited_once_with("task-missing")
        mock_subnet_repository.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_linked_task_check_skipped_when_no_task_repository(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """Backward-compat: legacy fixtures without ``task_repository``
        skip the existence check entirely (still required to pair
        ``task_scoped`` with a non-None ``linked_task_id``, but the
        task itself isn't verified). Production wiring in
        ``api.py`` always supplies one."""
        mock_subnet_repository.exists.return_value = False
        service = SubnetService(mock_subnet_repository)

        # No exception even though no task repository is wired.
        subnet = await service.create_subnet(
            slug="task-squad",
            name="Task Squad",
            owner="alice",
            lifecycle="task_scoped",
            linked_task_id="task-unverified",
        )
        assert subnet.linked_task_id == "task-unverified"
        assert subnet.lifecycle == "task_scoped"
        mock_subnet_repository.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_happy_path_child_subnet(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """Valid nested + task_scoped create — service writes through."""
        mock_subnet_repository.exists.return_value = False
        mock_subnet_repository.find_by_id.return_value = _make_parent_subnet(
            slug="parent-1", owner="alice", members={"alice", "bob"}
        )
        mock_task_repository = AsyncMock(spec=ITaskRepository)
        mock_task_repository.exists.return_value = True
        service = SubnetService(
            mock_subnet_repository, task_repository=mock_task_repository
        )

        subnet = await service.create_subnet(
            slug="squad-1",
            name="Bug Squad",
            owner="alice",
            parent_slug="parent-1",
            lifecycle="task_scoped",
            linked_task_id="task-42",
        )

        assert subnet.parent_slug == "parent-1"
        assert subnet.lifecycle == "task_scoped"
        assert subnet.linked_task_id == "task-42"
        # Owner-as-member invariant preserved for child subnets too.
        assert "alice" in subnet.member_agent_ids
        mock_subnet_repository.save.assert_awaited_once()


# ---------------------------------------------------------------------------
# add_member — membership-subset invariant
# ---------------------------------------------------------------------------


class TestAddMemberMembershipSubset:
    @pytest.mark.asyncio
    async def test_add_to_child_rejects_non_parent_member(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """``add_member`` on a child subnet rejects agents who aren't
        already members of the parent (ADR-0003 §A invariant 2)."""
        child = _make_parent_subnet(
            slug="child-1",
            owner="alice",
            parent_slug="parent-1",
        )
        parent = _make_parent_subnet(
            slug="parent-1",
            owner="alice",
            members={"alice"},  # bob is NOT in the parent
        )
        # find_by_id called twice: once for ``get_subnet(child)``,
        # once for ``find_by_id(parent_slug)`` inside add_member.
        mock_subnet_repository.find_by_id.side_effect = [child, parent]
        service = SubnetService(mock_subnet_repository)

        with pytest.raises(SubnetInvariantError) as exc:
            await service.add_member("child-1", "bob")
        assert exc.value.reason == REASON_NOT_PARENT_MEMBER
        mock_subnet_repository.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_to_child_accepts_parent_member(
        self, mock_subnet_repository: ISubnetRepository
    ):
        child = _make_parent_subnet(
            slug="child-1",
            owner="alice",
            parent_slug="parent-1",
        )
        parent = _make_parent_subnet(
            slug="parent-1",
            owner="alice",
            members={"alice", "bob"},  # bob IS in the parent
        )
        mock_subnet_repository.find_by_id.side_effect = [child, parent]
        service = SubnetService(mock_subnet_repository)

        updated = await service.add_member("child-1", "bob")

        assert "bob" in updated.member_agent_ids
        mock_subnet_repository.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_add_to_orphan_child_rejects(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """Parent has been deleted out from under us — refuse the
        add rather than silently bypass the subset invariant. Ops
        should ``delete_subnet`` the orphan."""
        child = _make_parent_subnet(
            slug="child-1",
            owner="alice",
            parent_slug="parent-gone",
        )
        mock_subnet_repository.find_by_id.side_effect = [child, None]
        service = SubnetService(mock_subnet_repository)

        with pytest.raises(SubnetInvariantError) as exc:
            await service.add_member("child-1", "bob")
        assert exc.value.reason == REASON_NOT_PARENT_MEMBER

    @pytest.mark.asyncio
    async def test_add_to_top_level_subnet_unchanged(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """Top-level subnets (no parent) skip the subset check entirely."""
        top = _make_parent_subnet(slug="top", owner="alice")
        mock_subnet_repository.find_by_id.return_value = top
        service = SubnetService(mock_subnet_repository)

        updated = await service.add_member("top", "newbie")

        assert "newbie" in updated.member_agent_ids
        mock_subnet_repository.save.assert_awaited_once()


# ---------------------------------------------------------------------------
# list_children — ACL parity with list_subnets
# ---------------------------------------------------------------------------


class TestListChildrenACL:
    @pytest.mark.asyncio
    async def test_returns_all_children_regardless_of_requester(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """Service layer returns ALL children; ACL rendering is now
        the route layer's responsibility (per-row SubnetStub for private
        unauthorised — same pattern as list_subnets, P2-1 fix).
        """
        children = [
            Subnet(
                slug="public-child",
                name="Public",
                owner="alice",
                is_private=False,
                parent_slug="parent-1",
                member_agent_ids={"alice"},
            ),
            Subnet(
                slug="private-child",
                name="Private",
                owner="alice",
                is_private=True,
                # ADR-0004: ``is_private=True`` requires
                # ``join_policy='approval'``; the entity invariant
                # rejects the legacy ``private + open`` combination.
                join_policy="approval",
                parent_slug="parent-1",
                member_agent_ids={"alice"},
            ),
        ]
        mock_subnet_repository.find_by_parent.return_value = children
        service = SubnetService(mock_subnet_repository)

        result = await service.list_children("parent-1", requester_id=None)
        ids = {c.slug for c in result}
        # Service now returns all children; route applies per-row V6 rendering.
        assert ids == {"public-child", "private-child"}

    @pytest.mark.asyncio
    async def test_returns_all_children_for_member(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """Service layer returns all children; access control is done
        at the route layer (per-row SubnetStub vs SubnetInfo)."""
        children = [
            Subnet(
                slug="private-mine",
                name="Mine",
                owner="alice",
                is_private=True,
                # ADR-0004 invariant: see "private-child" above.
                join_policy="approval",
                parent_slug="parent-1",
                member_agent_ids={"alice", "bob"},
            ),
            Subnet(
                slug="private-theirs",
                name="Theirs",
                owner="carol",
                is_private=True,
                join_policy="approval",
                parent_slug="parent-1",
                member_agent_ids={"carol"},
            ),
        ]
        mock_subnet_repository.find_by_parent.return_value = children
        service = SubnetService(mock_subnet_repository)

        result = await service.list_children("parent-1", requester_id="bob")
        ids = {c.slug for c in result}
        # Service returns all; route renders private-theirs as Stub for bob.
        assert ids == {"private-mine", "private-theirs"}

    @pytest.mark.asyncio
    async def test_no_existence_leak_for_unknown_parent(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """Unknown parent returns empty — same shape as legitimate
        no-children result."""
        mock_subnet_repository.find_by_parent.return_value = []
        service = SubnetService(mock_subnet_repository)

        result = await service.list_children("never-existed", requester_id="bob")
        assert result == []


# ---------------------------------------------------------------------------
# promote_to_persistent — owner-only + idempotent
# ---------------------------------------------------------------------------


class TestPromoteToPersistent:
    @pytest.mark.asyncio
    async def test_promote_flips_lifecycle_and_clears_linked_task(
        self, mock_subnet_repository: ISubnetRepository
    ):
        subnet = Subnet(
            slug="squad-1",
            name="Squad",
            owner="alice",
            parent_slug="parent-1",
            lifecycle="task_scoped",
            linked_task_id="task-42",
        )
        mock_subnet_repository.find_by_id.return_value = subnet
        service = SubnetService(mock_subnet_repository)

        updated = await service.promote_to_persistent("squad-1", owner="alice")

        assert updated.lifecycle == "persistent"
        assert updated.linked_task_id is None
        mock_subnet_repository.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_promote_already_persistent_is_idempotent(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """Idempotent — no repository write, no error."""
        subnet = Subnet(
            slug="persistent-1",
            name="Persistent",
            owner="alice",
            lifecycle="persistent",
        )
        mock_subnet_repository.find_by_id.return_value = subnet
        service = SubnetService(mock_subnet_repository)

        result = await service.promote_to_persistent(
            "persistent-1", owner="alice"
        )
        assert result.lifecycle == "persistent"
        mock_subnet_repository.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_promote_owner_mismatch_raises_permission_error(
        self, mock_subnet_repository: ISubnetRepository
    ):
        subnet = Subnet(
            slug="squad-1",
            name="Squad",
            owner="alice",
            parent_slug="parent-1",
            lifecycle="task_scoped",
            linked_task_id="task-42",
        )
        mock_subnet_repository.find_by_id.return_value = subnet
        service = SubnetService(mock_subnet_repository)

        with pytest.raises(PermissionError):
            await service.promote_to_persistent("squad-1", owner="mallory")
        mock_subnet_repository.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_promote_does_not_check_owner_parent_membership(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """ADR-0003 semantic decision #4: promote is a pure field
        flip authorised by owner-only ACL. The owner is NOT
        required to currently be a member of the parent subnet.

        Regression target: a previous draft attempted to enforce a
        ``parent.is_member(owner)`` check here, which broke
        delegated-admin flows (squad created by a moderator who is
        not themselves a parent member).
        """
        # Repository ``find_by_id`` is only called once — for the
        # subnet being promoted. The parent is never fetched.
        subnet = Subnet(
            slug="squad-1",
            name="Squad",
            owner="alice",
            parent_slug="parent-1",
            lifecycle="task_scoped",
            linked_task_id="task-42",
        )
        mock_subnet_repository.find_by_id.return_value = subnet
        service = SubnetService(mock_subnet_repository)

        result = await service.promote_to_persistent("squad-1", owner="alice")

        assert result.lifecycle == "persistent"
        # Critical: only ONE find_by_id call (for the subnet) —
        # confirms no parent lookup happened.
        assert mock_subnet_repository.find_by_id.await_count == 1
