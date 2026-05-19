"""SubnetService.delete_subnet — ADR-0004 cascade extension tests.

Slice 2.1 adds the join_request + allowlist cascade hooks to
``delete_subnet``. These tests pin the contract:

1. **Both new cascades fire BEFORE the subnet HASH delete**.
   Critical ordering — without it, a Redis partial failure on the
   join_request sweep would leave dust against a no-longer-existing
   subnet HASH (the very scenario ADR §"Cascade deletion" calls
   out).

2. **Top-level cascade sweeps every child + the parent**. Each
   child has its own join_requests / allowlist namespace; the
   service must call ``delete_for_subnet`` for every one.

3. **RuntimeError from a cascade aborts the subnet delete**.
   Mirrors the Redis cascade-partial-failure contract from
   ADR-0003; without this propagation, callers would treat a
   half-cascade as success.

4. **Silent no-op when the new repos aren't wired**. Legacy
   ``SubnetService(subnet_repository)`` instantiation (no kwargs
   for the two new repos) MUST behave exactly as it did pre-Slice
   2.1 — opt-in pattern matches ``agent_repository`` / issue #56.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from acn.core.entities import Subnet
from acn.core.interfaces import (
    ISubnetAllowlistRepository,
    ISubnetJoinRequestRepository,
    ISubnetRepository,
)
from acn.services.subnet_service import SubnetService


def _subnet(
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


@pytest.fixture
def mock_subnet_repo() -> AsyncMock:
    """Default mock: subnet has zero children (so cascade path
    doesn't fire unless a test overrides ``find_by_parent``)."""
    repo = AsyncMock(spec=ISubnetRepository)
    repo.find_by_parent.return_value = []
    return repo


@pytest.fixture
def mock_jr_repo() -> AsyncMock:
    """``delete_for_subnet`` returns 0 by default — most tests
    care about call shape, not row count."""
    repo = AsyncMock(spec=ISubnetJoinRequestRepository)
    repo.delete_for_subnet.return_value = 0
    return repo


@pytest.fixture
def mock_al_repo() -> AsyncMock:
    repo = AsyncMock(spec=ISubnetAllowlistRepository)
    repo.delete_for_subnet.return_value = 0
    return repo


# ---------------------------------------------------------------------------
# Single-subnet delete path (no children)
# ---------------------------------------------------------------------------


class TestSingleSubnetCascade:
    @pytest.mark.asyncio
    async def test_cascades_fire_before_subnet_repo_delete(
        self,
        mock_subnet_repo: AsyncMock,
        mock_jr_repo: AsyncMock,
        mock_al_repo: AsyncMock,
    ):
        """THE ordering invariant. If subnet HASH is deleted first,
        the two new cascades leave dust against a no-longer-existing
        subnet. The PG path tolerates this (FK-less manual cascade
        still removes the rows correctly); the Redis path doesn't
        — ``delete_for_subnet`` iterates the subnet's listing SET,
        which is part of the subnet's namespace and may not be
        readable after the subnet HASH is gone."""
        sn = _subnet("s-1")
        mock_subnet_repo.find_by_id.return_value = sn
        mock_subnet_repo.delete.return_value = True

        ordered_calls: list[str] = []
        # ``**_kw`` swallows the ``session=`` kwarg the service now
        # threads through every cascade method (added in Slice 2.1.1
        # for the optional :class:`IUnitOfWork` atomic path). These
        # side-effect lambdas pin cascade *ordering*, not the session
        # value — keep them session-agnostic per issue #75 acceptance.
        mock_jr_repo.delete_for_subnet.side_effect = (
            lambda sid, **_kw: ordered_calls.append(f"jr:{sid}") or 0
        )
        mock_al_repo.delete_for_subnet.side_effect = (
            lambda sid, **_kw: ordered_calls.append(f"al:{sid}") or 0
        )

        async def record_subnet_delete(sid, **_kw):
            ordered_calls.append(f"subnet_delete:{sid}")
            return True

        mock_subnet_repo.delete = AsyncMock(side_effect=record_subnet_delete)

        service = SubnetService(
            mock_subnet_repo,
            subnet_join_request_repository=mock_jr_repo,
            subnet_allowlist_repository=mock_al_repo,
        )
        ok = await service.delete_subnet("s-1", owner="alice")
        assert ok is True

        jr_idx = ordered_calls.index("jr:s-1")
        al_idx = ordered_calls.index("al:s-1")
        delete_idx = ordered_calls.index("subnet_delete:s-1")
        assert jr_idx < delete_idx, "join_request cascade must precede subnet delete"
        assert al_idx < delete_idx, "allowlist cascade must precede subnet delete"

    @pytest.mark.asyncio
    async def test_silent_no_op_when_new_repos_omitted(
        self, mock_subnet_repo: AsyncMock
    ):
        """Legacy ``SubnetService(subnet_repository)`` callers MUST
        get the pre-Slice-2.1 behaviour unchanged — opt-in pattern
        matches the existing ``agent_repository`` discipline."""
        sn = _subnet("s-1")
        mock_subnet_repo.find_by_id.return_value = sn
        mock_subnet_repo.delete.return_value = True

        service = SubnetService(mock_subnet_repo)
        ok = await service.delete_subnet("s-1", owner="alice")
        assert ok is True  # No raise, no fabricated cascade.

    @pytest.mark.asyncio
    async def test_cascade_logs_event_name_when_rows_deleted(
        self,
        mock_subnet_repo: AsyncMock,
        mock_jr_repo: AsyncMock,
        mock_al_repo: AsyncMock,
    ):
        """``deleted_count > 0`` triggers an info-level audit event
        with the canonical event name; zero is silent (don't pollute
        logs with no-op cascades). We patch ``logger.info`` directly
        rather than using ``caplog`` because structlog routes
        through a different formatter chain than stdlib in this
        codebase — ``caplog`` was producing inconsistent capture
        across local vs CI envs."""
        import unittest.mock as _mock

        from acn.services import subnet_service as _svc_mod

        sn = _subnet("s-with-rows")
        mock_subnet_repo.find_by_id.return_value = sn
        mock_subnet_repo.delete.return_value = True
        mock_jr_repo.delete_for_subnet.return_value = 7
        mock_al_repo.delete_for_subnet.return_value = 3

        service = SubnetService(
            mock_subnet_repo,
            subnet_join_request_repository=mock_jr_repo,
            subnet_allowlist_repository=mock_al_repo,
        )
        with _mock.patch.object(_svc_mod.logger, "info") as log_info:
            await service.delete_subnet("s-with-rows", owner="alice")

        event_names = [c.args[0] for c in log_info.call_args_list if c.args]
        assert "delete_subnet_cascade_join_requests" in event_names
        assert "delete_subnet_cascade_allowlist" in event_names
        # Per-event payloads carry the deleted count.
        jr_call = next(
            c
            for c in log_info.call_args_list
            if c.args
            and c.args[0] == "delete_subnet_cascade_join_requests"
        )
        assert jr_call.kwargs.get("deleted_count") == 7
        assert jr_call.kwargs.get("subnet_id") == "s-with-rows"

    @pytest.mark.asyncio
    async def test_cascade_silent_when_zero_rows_deleted(
        self,
        mock_subnet_repo: AsyncMock,
        mock_jr_repo: AsyncMock,
        mock_al_repo: AsyncMock,
    ):
        """Zero-row cascade emits no log noise — the gating that the
        previous test pins, viewed from the negative case so a
        future refactor that always-logs can't slip through either
        direction."""
        import unittest.mock as _mock

        from acn.services import subnet_service as _svc_mod

        sn = _subnet("s-empty")
        mock_subnet_repo.find_by_id.return_value = sn
        mock_subnet_repo.delete.return_value = True
        # Default fixture already returns 0 from delete_for_subnet.

        service = SubnetService(
            mock_subnet_repo,
            subnet_join_request_repository=mock_jr_repo,
            subnet_allowlist_repository=mock_al_repo,
        )
        with _mock.patch.object(_svc_mod.logger, "info") as log_info:
            await service.delete_subnet("s-empty", owner="alice")

        event_names = [c.args[0] for c in log_info.call_args_list if c.args]
        assert "delete_subnet_cascade_join_requests" not in event_names
        assert "delete_subnet_cascade_allowlist" not in event_names


# ---------------------------------------------------------------------------
# Top-level cascade path (parent + children)
# ---------------------------------------------------------------------------


class TestTopLevelCascadeWithChildren:
    @pytest.mark.asyncio
    async def test_sweeps_every_child_and_parent(
        self,
        mock_subnet_repo: AsyncMock,
        mock_jr_repo: AsyncMock,
        mock_al_repo: AsyncMock,
    ):
        """Each child has its own join_requests + allowlist
        namespace. Cascade must call ``delete_for_subnet`` once per
        child AND once for the parent — missing a single one leaves
        per-subnet dust that ops can't clean up without scanning
        every subnet HASH for orphan secondary keys."""
        parent = _subnet("parent")
        children = [
            _subnet("child-1", parent_subnet_id="parent"),
            _subnet("child-2", parent_subnet_id="parent"),
        ]
        mock_subnet_repo.find_by_id.return_value = parent
        mock_subnet_repo.find_by_parent.return_value = children
        mock_subnet_repo.delete_with_children.return_value = True

        service = SubnetService(
            mock_subnet_repo,
            subnet_join_request_repository=mock_jr_repo,
            subnet_allowlist_repository=mock_al_repo,
        )
        await service.delete_subnet("parent", owner="alice")

        jr_subnet_ids = [
            c.args[0] for c in mock_jr_repo.delete_for_subnet.await_args_list
        ]
        al_subnet_ids = [
            c.args[0] for c in mock_al_repo.delete_for_subnet.await_args_list
        ]
        assert set(jr_subnet_ids) == {"parent", "child-1", "child-2"}
        assert set(al_subnet_ids) == {"parent", "child-1", "child-2"}


# ---------------------------------------------------------------------------
# Partial-failure propagation
# ---------------------------------------------------------------------------


class TestPartialFailurePropagation:
    @pytest.mark.asyncio
    async def test_join_request_cascade_runtime_error_aborts_subnet_delete(
        self,
        mock_subnet_repo: AsyncMock,
        mock_jr_repo: AsyncMock,
        mock_al_repo: AsyncMock,
    ):
        """Redis cascade partial failure → ``RuntimeError`` from
        ``delete_for_subnet``. The service MUST NOT swallow it —
        ADR §"Cascade deletion: Redis" requires the failure to
        abort the subnet HASH delete so a half-cascade isn't
        treated as success."""
        sn = _subnet("s-x")
        mock_subnet_repo.find_by_id.return_value = sn
        mock_jr_repo.delete_for_subnet.side_effect = RuntimeError(
            "redis cascade partial"
        )

        service = SubnetService(
            mock_subnet_repo,
            subnet_join_request_repository=mock_jr_repo,
            subnet_allowlist_repository=mock_al_repo,
        )
        with pytest.raises(RuntimeError, match="redis cascade partial"):
            await service.delete_subnet("s-x", owner="alice")

        # Subnet HASH delete must NOT have been called.
        mock_subnet_repo.delete.assert_not_called()
        # Allowlist cascade is fine to either run or skip; ordering
        # (jr → al) currently means al doesn't run after jr raises.
        # We don't pin "must not run" because a future swap to a
        # gather-style parallel cascade might legitimately call both
        # — only the SUBNET HASH delete suppression is contractual.

    @pytest.mark.asyncio
    async def test_allowlist_cascade_runtime_error_aborts_subnet_delete(
        self,
        mock_subnet_repo: AsyncMock,
        mock_jr_repo: AsyncMock,
        mock_al_repo: AsyncMock,
    ):
        sn = _subnet("s-x")
        mock_subnet_repo.find_by_id.return_value = sn
        mock_al_repo.delete_for_subnet.side_effect = RuntimeError(
            "redis allowlist cascade partial"
        )

        service = SubnetService(
            mock_subnet_repo,
            subnet_join_request_repository=mock_jr_repo,
            subnet_allowlist_repository=mock_al_repo,
        )
        with pytest.raises(RuntimeError, match="allowlist cascade partial"):
            await service.delete_subnet("s-x", owner="alice")
        mock_subnet_repo.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Partial wiring — one repo wired, other absent
# ---------------------------------------------------------------------------


class TestPartialWiring:
    @pytest.mark.asyncio
    async def test_only_join_request_repo_wired(
        self,
        mock_subnet_repo: AsyncMock,
        mock_jr_repo: AsyncMock,
    ):
        """Defensive — the two repos are independent opt-ins.
        Wiring only one shouldn't raise; the other cascade just
        doesn't run."""
        sn = _subnet("s-1")
        mock_subnet_repo.find_by_id.return_value = sn
        mock_subnet_repo.delete.return_value = True

        service = SubnetService(
            mock_subnet_repo,
            subnet_join_request_repository=mock_jr_repo,
            # allowlist repo intentionally omitted
        )
        await service.delete_subnet("s-1", owner="alice")
        # Session-agnostic assertion per issue #75 acceptance signal
        # ("the contracts pinned there are session-agnostic") — pin
        # "called once with the subnet id", but allow either ``session``
        # kwarg shape (Slice 2.1.1 added ``session=None`` to the legacy
        # path's call shape, but the contract intent here is just
        # "this repo's cascade method ran for this subnet exactly once").
        mock_jr_repo.delete_for_subnet.assert_awaited_once()
        assert mock_jr_repo.delete_for_subnet.await_args.args == ("s-1",)

    @pytest.mark.asyncio
    async def test_only_allowlist_repo_wired(
        self,
        mock_subnet_repo: AsyncMock,
        mock_al_repo: AsyncMock,
    ):
        sn = _subnet("s-1")
        mock_subnet_repo.find_by_id.return_value = sn
        mock_subnet_repo.delete.return_value = True

        service = SubnetService(
            mock_subnet_repo,
            subnet_allowlist_repository=mock_al_repo,
            # join_request repo intentionally omitted
        )
        await service.delete_subnet("s-1", owner="alice")
        # Session-agnostic — see sibling
        # ``test_only_join_request_repo_wired`` for the rationale.
        mock_al_repo.delete_for_subnet.assert_awaited_once()
        assert mock_al_repo.delete_for_subnet.await_args.args == ("s-1",)
