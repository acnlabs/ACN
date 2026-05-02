"""Direct unit tests for ``BroadcastService.broadcast()`` — the unified entry.

Phase 2 Group C #9 / review v2 P1 #7 added a high-level ``broadcast()``
method on top of the existing ``send`` / ``send_by_tag`` API so the HTTP
routes can hit one entry-point that handles sender existence checks,
selector-based target resolution (``target_agents`` / ``subnet_id`` /
``tags`` / all-agents fallback), and sender auto-filter — all the
business logic that used to live in ``MessageService.broadcast_message``.

The route-level contract test (``test_broadcast_service_convergence.py``)
covers the **integration** by stubbing the entire BroadcastService and
asserting on what kwargs the route handlers pass in. That doesn't pin
the selector / filter / existence-check branches **inside** ``broadcast()``
itself — if a future refactor swaps the precedence order, drops the
sender filter, or breaks the empty-target short-circuit, the integration
test would still pass (it stubs the method out entirely).

This file fills that gap with direct unit tests: each branch of
``broadcast()`` is exercised against a real ``BroadcastService`` instance
with mocked repository + router + redis_client, so semantic regressions
surface at the unit layer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from a2a.compat.v0_3.types import Message, Role, TextPart

from acn.core.exceptions import AgentNotFoundException
from acn.infrastructure.messaging.broadcast_service import (
    BroadcastResult,
    BroadcastService,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _make_message() -> Message:
    return Message(
        role=Role.user,
        message_id="msg-unified-test",
        parts=[TextPart(text="hello")],
    )


def _make_agent(agent_id: str):
    a = MagicMock()
    a.agent_id = agent_id
    return a


def _make_repo(
    *,
    sender_id: str | None = "agent-sender",
    by_subnet: list[str] | None = None,
    by_tags: list[str] | None = None,
    all_agents: list[str] | None = None,
):
    """Build an IAgentRepository stub.

    ``sender_id``: if set, ``find_by_id`` returns a matching agent for that
    id and ``None`` for everything else. Pass ``sender_id=None`` to make
    the repo treat every lookup as "not found" — useful for the
    ``AgentNotFoundException`` branch test.

    ``by_subnet`` / ``by_tags`` / ``all_agents``: pre-seeded results for
    the corresponding repository methods. Each test asserts which of
    these were *called*, so unrelated paths returning ``[]`` is fine.
    """
    repo = MagicMock()

    async def find_by_id(aid: str):
        if sender_id is not None and aid == sender_id:
            return _make_agent(aid)
        return None

    repo.find_by_id = AsyncMock(side_effect=find_by_id)
    repo.find_by_subnet = AsyncMock(
        return_value=[_make_agent(a) for a in (by_subnet or [])]
    )
    repo.find_by_tags = AsyncMock(
        return_value=[_make_agent(a) for a in (by_tags or [])]
    )
    repo.find_all = AsyncMock(
        return_value=[_make_agent(a) for a in (all_agents or [])]
    )
    return repo


def _make_router():
    """Minimal MessageRouter stub. The unified entry's responsibility is
    to *resolve targets and delegate to ``self.send()``* — the actual
    fan-out semantics are pinned by ``test_broadcast_service_policy.py``,
    so here we only need router.route() to be a no-op success."""
    router = MagicMock()

    async def route(from_agent, to_agent, message, **kwargs):
        return {"status": "delivered", "to": to_agent}

    router.route = route
    router.registry = MagicMock()
    return router


def _make_service(repo=None, *, with_repo: bool = True) -> BroadcastService:
    """Build a BroadcastService for unit testing.

    Defaults to wiring an agent_repository (the production case). Pass
    ``with_repo=False`` to test the guard branch where ``broadcast()``
    should raise RuntimeError.
    """
    redis_client = MagicMock()
    redis_client.setex = AsyncMock()
    svc = BroadcastService(
        router=_make_router(),
        redis_client=redis_client,
        agent_repository=(repo if with_repo else None),
    )
    # Silence the broadcast log sink — separately tested by
    # test_broadcast_service_policy.py and not the focus here.
    svc._log_broadcast = AsyncMock()
    return svc


# --------------------------------------------------------------------------- #
# 1. Construction-time guard — broadcast() requires agent_repository
# --------------------------------------------------------------------------- #


class TestBroadcastRequiresAgentRepository:
    """``BroadcastService`` keeps ``agent_repository`` optional in the
    constructor for backward-compat (the legacy ``send`` / ``send_by_tag``
    A2A path doesn't need it). But ``broadcast()`` strictly requires it
    — calling ``broadcast()`` on a repo-less service must fail loud
    rather than silently NoneType-attribute-erroring deep inside the
    selector branches."""

    @pytest.mark.asyncio
    async def test_broadcast_without_repo_raises_runtime_error(self):
        svc = _make_service(with_repo=False)

        with pytest.raises(RuntimeError, match="requires agent_repository"):
            await svc.broadcast(
                from_agent="agent-sender",
                message=_make_message(),
            )

    @pytest.mark.asyncio
    async def test_send_without_repo_still_works(self):
        """Regression guard: the repo-less branch of the constructor
        is still useful for the A2A path, which calls ``send`` directly.
        ``send()`` must not gain an accidental dependency on repo."""
        svc = _make_service(with_repo=False)

        result = await svc.send(
            from_agent="agent-a",
            to_agents=["agent-b"],
            message=_make_message(),
        )

        assert isinstance(result, BroadcastResult)
        assert result.total == 1
        assert result.success == 1


# --------------------------------------------------------------------------- #
# 2. Sender existence check
# --------------------------------------------------------------------------- #


class TestSenderExistence:
    """``broadcast()`` must verify the sender exists before any
    target resolution work, and raise ``AgentNotFoundException`` so
    the route layer can map it to HTTP 404. This was the explicit
    contract of the deleted ``MessageService.broadcast_message``;
    the unified entry must preserve it."""

    @pytest.mark.asyncio
    async def test_missing_sender_raises_agent_not_found(self):
        # sender_id=None makes find_by_id always return None.
        repo = _make_repo(sender_id=None)
        svc = _make_service(repo)

        with pytest.raises(AgentNotFoundException, match="agent-ghost"):
            await svc.broadcast(
                from_agent="agent-ghost",
                message=_make_message(),
                target_agents=["agent-x"],
            )

        # Selector resolution must NOT have run when the sender check
        # failed — pinning that the existence check is genuinely first
        # rather than incidentally first.
        repo.find_by_subnet.assert_not_awaited()
        repo.find_by_tags.assert_not_awaited()
        repo.find_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_existing_sender_passes_through_to_resolution(self):
        """Happy path: sender exists, selector resolution proceeds.
        Pin that find_by_id is awaited exactly once (no double lookups)."""
        repo = _make_repo(sender_id="agent-real", by_tags=["agent-x"])
        svc = _make_service(repo)

        await svc.broadcast(
            from_agent="agent-real",
            message=_make_message(),
            tags=["frontend"],
        )

        repo.find_by_id.assert_awaited_once_with("agent-real")


# --------------------------------------------------------------------------- #
# 3. Selector precedence — target_agents > subnet_id > tags > all
# --------------------------------------------------------------------------- #


class TestSelectorPrecedence:
    """The unified entry has four target-resolution paths. Precedence
    is documented in the docstring as ``target_agents > subnet_id >
    tags > all``. These tests pin that contract: when multiple
    selectors are passed, only the highest-priority one drives
    resolution, and the lower-priority repository methods are NOT
    called (saves a round-trip and avoids ambiguity)."""

    @pytest.mark.asyncio
    async def test_target_agents_takes_precedence_over_everything(self):
        repo = _make_repo(
            sender_id="agent-sender",
            by_subnet=["should-not-appear"],
            by_tags=["should-not-appear"],
            all_agents=["should-not-appear"],
        )
        svc = _make_service(repo)

        result = await svc.broadcast(
            from_agent="agent-sender",
            message=_make_message(),
            target_agents=["agent-a", "agent-b"],
            subnet_id="ignored-subnet",
            tags=["ignored-tag"],
        )

        # Only the explicit targets should appear in results.
        assert set(result.results.keys()) == {"agent-a", "agent-b"}
        # None of the repo selectors should have been queried.
        repo.find_by_subnet.assert_not_awaited()
        repo.find_by_tags.assert_not_awaited()
        repo.find_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_subnet_takes_precedence_over_tags_and_all(self):
        repo = _make_repo(
            sender_id="agent-sender",
            by_subnet=["agent-a"],
            by_tags=["should-not-appear"],
            all_agents=["should-not-appear"],
        )
        svc = _make_service(repo)

        result = await svc.broadcast(
            from_agent="agent-sender",
            message=_make_message(),
            subnet_id="subnet-1",
            tags=["frontend"],
        )

        assert set(result.results.keys()) == {"agent-a"}
        repo.find_by_subnet.assert_awaited_once_with("subnet-1")
        repo.find_by_tags.assert_not_awaited()
        repo.find_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tags_takes_precedence_over_all(self):
        repo = _make_repo(
            sender_id="agent-sender",
            by_tags=["agent-a"],
            all_agents=["should-not-appear"],
        )
        svc = _make_service(repo)

        result = await svc.broadcast(
            from_agent="agent-sender",
            message=_make_message(),
            tags=["frontend", "review"],
        )

        assert set(result.results.keys()) == {"agent-a"}
        repo.find_by_tags.assert_awaited_once_with(["frontend", "review"])
        repo.find_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_selector_falls_back_to_find_all(self):
        repo = _make_repo(
            sender_id="agent-sender",
            all_agents=["agent-a", "agent-b"],
        )
        svc = _make_service(repo)

        result = await svc.broadcast(
            from_agent="agent-sender",
            message=_make_message(),
        )

        assert set(result.results.keys()) == {"agent-a", "agent-b"}
        repo.find_all.assert_awaited_once()


# --------------------------------------------------------------------------- #
# 4. Sender auto-filter — across all selector paths
# --------------------------------------------------------------------------- #


class TestSenderAutoFilter:
    """The sender must NEVER be in the resolved target set, regardless
    of which selector produced the list. Includes the explicit
    ``target_agents=[from_agent]`` corner case — the caller asked to
    message themselves, but broadcast semantics say no (a fan-out is
    by definition to *other* agents)."""

    @pytest.mark.asyncio
    async def test_sender_filtered_from_explicit_target_agents(self):
        repo = _make_repo(sender_id="agent-self")
        svc = _make_service(repo)

        result = await svc.broadcast(
            from_agent="agent-self",
            message=_make_message(),
            target_agents=["agent-self", "agent-other"],
        )

        assert set(result.results.keys()) == {"agent-other"}

    @pytest.mark.asyncio
    async def test_sender_filtered_from_subnet_resolution(self):
        repo = _make_repo(
            sender_id="agent-self",
            by_subnet=["agent-self", "agent-other"],
        )
        svc = _make_service(repo)

        result = await svc.broadcast(
            from_agent="agent-self",
            message=_make_message(),
            subnet_id="subnet-x",
        )

        assert set(result.results.keys()) == {"agent-other"}

    @pytest.mark.asyncio
    async def test_sender_filtered_from_tags_resolution(self):
        repo = _make_repo(
            sender_id="agent-self",
            by_tags=["agent-self", "agent-other"],
        )
        svc = _make_service(repo)

        result = await svc.broadcast(
            from_agent="agent-self",
            message=_make_message(),
            tags=["frontend"],
        )

        assert set(result.results.keys()) == {"agent-other"}

    @pytest.mark.asyncio
    async def test_sender_filtered_from_find_all(self):
        repo = _make_repo(
            sender_id="agent-self",
            all_agents=["agent-self", "agent-other"],
        )
        svc = _make_service(repo)

        result = await svc.broadcast(
            from_agent="agent-self",
            message=_make_message(),
        )

        assert set(result.results.keys()) == {"agent-other"}

    @pytest.mark.asyncio
    async def test_sender_only_target_yields_empty_fanout(self):
        """``target_agents=[from_agent]`` is a meaningful corner case
        — the caller explicitly asked to message themselves, the
        sender filter strips the only entry, and we end up with an
        empty fan-out. Pin that this is treated as the empty branch
        (zero-target BroadcastResult) rather than an error."""
        repo = _make_repo(sender_id="agent-self")
        svc = _make_service(repo)

        result = await svc.broadcast(
            from_agent="agent-self",
            message=_make_message(),
            target_agents=["agent-self"],
        )

        assert result.total == 0
        assert result.success == 0
        assert result.results == {}


# --------------------------------------------------------------------------- #
# 5. Empty fan-out short-circuit — does NOT call self.send()
# --------------------------------------------------------------------------- #


class TestEmptyFanoutShortCircuit:
    """When the resolved + filtered target set is empty, ``broadcast()``
    must short-circuit and return a stub BroadcastResult *without*
    delegating to ``self.send()``. This avoids a useless Redis log
    write for a fan-out that delivered to nobody, and matches the
    legacy ``MessageService.broadcast_message`` behaviour of returning
    ``[]`` early.

    The broadcast_id is still issued (for caller-side telemetry /
    log correlation) but is not persisted — clients should not
    expect ``get_broadcast_status(broadcast_id)`` to return a
    record for empty fan-outs."""

    @pytest.mark.asyncio
    async def test_empty_resolution_returns_zero_result_without_send(self):
        repo = _make_repo(sender_id="agent-sender", all_agents=[])
        svc = _make_service(repo)
        # Spy on send to verify the short-circuit. We DON'T replace
        # it with AsyncMock at the constructor — we want the real
        # implementation present so accidentally landing in it
        # would surface as router.route being called.
        send_spy = AsyncMock(side_effect=AssertionError(
            "broadcast() must NOT delegate to self.send() when the "
            "resolved target set is empty — empty fan-out has no "
            "messages to deliver."
        ))
        svc.send = send_spy

        result = await svc.broadcast(
            from_agent="agent-sender",
            message=_make_message(),
        )

        assert result.total == 0
        assert result.success == 0
        assert result.failed == 0
        assert result.results == {}
        # broadcast_id is still issued — caller-visible identity for
        # log correlation even though nothing was persisted.
        assert isinstance(result.broadcast_id, str)
        assert len(result.broadcast_id) == 12
        send_spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_log_broadcast_not_called_on_empty_fanout(self):
        """Tightens the previous test: the Redis log sink also must
        not run for empty fan-outs. Pinning this so any future
        refactor that "always logs broadcasts for observability"
        has to deliberately revisit the empty-case decision rather
        than silently writing a stub log."""
        repo = _make_repo(sender_id="agent-sender", all_agents=[])
        svc = _make_service(repo)

        await svc.broadcast(
            from_agent="agent-sender",
            message=_make_message(),
        )

        svc._log_broadcast.assert_not_awaited()


# --------------------------------------------------------------------------- #
# 6. Non-empty fan-out — delegates to self.send() correctly
# --------------------------------------------------------------------------- #


class TestNonEmptyDelegation:
    """The non-empty branch must delegate to ``self.send()`` with the
    resolved+filtered target list and propagate the full
    BroadcastResult (broadcast_id, results, stats) back to the
    caller. Pinning this contract so future changes to ``send()``
    signature stay observable from the unified entry."""

    @pytest.mark.asyncio
    async def test_non_empty_resolution_delegates_to_send(self):
        repo = _make_repo(
            sender_id="agent-sender",
            by_tags=["agent-a", "agent-b"],
        )
        svc = _make_service(repo)

        # Replace send with a spy that returns a synthetic result so
        # we can assert the unified entry forwards it untouched.
        synthetic = BroadcastResult(
            broadcast_id="bcast-fixed",
            total=2,
            success=2,
            failed=0,
            results={"agent-a": {"ok": True}, "agent-b": {"ok": True}},
        )
        send_spy = AsyncMock(return_value=synthetic)
        svc.send = send_spy

        result = await svc.broadcast(
            from_agent="agent-sender",
            message=_make_message(),
            tags=["frontend"],
        )

        send_spy.assert_awaited_once()
        kwargs = send_spy.await_args.kwargs
        assert kwargs["from_agent"] == "agent-sender"
        assert set(kwargs["to_agents"]) == {"agent-a", "agent-b"}
        # The unified entry returns send()'s result verbatim — no
        # rewriting / wrapping. Caller (HTTP route) gets the full
        # BroadcastResult including the persisted broadcast_id.
        assert result is synthetic
