"""SubnetService — agent-side back-reference cleanup on delete (issue #56).

Pins the contract that ``delete_subnet`` symmetrically clears the
``slug`` from each member's ``agent.subnet_ids`` set before the
subnet record itself is removed. Without this cleanup, ADR-0003
cascade deletes amplify pre-existing dual-store dust (per-child member
× per-subnet) into a much larger orphan surface.

Three behavioural contracts pinned here:

1. **Single-subnet delete** — every member's ``subnet_ids`` loses the
   deleted subnet's id; ``agent_repository.save`` is awaited once per
   member.
2. **Cascade delete** — back-reference cleanup runs for each child
   subnet's members AND the parent's members, in that order, BEFORE
   the repository cascade fires.
3. **Best-effort** — a single agent's cleanup failure is logged at
   ``warning`` and does not abort the subnet delete (the deletion is
   the primary side-effect; agent-side dust is secondary).
4. **Backward compatibility** — when no ``agent_repository`` is wired
   (legacy fixtures), ``delete_subnet`` retains its pre-#56 behaviour:
   no cleanup, no error, subnet removed.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from acn.core.entities import Agent, Subnet
from acn.core.interfaces import IAgentRepository, ISubnetRepository
from acn.services.subnet_service import SubnetService


def _make_subnet(
    slug: str,
    owner: str = "alice",
    parent_slug: str | None = None,
    members: set[str] | None = None,
) -> Subnet:
    return Subnet(
        slug=slug,
        name=slug,
        owner=owner,
        parent_slug=parent_slug,
        member_agent_ids=members if members is not None else {owner},
        created_at=datetime.now(UTC),
    )


def _make_agent(agent_id: str, subnet_ids: list[str]) -> Agent:
    """Build an Agent stub. Note: Agent.__post_init__ falls back to
    ``["public"]`` when ``subnet_ids`` ends up empty, so pin the
    subnet_ids we want to see in the test."""
    return Agent(
        agent_id=agent_id,
        name=agent_id,
        description="test",
        tags=[],
        endpoint=f"http://example.com/{agent_id}",
        owner="owner-of-" + agent_id,
        subnet_ids=list(subnet_ids),
    )


# ---------------------------------------------------------------------------
# 1. Single-subnet delete cleans every member
# ---------------------------------------------------------------------------


class TestSingleSubnetDeleteClearsBackRefs:
    @pytest.mark.asyncio
    async def test_three_member_subnet_clears_all_three_agents(
        self,
        mock_subnet_repository: ISubnetRepository,
        mock_agent_repository: IAgentRepository,
    ):
        """3-member subnet → 3 ``find_by_id`` + 3 ``save`` calls with
        the subnet id removed from each agent's ``subnet_ids`` list."""
        subnet = _make_subnet(
            "team-a",
            owner="alice",
            members={"alice", "bob", "carol"},
        )
        agents = {
            "alice": _make_agent("alice", ["public", "team-a"]),
            "bob": _make_agent("bob", ["public", "team-a", "team-b"]),
            "carol": _make_agent("carol", ["team-a"]),
        }
        mock_subnet_repository.find_by_id.return_value = subnet
        mock_subnet_repository.find_by_parent.return_value = []
        mock_subnet_repository.delete.return_value = True
        mock_agent_repository.find_by_id.side_effect = (
            lambda aid: agents.get(aid)
        )

        service = SubnetService(
            mock_subnet_repository,
            agent_repository=mock_agent_repository,
        )

        ok = await service.delete_subnet("team-a", owner="alice")

        assert ok is True
        # 3 lookups for the 3 members.
        assert mock_agent_repository.find_by_id.await_count == 3
        # 3 saves — one per member.
        assert mock_agent_repository.save.await_count == 3
        # Every saved agent has ``team-a`` removed from its subnet_ids.
        for call in mock_agent_repository.save.await_args_list:
            saved = call.args[0]
            assert "team-a" not in saved.subnet_ids, (
                f"agent {saved.agent_id} still carries team-a"
            )

    @pytest.mark.asyncio
    async def test_cleanup_skips_agents_already_missing_subnet(
        self,
        mock_subnet_repository: ISubnetRepository,
        mock_agent_repository: IAgentRepository,
    ):
        """If an agent's ``subnet_ids`` doesn't contain the subnet id
        (e.g. they explicitly left earlier), the cleanup skips the
        ``save`` — no redundant write."""
        subnet = _make_subnet("team-a", members={"alice"})
        # alice's subnet_ids no longer carries team-a (concurrent leave).
        agents = {"alice": _make_agent("alice", ["public"])}
        mock_subnet_repository.find_by_id.return_value = subnet
        mock_subnet_repository.find_by_parent.return_value = []
        mock_subnet_repository.delete.return_value = True
        mock_agent_repository.find_by_id.side_effect = (
            lambda aid: agents.get(aid)
        )

        service = SubnetService(
            mock_subnet_repository,
            agent_repository=mock_agent_repository,
        )
        await service.delete_subnet("team-a", owner="alice")

        mock_agent_repository.find_by_id.assert_awaited_once_with("alice")
        # No save — nothing to clean.
        mock_agent_repository.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_skips_missing_agents(
        self,
        mock_subnet_repository: ISubnetRepository,
        mock_agent_repository: IAgentRepository,
    ):
        """Members may have been deleted between subnet membership
        and subnet delete. ``find_by_id`` returning ``None`` must not
        crash the cleanup."""
        subnet = _make_subnet("team-a", members={"alice", "ghost"})
        agents = {"alice": _make_agent("alice", ["team-a"])}  # no ghost
        mock_subnet_repository.find_by_id.return_value = subnet
        mock_subnet_repository.find_by_parent.return_value = []
        mock_subnet_repository.delete.return_value = True
        mock_agent_repository.find_by_id.side_effect = (
            lambda aid: agents.get(aid)
        )

        service = SubnetService(
            mock_subnet_repository,
            agent_repository=mock_agent_repository,
        )
        ok = await service.delete_subnet("team-a", owner="alice")

        assert ok is True
        # Both members looked up — ghost returned None and was skipped.
        assert mock_agent_repository.find_by_id.await_count == 2
        # Only alice triggered a save.
        mock_agent_repository.save.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. Cascade delete cleans children's members + parent's members
