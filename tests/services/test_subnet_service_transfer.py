"""Service-layer unit tests for ``SubnetService.transfer_owner``.

White-box coverage that complements the route-layer smoke tests in
``tests/routes/test_subnets_transfer.py``.  These tests drive the
service directly (no HTTP stack) so they can assert internal details
such as the shallow-copy protection, the saved entity shape, and the
conditional agent-existence check.

Test matrix
-----------
- Happy path: owner field updated, new_owner added to member set,
  repository.save called with the transferred entity.
- Shallow-copy safety: original ``member_agent_ids`` set is NOT mutated
  after the transfer.
- Previous owner retains membership after transfer.
- ``PermissionError`` on non-owner caller.
- ``PermissionError`` on reserved system subnet (``public`` / ``system``).
- ``ValueError`` on self-transfer.
- ``ValueError`` on ``backend@internal`` new_owner (ADR-0002).
- ``ValueError`` on ``system`` new_owner (reserved platform identity).
- ``ValueError`` on unregistered new_owner when agent_repository is wired.
- Agent-existence check is skipped when agent_repository is ``None``.
- ``SubnetNotFoundException`` propagates when subnet does not exist.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.entities.subnet import Subnet  # noqa: E402
from acn.core.exceptions import SubnetNotFoundException
from acn.services import SubnetService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_subnet(
    slug: str = "subnet-abc",
    owner: str = "alice",
    members: set[str] | None = None,
) -> Subnet:
    """Return a minimal ``Subnet`` entity with the given owner/members."""
    return Subnet(
        slug=slug,
        name="Test",
        owner=owner,
        member_agent_ids=members if members is not None else {owner},
    )


def _make_repo(subnet: Subnet | MagicMock | None) -> AsyncMock:
    """Subnet repository that returns *subnet* on ``find_by_id``."""
    repo = AsyncMock()
    repo.find_by_id = AsyncMock(return_value=subnet)
    repo.save = AsyncMock()
    return repo


def _make_agent_repo(known_ids: set[str]) -> AsyncMock:
    """Agent repository that returns a mock agent for IDs in *known_ids*."""
    repo = AsyncMock()

    async def _find(agent_id: str):
        if agent_id in known_ids:
            m = MagicMock()
            m.agent_id = agent_id
            return m
        return None

    repo.find_by_id = AsyncMock(side_effect=_find)
    return repo


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestTransferOwnerSuccess:
    @pytest.mark.asyncio
    async def test_owner_field_is_updated(self):
        subnet = _make_subnet(owner="alice")
        svc = SubnetService(_make_repo(subnet))

        result = await svc.transfer_owner("subnet-abc", "alice", "bob")

        assert result.owner == "bob"

    @pytest.mark.asyncio
    async def test_new_owner_is_added_to_member_set(self):
        subnet = _make_subnet(owner="alice")
        svc = SubnetService(_make_repo(subnet))

        result = await svc.transfer_owner("subnet-abc", "alice", "bob")

        assert "bob" in result.member_agent_ids

    @pytest.mark.asyncio
    async def test_previous_owner_retains_membership(self):
        """alice's membership is not revoked by the transfer."""
        subnet = _make_subnet(owner="alice")
        svc = SubnetService(_make_repo(subnet))

        result = await svc.transfer_owner("subnet-abc", "alice", "bob")

        assert "alice" in result.member_agent_ids

    @pytest.mark.asyncio
    async def test_repository_save_called_with_transferred_entity(self):
        subnet = _make_subnet(owner="alice")
        repo = _make_repo(subnet)
        svc = SubnetService(repo)

        result = await svc.transfer_owner("subnet-abc", "alice", "bob")

        repo.save.assert_awaited_once()
        saved = repo.save.call_args.args[0]
        assert saved.owner == "bob"
        assert saved is result  # same object returned and persisted

    @pytest.mark.asyncio
    async def test_idempotent_when_new_owner_already_a_member(self):
        """If bob is already a member, the transfer still succeeds."""
        subnet = _make_subnet(owner="alice", members={"alice", "bob"})
        svc = SubnetService(_make_repo(subnet))

        result = await svc.transfer_owner("subnet-abc", "alice", "bob")

        assert result.owner == "bob"
        assert "bob" in result.member_agent_ids

    @pytest.mark.asyncio
    async def test_agent_repo_wired_accepts_registered_new_owner(self):
        subnet = _make_subnet(owner="alice")
        svc = SubnetService(
            _make_repo(subnet),
            agent_repository=_make_agent_repo({"bob"}),
        )

        result = await svc.transfer_owner("subnet-abc", "alice", "bob")

        assert result.owner == "bob"


# ---------------------------------------------------------------------------
# Shallow-copy safety
# ---------------------------------------------------------------------------


