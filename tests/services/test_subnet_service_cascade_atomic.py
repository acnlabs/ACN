"""SubnetService.delete_subnet cascade atomicity (issue #75 / Slice 2.1.1).

What this pins
--------------
ADR-0004 §"Cascade deletion: Postgres" promises:

> ``delete_subnet`` runs a single ``session.begin()`` transaction
> containing ``DELETE FROM subnet_join_requests WHERE subnet_id=...``,
> ``DELETE FROM subnet_allowlist WHERE subnet_id=...``, and
> ``DELETE FROM subnets WHERE subnet_id=...`` in that order. Any
> failure rolls back the whole batch.

Slice 2.1 shipped the three cascade methods but had them each open
and commit their own session, so a process crash between the three
commits left a durable partial cascade. Issue #75 tracked the gap;
Slice 2.1.1 (this PR) closes it by threading an :class:`IUnitOfWork`
through ``SubnetService`` and passing its yielded session token into
every cascade method's ``session=`` kwarg.

The contracts pinned here are session-token-identity contracts —
"the same opaque token flowed through join_requests → allowlist →
subnets". We use a sentinel ``object()`` as the token so the
assertion is exact (no value coincidence). Real semantics
(commit-on-clean-exit, rollback-on-exception) belong to
:class:`PostgresUnitOfWork`, which has its own test coverage; here
we only verify the *plumbing*: that the service actually threads
the token through, and that absence of UoW falls back to the
Slice-2.1 sequential-commit shape unchanged.

The legacy fixtures in
``test_subnet_service_join_policy_cascade.py`` exercise the
"no UoW wired" path and stay green — that's the Slice-2.1
contract preserved.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.entities import Subnet
from acn.services.subnet_service import SubnetService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _subnet(subnet_id: str, parent_subnet_id: str | None = None) -> Subnet:
    return Subnet(
        subnet_id=subnet_id,
        name=subnet_id,
        owner="alice",
        parent_subnet_id=parent_subnet_id,
    )


def _make_uow(token: object):
    """Build a mock :class:`IUnitOfWork` whose ``transaction()`` yields
    the provided ``token``.

    Two assertion seams the tests inspect:

    - ``uow.transaction`` — used exactly once per ``delete_subnet``
      cascade body (the atomic path opens one transaction; the
      no-UoW fallback never reaches this fixture at all because we
      pass ``unit_of_work=None`` for that case).
    - ``entered`` / ``exited`` flags — pin commit-on-clean-exit /
      rollback-on-exception behaviour at the *plumbing* level
      (we don't second-guess PG semantics, only that the service
      did enter and exit the context manager exactly once around
      the cascade body).
    """
    state = {"entered": 0, "exited_ok": 0, "exited_err": 0}

    @asynccontextmanager
    async def _txn():
        state["entered"] += 1
        try:
            yield token
            state["exited_ok"] += 1
        except BaseException:
            state["exited_err"] += 1
            raise

    uow = MagicMock()
    uow.transaction = MagicMock(side_effect=_txn)
    return uow, state


@pytest.fixture
def mock_subnet_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.find_by_parent.return_value = []
    repo.delete.return_value = True
    repo.delete_with_children.return_value = True
    return repo


@pytest.fixture
def mock_jr_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.delete_for_subnet.return_value = 0
    return repo


@pytest.fixture
def mock_al_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.delete_for_subnet.return_value = 0
    return repo


# ---------------------------------------------------------------------------
# Atomic path — single subnet (no children)
# ---------------------------------------------------------------------------


class TestAtomicCascadeSingleSubnet:
    @pytest.mark.asyncio
    async def test_threads_same_session_token_through_all_three_deletes(
        self,
        mock_subnet_repo: AsyncMock,
        mock_jr_repo: AsyncMock,
        mock_al_repo: AsyncMock,
    ):
        """The core acceptance signal from issue #75: when a UoW is
        wired, the join_requests / allowlist / subnets DELETEs each
        receive the *same* session token, and that token is the one
        yielded by ``uow.transaction()``. This is the
        single-PG-transaction guarantee at the plumbing level
        (real commit / rollback is :class:`PostgresUnitOfWork`'s
        responsibility — covered by its own tests / live PG)."""
        sn = _subnet("s-1")
        mock_subnet_repo.find_by_id.return_value = sn

        token = object()  # sentinel for identity comparison
        uow, state = _make_uow(token)

        service = SubnetService(
            mock_subnet_repo,
            subnet_join_request_repository=mock_jr_repo,
            subnet_allowlist_repository=mock_al_repo,
            unit_of_work=uow,
        )
        ok = await service.delete_subnet("s-1", owner="alice")
        assert ok is True

        # One transaction opened, exited cleanly.
        uow.transaction.assert_called_once()
        assert state == {"entered": 1, "exited_ok": 1, "exited_err": 0}

        # All three cascade methods received the SAME token by identity.
        jr_call = mock_jr_repo.delete_for_subnet.await_args
        al_call = mock_al_repo.delete_for_subnet.await_args
        sn_call = mock_subnet_repo.delete.await_args
        assert jr_call.kwargs.get("session") is token
        assert al_call.kwargs.get("session") is token
        assert sn_call.kwargs.get("session") is token

    @pytest.mark.asyncio
    async def test_legacy_path_when_no_uow_wired_passes_none(
        self,
        mock_subnet_repo: AsyncMock,
        mock_jr_repo: AsyncMock,
        mock_al_repo: AsyncMock,
    ):
        """No UoW → service falls back to the Slice-2.1 sequential-
        commit shape: each cascade method still receives a ``session``
        kwarg (Liskov contract — the interface always accepts one),
        but the value is ``None``, signalling each repo to manage
        its own session. This is the contract Redis-only and legacy
        out-of-tree callers rely on."""
        sn = _subnet("s-1")
        mock_subnet_repo.find_by_id.return_value = sn

        service = SubnetService(
            mock_subnet_repo,
            subnet_join_request_repository=mock_jr_repo,
            subnet_allowlist_repository=mock_al_repo,
            # unit_of_work intentionally omitted
        )
        await service.delete_subnet("s-1", owner="alice")

        assert mock_jr_repo.delete_for_subnet.await_args.kwargs.get(
            "session"
        ) is None
        assert mock_al_repo.delete_for_subnet.await_args.kwargs.get(
            "session"
        ) is None
        assert mock_subnet_repo.delete.await_args.kwargs.get("session") is None


# ---------------------------------------------------------------------------
# Atomic path — parent + children
# ---------------------------------------------------------------------------


class TestAtomicCascadeWithChildren:
    @pytest.mark.asyncio
    async def test_same_token_for_every_child_and_parent(
        self,
        mock_subnet_repo: AsyncMock,
        mock_jr_repo: AsyncMock,
        mock_al_repo: AsyncMock,
    ):
        """Every per-subnet cascade sweep (both children + the
        parent) and the final ``delete_with_children`` all use the
        SAME UoW session token — the parent + children + their
        join_policy artifacts must commit / roll back as a unit, not
        as N+1 independent transactions."""
        parent = _subnet("parent")
        children = [
            _subnet("child-1", parent_subnet_id="parent"),
            _subnet("child-2", parent_subnet_id="parent"),
        ]
        mock_subnet_repo.find_by_id.return_value = parent
        mock_subnet_repo.find_by_parent.return_value = children

        token = object()
        uow, state = _make_uow(token)
        service = SubnetService(
            mock_subnet_repo,
            subnet_join_request_repository=mock_jr_repo,
            subnet_allowlist_repository=mock_al_repo,
            unit_of_work=uow,
        )
        await service.delete_subnet("parent", owner="alice")

        # Exactly ONE transaction opened.
        uow.transaction.assert_called_once()
        assert state["exited_ok"] == 1 and state["exited_err"] == 0

        # join_requests cascade: 3 calls (2 children + 1 parent),
        # ALL carrying the same token.
        jr_calls = mock_jr_repo.delete_for_subnet.await_args_list
        assert len(jr_calls) == 3
        jr_subnets = [c.args[0] for c in jr_calls]
        assert set(jr_subnets) == {"parent", "child-1", "child-2"}
        for c in jr_calls:
            assert c.kwargs.get("session") is token

        # allowlist cascade: same shape.
        al_calls = mock_al_repo.delete_for_subnet.await_args_list
        assert len(al_calls) == 3
        for c in al_calls:
            assert c.kwargs.get("session") is token

        # The final batched cascade DELETE uses the token too.
        dwc_call = mock_subnet_repo.delete_with_children.await_args
        assert dwc_call.kwargs.get("session") is token


# ---------------------------------------------------------------------------
# Failure propagation — exception inside cascade must exit the UoW with err
# ---------------------------------------------------------------------------


class TestAtomicCascadeFailurePropagation:
    @pytest.mark.asyncio
    async def test_join_request_raise_exits_uow_with_exception(
        self,
        mock_subnet_repo: AsyncMock,
        mock_jr_repo: AsyncMock,
        mock_al_repo: AsyncMock,
    ):
        """A failure mid-cascade must propagate OUT of the
        ``uow.transaction()`` context with the exception preserved,
        so the real :class:`PostgresUnitOfWork` triggers ROLLBACK
        (its commit-on-clean-exit shape only commits when no
        exception escaped the block). We verify the plumbing-level
        invariant: the context manager's exception branch fired."""
        sn = _subnet("s-x")
        mock_subnet_repo.find_by_id.return_value = sn
        mock_jr_repo.delete_for_subnet.side_effect = RuntimeError(
            "simulated cascade abort"
        )

        token = object()
        uow, state = _make_uow(token)
        service = SubnetService(
            mock_subnet_repo,
            subnet_join_request_repository=mock_jr_repo,
            subnet_allowlist_repository=mock_al_repo,
            unit_of_work=uow,
        )
        with pytest.raises(RuntimeError, match="simulated cascade abort"):
            await service.delete_subnet("s-x", owner="alice")

        # Context manager opened and exited with an exception —
        # this is the rollback-trigger seam.
        assert state == {"entered": 1, "exited_ok": 0, "exited_err": 1}
        # The subnet HASH delete must NOT have been attempted (it
        # would have been inside the same aborted transaction — but
        # asserting "not called" still pins the cascade ordering
        # contract from ADR §"Cascade deletion").
        mock_subnet_repo.delete.assert_not_called()
        mock_subnet_repo.delete_with_children.assert_not_called()

    @pytest.mark.asyncio
    async def test_subnet_delete_raise_exits_uow_with_exception(
        self,
        mock_subnet_repo: AsyncMock,
        mock_jr_repo: AsyncMock,
        mock_al_repo: AsyncMock,
    ):
        """Symmetric case: cascade methods succeed, but the final
        subnet DELETE raises (simulated PG IntegrityError or similar).
        The exception must still escape the UoW context so rollback
        unwinds the already-executed cascade DELETEs. Otherwise the
        ADR-0004 'three DELETEs commit together' guarantee would
        degrade into 'three DELETEs commit independently'."""
        sn = _subnet("s-y")
        mock_subnet_repo.find_by_id.return_value = sn
        mock_subnet_repo.delete.side_effect = RuntimeError("simulated PG abort")

        token = object()
        uow, state = _make_uow(token)
        service = SubnetService(
            mock_subnet_repo,
            subnet_join_request_repository=mock_jr_repo,
            subnet_allowlist_repository=mock_al_repo,
            unit_of_work=uow,
        )
        with pytest.raises(RuntimeError, match="simulated PG abort"):
            await service.delete_subnet("s-y", owner="alice")

        # Both cascade sweeps ran (and would have been rolled back
        # by the real UoW once the subnet DELETE raised).
        mock_jr_repo.delete_for_subnet.assert_awaited_once()
        mock_al_repo.delete_for_subnet.assert_awaited_once()
        # And the context manager saw the exception.
        assert state["exited_err"] == 1
        assert state["exited_ok"] == 0