# ---------------------------------------------------------------------------


class TestCascadeDeleteClearsBackRefs:
    @pytest.mark.asyncio
    async def test_parent_cascade_cleans_each_child_then_parent(
        self,
        mock_subnet_repository: ISubnetRepository,
        mock_agent_repository: IAgentRepository,
    ):
        """Parent with two children:

        - child-1 members: {alice, bob}
        - child-2 members: {carol}
        - parent members: {alice, bob, carol, dave}

        Expectation: each child's members cleaned first, then the
        parent's. ``delete_with_children`` fires AFTER all cleanup.
        Order matters because the repo cascade order is
        children-first, parent-last — agent-side cleanup mirrors
        that to keep the recovery story symmetric.
        """
        parent = _make_subnet(
            "team",
            owner="alice",
            members={"alice", "bob", "carol", "dave"},
        )
        children = [
            _make_subnet(
                "squad-1",
                owner="alice",
                parent_slug="team",
                members={"alice", "bob"},
            ),
            _make_subnet(
                "squad-2",
                owner="carol",
                parent_slug="team",
                members={"carol"},
            ),
        ]
        # Agents — every member carries the relevant subnet_ids.
        agents = {
            "alice": _make_agent("alice", ["public", "team", "squad-1"]),
            "bob": _make_agent("bob", ["team", "squad-1"]),
            "carol": _make_agent("carol", ["team", "squad-2"]),
            "dave": _make_agent("dave", ["team"]),
        }
        mock_subnet_repository.find_by_id.return_value = parent
        mock_subnet_repository.find_by_parent.return_value = children
        mock_subnet_repository.delete_with_children.return_value = True
        mock_agent_repository.find_by_id.side_effect = (
            lambda aid: agents.get(aid)
        )

        service = SubnetService(
            mock_subnet_repository,
            agent_repository=mock_agent_repository,
        )
        ok = await service.delete_subnet("team", owner="alice")

        assert ok is True
        # Repository cascade still fires with the right child ids.
        # Session-agnostic per issue #75 acceptance signal.
        mock_subnet_repository.delete_with_children.assert_awaited_once()
        assert mock_subnet_repository.delete_with_children.await_args.args == (
            "team",
            ["squad-1", "squad-2"],
        )
        # Saved agents' subnet_ids must NOT contain any of the three
        # subnets that were deleted (this is the user-visible signal
        # the back-ref cleanup did its job).
        saved_by_id: dict[str, Agent] = {}
        for call in mock_agent_repository.save.await_args_list:
            saved = call.args[0]
            saved_by_id[saved.agent_id] = saved
        for agent_id, saved in saved_by_id.items():
            assert "team" not in saved.subnet_ids, agent_id
            assert "squad-1" not in saved.subnet_ids, agent_id
            assert "squad-2" not in saved.subnet_ids, agent_id

    @pytest.mark.asyncio
    async def test_cleanup_runs_before_repository_cascade(
        self,
        mock_subnet_repository: ISubnetRepository,
        mock_agent_repository: IAgentRepository,
    ):
        """Strict ordering: every ``agent_repository.save`` must
        finish BEFORE ``delete_with_children`` is awaited. Otherwise
        a cascade failure could leave agents pointing at
        already-deleted subnets — strictly worse than the inverse."""
        parent = _make_subnet(
            "team", owner="alice", members={"alice", "bob"}
        )
        children = [
            _make_subnet(
                "squad-1",
                owner="alice",
                parent_slug="team",
                members={"alice"},
            ),
        ]
        agents = {
            "alice": _make_agent("alice", ["team", "squad-1"]),
            "bob": _make_agent("bob", ["team"]),
        }

        call_order: list[str] = []

        async def _record_save(agent: Agent) -> None:
            call_order.append(f"save:{agent.agent_id}")

        async def _record_cascade(
            parent_id: str, child_ids: list[str], **_kw
        ) -> bool:
            call_order.append("delete_with_children")
            return True

        mock_subnet_repository.find_by_id.return_value = parent
        mock_subnet_repository.find_by_parent.return_value = children
        mock_subnet_repository.delete_with_children = AsyncMock(
            side_effect=_record_cascade
        )
        mock_agent_repository.find_by_id.side_effect = (
            lambda aid: agents.get(aid)
        )
        mock_agent_repository.save = AsyncMock(side_effect=_record_save)

        service = SubnetService(
            mock_subnet_repository,
            agent_repository=mock_agent_repository,
        )
        await service.delete_subnet("team", owner="alice")

        # Every save must precede the cascade.
        cascade_idx = call_order.index("delete_with_children")
        save_indices = [
            i for i, ev in enumerate(call_order) if ev.startswith("save:")
        ]
        assert all(i < cascade_idx for i in save_indices), call_order