class TestTransferOwnerShallowCopySafety:
    @pytest.mark.asyncio
    async def test_original_member_set_is_not_mutated(self):
        """``dataclasses.replace`` is shallow — verify we make an explicit copy
        so the original entity's ``member_agent_ids`` is unaffected."""
        subnet = _make_subnet(owner="alice")
        original_members = set(subnet.member_agent_ids)  # snapshot before call
        repo = _make_repo(subnet)
        svc = SubnetService(repo)

        await svc.transfer_owner("subnet-abc", "alice", "bob")

        # The object that was passed to the repo and then returned shares no
        # identity with the original set; the original must be unchanged.
        assert subnet.member_agent_ids == original_members


# ---------------------------------------------------------------------------
# PermissionError paths
# ---------------------------------------------------------------------------


class TestTransferOwnerPermission:
    @pytest.mark.asyncio
    async def test_non_owner_raises_permission_error(self):
        subnet = _make_subnet(owner="alice")
        svc = SubnetService(_make_repo(subnet))

        with pytest.raises(PermissionError, match="mismatch"):
            await svc.transfer_owner("subnet-abc", "charlie", "bob")

    @pytest.mark.asyncio
    async def test_reserved_subnet_public_raises_permission_error(self):
        # The ``Subnet`` entity rejects reserved IDs at construction time, so
        # real ``public`` / ``system`` subnets are stored via internal bootstrap
        # paths that bypass entity validation.  We use a MagicMock to simulate
        # the repository returning such an object.
        stub = MagicMock()
        stub.slug = "public"
        stub.owner = "alice"
        svc = SubnetService(_make_repo(stub))

        with pytest.raises(PermissionError, match="Cannot transfer system subnet"):
            await svc.transfer_owner("public", "alice", "bob")

    @pytest.mark.asyncio
    async def test_reserved_subnet_system_raises_permission_error(self):
        stub = MagicMock()
        stub.slug = "system"
        stub.owner = "alice"
        svc = SubnetService(_make_repo(stub))

        with pytest.raises(PermissionError, match="Cannot transfer system subnet"):
            await svc.transfer_owner("system", "alice", "bob")

    @pytest.mark.asyncio
    async def test_non_owner_check_fires_before_reserved_subnet_check(self):
        """Owner mismatch takes priority — a non-owner cannot even learn the
        subnet is reserved or non-reserved via the error type."""
        stub = MagicMock()
        stub.slug = "public"
        stub.owner = "alice"
        svc = SubnetService(_make_repo(stub))

        with pytest.raises(PermissionError, match="mismatch"):
            await svc.transfer_owner("public", "charlie", "bob")


# ---------------------------------------------------------------------------
# ValueError paths
# ---------------------------------------------------------------------------


class TestTransferOwnerValidation:
    @pytest.mark.asyncio
    async def test_self_transfer_raises_value_error(self):
        subnet = _make_subnet(owner="alice")
        svc = SubnetService(_make_repo(subnet))

        with pytest.raises(ValueError, match="new_owner must differ"):
            await svc.transfer_owner("subnet-abc", "alice", "alice")

    @pytest.mark.asyncio
    async def test_backend_internal_new_owner_raises_value_error(self):
        """ADR-0002 guard fires unconditionally — no agent_repo needed."""
        subnet = _make_subnet(owner="alice")
        svc = SubnetService(_make_repo(subnet))  # no agent_repository

        with pytest.raises(ValueError, match="ADR-0002"):
            await svc.transfer_owner("subnet-abc", "alice", "backend@internal")

    @pytest.mark.asyncio
    async def test_system_identity_new_owner_raises_value_error(self):
        """``"system"`` guard fires unconditionally — no agent_repo needed."""
        subnet = _make_subnet(owner="alice")
        svc = SubnetService(_make_repo(subnet))  # no agent_repository

        with pytest.raises(ValueError, match="reserved platform identity"):
            await svc.transfer_owner("subnet-abc", "alice", "system")

    @pytest.mark.asyncio
    async def test_unregistered_new_owner_raises_value_error(self):
        """When agent_repository is wired, unknown agents are rejected."""
        subnet = _make_subnet(owner="alice")
        svc = SubnetService(
            _make_repo(subnet),
            agent_repository=_make_agent_repo(set()),  # no agents known
        )

        with pytest.raises(ValueError, match="not registered"):
            await svc.transfer_owner("subnet-abc", "alice", "ghost")

    @pytest.mark.asyncio
    async def test_agent_existence_check_skipped_without_repo(self):
        """When agent_repository is ``None``, unverified owners are allowed
        through (legacy fixtures / Redis-only deployments)."""
        subnet = _make_subnet(owner="alice")
        svc = SubnetService(_make_repo(subnet))  # agent_repository=None (default)

        # Should NOT raise — the existence check is skipped.
        result = await svc.transfer_owner("subnet-abc", "alice", "unverified-bob")

        assert result.owner == "unverified-bob"


# ---------------------------------------------------------------------------
# Not-found path
# ---------------------------------------------------------------------------


class TestTransferOwnerNotFound:
    @pytest.mark.asyncio
    async def test_missing_subnet_raises_subnet_not_found(self):
        repo = _make_repo(None)  # find_by_id returns None → not found
        svc = SubnetService(repo)

        with pytest.raises(SubnetNotFoundException):
            await svc.transfer_owner("no-such-subnet", "alice", "bob")
