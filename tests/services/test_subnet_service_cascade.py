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
    async def test_top_level_delegates_to_delete_with_children(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """Top-level subnet with children → service calls
        ``delete_with_children`` ONCE (instead of looping ``delete``)
        so the repository can keep parent + children inside a single
        transaction. The atomicity guarantee lives in the repo per
        ADR-0003 §A.4 / issue #54.
        """
        parent = _make_subnet("parent")
        children = [
            _make_subnet("child-1", parent_subnet_id="parent"),
            _make_subnet("child-2", parent_subnet_id="parent"),
        ]
        mock_subnet_repository.find_by_id.return_value = parent
        mock_subnet_repository.find_by_parent.return_value = children
        mock_subnet_repository.delete_with_children.return_value = True
        service = SubnetService(mock_subnet_repository)

        ok = await service.delete_subnet("parent", owner="alice")

        assert ok is True
        # ONE batched cascade call — parent + children in the same
        # repository invocation.
        mock_subnet_repository.delete_with_children.assert_awaited_once_with(
            "parent", ["child-1", "child-2"]
        )
        # Per-id ``delete()`` is no longer used on the cascade path —
        # this catches accidental regressions back to the looped impl.
        mock_subnet_repository.delete.assert_not_called()

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
        mock_subnet_repository.delete_with_children.assert_not_called()
        # Single-row delete path used directly.
        mock_subnet_repository.delete.assert_awaited_once_with("child-1")

    @pytest.mark.asyncio
    async def test_cascade_failure_propagates_from_repository(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """When the repository's ``delete_with_children`` raises
        (Redis breadcrumb path or PG transaction abort), the service
        does NOT swallow it — caller sees the original exception and
        the parent subnet remains addressable (best-effort guarantee
        from the repo)."""
        parent = _make_subnet("parent")
        children = [_make_subnet("child-1", parent_subnet_id="parent")]
        mock_subnet_repository.find_by_id.return_value = parent
        mock_subnet_repository.find_by_parent.return_value = children
        mock_subnet_repository.delete_with_children.side_effect = RuntimeError(
            "Cascade delete failed for child child-1; "
            "refusing to delete parent parent"
        )
        service = SubnetService(mock_subnet_repository)

        with pytest.raises(RuntimeError, match="Cascade delete failed"):
            await service.delete_subnet("parent", owner="alice")

        # Service must not have fallen back to a per-row delete after
        # the cascade raised — that would re-open the orphan window.
        mock_subnet_repository.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_top_level_with_no_children_uses_simple_delete(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """Top-level subnet with zero children skips the cascade
        seam entirely — a single ``delete(parent_id)`` is cheaper than
        an empty transactional batch and matches the legacy behaviour
        for non-nested subnets."""
        parent = _make_subnet("lonely-parent")
        mock_subnet_repository.find_by_id.return_value = parent
        mock_subnet_repository.find_by_parent.return_value = []
        mock_subnet_repository.delete.return_value = True
        service = SubnetService(mock_subnet_repository)

        ok = await service.delete_subnet("lonely-parent", owner="alice")
        assert ok is True

        mock_subnet_repository.delete.assert_awaited_once_with("lonely-parent")
        mock_subnet_repository.delete_with_children.assert_not_called()

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