# ---------------------------------------------------------------------------
# UoW wired but cascade repos absent — fast-path through empty cascade body
# ---------------------------------------------------------------------------


class TestAtomicPathWithoutCascadeRepos:
    @pytest.mark.asyncio
    async def test_uow_opened_even_when_cascade_repos_omitted(
        self, mock_subnet_repo: AsyncMock
    ):
        """The current production wiring shape during Slice 2.1.1
        (this PR): ``unit_of_work`` is wired but
        ``subnet_join_request_repository`` /
        ``subnet_allowlist_repository`` are NOT — Slice 2.2 will
        land those. ``delete_subnet`` must still open a UoW (so the
        single subnet DELETE participates in it) and exit cleanly
        without crashing on the missing cascade repos.

        This pins the explicit no-op fast-path that the wiring docs
        promise (api.py comment block on ``unit_of_work=_unit_of_work``).
        """
        sn = _subnet("s-1")
        mock_subnet_repo.find_by_id.return_value = sn

        token = object()
        uow, state = _make_uow(token)
        service = SubnetService(
            mock_subnet_repo,
            unit_of_work=uow,
            # No cascade repos wired — Slice 2.1.1 transitional shape.
        )
        ok = await service.delete_subnet("s-1", owner="alice")

        assert ok is True
        uow.transaction.assert_called_once()
        assert state["exited_ok"] == 1
        # The subnet DELETE was the only call, and it received the token.
        mock_subnet_repo.delete.assert_awaited_once()
        assert mock_subnet_repo.delete.await_args.kwargs.get("session") is token