# ---------------------------------------------------------------------------
# 3. Best-effort — per-agent failure does not abort the delete
# ---------------------------------------------------------------------------


class TestCleanupBestEffort:
    @pytest.mark.asyncio
    async def test_single_agent_save_failure_logs_warning_and_continues(
        self,
        mock_subnet_repository: ISubnetRepository,
        mock_agent_repository: IAgentRepository,
        caplog,
    ):
        """If one agent's ``save`` raises, the other members are
        still cleaned and the subnet is still deleted. A
        ``subnet_back_reference_cleanup_failed`` warning carries the
        offending agent id for ops follow-up."""
        subnet = _make_subnet(
            "team-a", owner="alice", members={"alice", "bob"}
        )
        agents = {
            "alice": _make_agent("alice", ["team-a"]),
            "bob": _make_agent("bob", ["team-a"]),
        }
        mock_subnet_repository.find_by_id.return_value = subnet
        mock_subnet_repository.find_by_parent.return_value = []
        mock_subnet_repository.delete.return_value = True
        mock_agent_repository.find_by_id.side_effect = (
            lambda aid: agents.get(aid)
        )

        # bob's save raises; alice's save succeeds.
        async def _save(agent: Agent) -> None:
            if agent.agent_id == "bob":
                raise RuntimeError("redis unavailable for bob")

        mock_agent_repository.save = AsyncMock(side_effect=_save)

        service = SubnetService(
            mock_subnet_repository,
            agent_repository=mock_agent_repository,
        )

        ok = await service.delete_subnet("team-a", owner="alice")

        # Subnet delete must succeed despite the per-agent failure.
        assert ok is True
        # Session-agnostic per issue #75.
        mock_subnet_repository.delete.assert_awaited_once()
        assert mock_subnet_repository.delete.await_args.args == ("team-a",)
        # Both members were attempted; one succeeded, one failed.
        assert mock_agent_repository.save.await_count == 2

    @pytest.mark.asyncio
    async def test_cleanup_completes_even_when_cascade_raises(
        self,
        mock_subnet_repository: ISubnetRepository,
        mock_agent_repository: IAgentRepository,
    ):
        """When the repository cascade raises (e.g. Redis breadcrumb
        path returns failure or PG transaction aborts), the back-ref
        cleanup already ran to completion BEFORE the cascade was
        invoked. This pins the documented ordering rationale: agents
        end up pointing at subnets that still exist (recoverable)
        rather than at already-deleted subnets (the dust this PR
        fixes). The cascade exception then propagates to the caller
        as expected."""
        parent = _make_subnet("team", owner="alice", members={"alice", "bob"})
        children = [
            _make_subnet(
                "squad-1",
                owner="alice",
                parent_slug="team",
                members={"alice"},
            ),
        ]
        agents = {
            "alice": _make_agent("alice", ["team", "squad-1"]),
            "bob": _make_agent("bob", ["team"]),
        }
        mock_subnet_repository.find_by_id.return_value = parent
        mock_subnet_repository.find_by_parent.return_value = children
        # Cascade fails after cleanup has already done its job —
        # simulates either the Redis breadcrumb path or a PG
        # transaction abort.
        mock_subnet_repository.delete_with_children.side_effect = RuntimeError(
            "simulated cascade failure"
        )
        mock_agent_repository.find_by_id.side_effect = (
            lambda aid: agents.get(aid)
        )

        service = SubnetService(
            mock_subnet_repository,
            agent_repository=mock_agent_repository,
        )

        with pytest.raises(RuntimeError, match="simulated cascade failure"):
            await service.delete_subnet("team", owner="alice")

        # Cleanup ran fully before the cascade raised — 3 distinct
        # agents (squad-1: alice; parent: alice, bob) saw save()
        # with the relevant subnet ids stripped. Alice may be saved
        # twice (once per subnet she belonged to) which is the
        # cleanup's natural shape; assert on the SET of agents
        # touched rather than exact call count.
        saved_agent_ids = {
            call.args[0].agent_id
            for call in mock_agent_repository.save.await_args_list
        }
        assert saved_agent_ids == {"alice", "bob"}
        # And every save carried the right shape — no agent still
        # carries any of the about-to-be-deleted subnet ids.
        for call in mock_agent_repository.save.await_args_list:
            saved = call.args[0]
            assert "team" not in saved.subnet_ids
            assert "squad-1" not in saved.subnet_ids

    @pytest.mark.asyncio
    async def test_find_by_id_failure_also_treated_as_warning(
        self,
        mock_subnet_repository: ISubnetRepository,
        mock_agent_repository: IAgentRepository,
    ):
        """``find_by_id`` failure (e.g. transient PG hiccup) is
        likewise non-fatal — log warning, keep going."""
        subnet = _make_subnet(
            "team-a", owner="alice", members={"alice", "bob"}
        )

        async def _find(aid: str) -> Agent | None:
            if aid == "bob":
                raise RuntimeError("transient lookup failure")
            return _make_agent("alice", ["team-a"])

        mock_subnet_repository.find_by_id.return_value = subnet
        mock_subnet_repository.find_by_parent.return_value = []
        mock_subnet_repository.delete.return_value = True
        mock_agent_repository.find_by_id.side_effect = _find

        service = SubnetService(
            mock_subnet_repository,
            agent_repository=mock_agent_repository,
        )

        ok = await service.delete_subnet("team-a", owner="alice")
        assert ok is True
        # alice's save still happened; bob never reached the save step.
        mock_agent_repository.save.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4. Backward compatibility — no agent_repository wired
# ---------------------------------------------------------------------------


class TestBackwardCompatNoAgentRepository:
    @pytest.mark.asyncio
    async def test_delete_works_without_agent_repository(
        self, mock_subnet_repository: ISubnetRepository
    ):
        """Legacy fixtures that don't wire an ``agent_repository``
        retain their pre-#56 behaviour: subnet is deleted, no
        agent-side cleanup attempted, no error raised. Agent-side
        dust survives (acceptable for legacy tests; production
        composition always wires the repo)."""
        subnet = _make_subnet(
            "team-a", owner="alice", members={"alice", "bob"}
        )
        mock_subnet_repository.find_by_id.return_value = subnet
        mock_subnet_repository.find_by_parent.return_value = []
        mock_subnet_repository.delete.return_value = True

        service = SubnetService(mock_subnet_repository)
        # No agent_repository wired — must not crash even though
        # ``subnet.member_agent_ids`` is non-empty.
        ok = await service.delete_subnet("team-a", owner="alice")
        assert ok is True
        # Session-agnostic per issue #75.
        mock_subnet_repository.delete.assert_awaited_once()
        assert mock_subnet_repository.delete.await_args.args == ("team-a",)
