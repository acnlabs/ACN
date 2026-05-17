"""SubnetService cascade-delete unit tests (ADR-0003 Phase 2).

Pin ``delete_subnet`` of a top-level subnet cascading to children
(``find_by_parent`` → delete each child → delete self) and the
partial-failure breadcrumb path that refuses to delete the parent
when a child delete fails.
"""

from datetime import UTC, datetime

import pytest

from acn.core.entities import Subnet
from acn.core.interfaces import ISubnetRepository
from acn.services.subnet_service import SubnetService


def _make_subnet(
    subnet_id: str,
    owner: str = "alice",
    parent_subnet_id: str | None = None,
) -> Subnet:
    return Subnet(
        subnet_id=subnet_id,
        name=subnet_id,
        owner=owner,
        parent_subnet_id=parent_subnet_id,
        member_agent_ids={owner},
        created_at=datetime.now(UTC),
    )


class TestDeleteSubnetCascade:
    @pytest.mark.asyncio
    async def test_top_level_deletes_children_first_then_self(
        self, mock_subnet_repository: ISubnetRepository
    ):
        parent = _make_subnet("parent")
        children = [
            _make_subnet("child-1", parent_subnet_id="parent"),
            _make_subnet("child-2", parent_subnet_id="parent"),
        ]
        mock_subnet_repository.find_by_id.return_value = parent
        mock_subnet_repository.find_by_parent.return_value = children
        mock_subnet_repository.delete.return_value = True
        service = SubnetService(mock_subnet_repository)

        ok = await service.delete_subnet("parent", owner="alice")

        assert ok is True
        # Three delete() calls in order: child-1, child-2, parent.
        delete_call_args = [
            c.args[0] for c in mock_subnet_repository.delete.call_args_list
        ]
        assert delete_call_args == ["child-1", "child-2", "parent"]

    @pytest.mark.asyncio
    async def test_child_delete_no_recursive_cascade(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """When deleting a child subnet directly, the service does
        NOT scan for grandchildren (single-layer cap means a child
        cannot itself have children) — saves a redundant
        ``find_by_parent`` round-trip.
        """
        child = _make_subnet("child-1", parent_subnet_id="parent")
        mock_subnet_repository.find_by_id.return_value = child
        mock_subnet_repository.delete.return_value = True
        service = SubnetService(mock_subnet_repository)

        await service.delete_subnet("child-1", owner="alice")

        # Cascade only fires when ``parent_subnet_id is None``.
        mock_subnet_repository.find_by_parent.assert_not_called()

    @pytest.mark.asyncio
    async def test_child_delete_failure_refuses_to_delete_parent(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """When repo.delete(child) returns False (e.g. concurrent
        deletion), the cascade aborts BEFORE the parent delete —
        ops will see the breadcrumb and clean up manually."""
        parent = _make_subnet("parent")
        children = [_make_subnet("child-1", parent_subnet_id="parent")]
        mock_subnet_repository.find_by_id.return_value = parent
        mock_subnet_repository.find_by_parent.return_value = children
        # child delete fails, parent never reached.
        mock_subnet_repository.delete.return_value = False
        service = SubnetService(mock_subnet_repository)

        with pytest.raises(RuntimeError, match="Cascade delete failed"):
            await service.delete_subnet("parent", owner="alice")

        # Only the failing child delete was attempted — the parent
        # was NOT removed (preserves a recoverable state).
        delete_call_args = [
            c.args[0] for c in mock_subnet_repository.delete.call_args_list
        ]
        assert delete_call_args == ["child-1"]

    @pytest.mark.asyncio
    async def test_top_level_with_no_children_deletes_self(
        self, mock_subnet_repository: ISubnetRepository
    ):
        parent = _make_subnet("lonely-parent")
        mock_subnet_repository.find_by_id.return_value = parent
        mock_subnet_repository.find_by_parent.return_value = []
        mock_subnet_repository.delete.return_value = True
        service = SubnetService(mock_subnet_repository)

        ok = await service.delete_subnet("lonely-parent", owner="alice")
        assert ok is True

        delete_call_args = [
            c.args[0] for c in mock_subnet_repository.delete.call_args_list
        ]
        assert delete_call_args == ["lonely-parent"]

    @pytest.mark.asyncio
    async def test_reserved_subnet_cannot_be_deleted(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """Cascade entry-point still blocks delete of reserved IDs."""
        # Reserved subnets must have owner="system" (entity
        # invariant), so we build it explicitly here.
        public_subnet = Subnet(
            subnet_id="public",
            name="Public",
            owner="system",
        )
        mock_subnet_repository.find_by_id.return_value = public_subnet
        service = SubnetService(mock_subnet_repository)

        with pytest.raises(PermissionError, match="Cannot delete system subnet"):
            await service.delete_subnet("public", owner="system")
        mock_subnet_repository.delete.assert_not_called()
