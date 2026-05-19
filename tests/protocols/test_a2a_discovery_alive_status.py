"""Regression tests for ``ACNAgentExecutor._handle_discovery`` status field.

Before 2026-05, the A2A discovery response read ``agent.status``
directly from the Agent entity. That field was removed in an earlier
phase of the alive-as-single-source-of-truth migration (commit
``e616541``), which left the discovery handler emitting an
``AttributeError`` for any client that hit it — silent dead code,
because no test exercised this exact path.

The 2026-05 AgentRegistry-removal commit (``69b9d0f``) fixed this by
recomputing ``status`` from the Redis alive index:

    alive_ids = await agent_service.repository.filter_alive(
        [agent.agent_id for agent in agents]
    )
    ...
    "status": "online" if agent.agent_id in alive_ids else "offline"

These tests pin that contract:

* discovery response carries one entry per discovered agent;
* each entry's ``status`` field is exactly ``"online"`` when the agent
  is in the alive set and exactly ``"offline"`` when it is not;
* ``filter_alive`` is called with the full list of discovered ids (one
  Redis round-trip, not N).

The carve-out is intentionally narrow because the rest of the discovery
contract (artifact name, top-level ``total``, the other per-agent
fields) is already covered by upstream A2A SDK tests; what's specific
to this codebase is the alive-derived status projection."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from a2a.compat.v0_3.types import (
    Artifact,
    DataPart,
    Message,
    Role,
    TaskArtifactUpdateEvent,
)

from acn.core.entities import Agent
from acn.protocols.a2a.server import ACNAgentExecutor


def _make_executor(*, search_result: list[Agent], alive_ids: set[str]):
    """Build an executor wired with stubbed agent_service search +
    filter_alive. Other downstream services are MagicMocks because
    ``_handle_discovery`` doesn't touch router / broadcast / subnet_manager."""
    agent_service = MagicMock()
    agent_service.search_agents = AsyncMock(return_value=search_result)
    agent_service.repository = MagicMock()
    agent_service.repository.filter_alive = AsyncMock(return_value=alive_ids)

    return ACNAgentExecutor(
        agent_service=agent_service,
        router=MagicMock(),
        broadcast=MagicMock(),
        subnet_manager=MagicMock(),
    )


def _discovery_message(tags: list[str] | None = None) -> Message:
    return Message(
        role=Role.user,
        message_id="msg-1",
        parts=[DataPart(data={"tags": tags or []})],
    )


def _make_event_queue() -> MagicMock:
    q = MagicMock()
    q.enqueue_event = AsyncMock()
    return q


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.metadata = {}
    ctx.task_id = "task-1"
    ctx.context_id = "ctx-1"
    return ctx


def _artifact_data(eq: MagicMock) -> dict[str, Any]:
    """Pull the DataPart payload out of the artifact event the handler
    enqueued. There's only one artifact in the discovery path."""
    artifact_events = [
        call.args[0]
        for call in eq.enqueue_event.call_args_list
        if isinstance(call.args[0], TaskArtifactUpdateEvent)
    ]
    assert len(artifact_events) == 1, (
        f"expected exactly one artifact event, got {len(artifact_events)}"
    )
    artifact: Artifact = artifact_events[0].artifact
    assert artifact.parts, "artifact has no parts"
    part = artifact.parts[0]
    actual = part.root if hasattr(part, "root") else part
    assert isinstance(actual, DataPart)
    return actual.data


def _agent(agent_id: str, name: str = "Agent") -> Agent:
    return Agent(agent_id=agent_id, name=name, endpoint=f"http://{agent_id}", tags=["demo"])


@pytest.mark.asyncio
async def test_discovery_marks_alive_agents_as_online():
    """When an agent's id is in the ``filter_alive`` result set, its
    discovery entry must carry ``status="online"``."""
    agents = [_agent("a"), _agent("b")]
    executor = _make_executor(search_result=agents, alive_ids={"a", "b"})
    eq = _make_event_queue()

    await executor._handle_discovery(_discovery_message(), _make_context(), eq)

    data = _artifact_data(eq)
    assert {entry["agent_id"]: entry["status"] for entry in data["agents"]} == {
        "a": "online",
        "b": "online",
    }
    assert data["total"] == 2


@pytest.mark.asyncio
async def test_discovery_marks_absent_agents_as_offline():
    """When an agent's id is NOT in the ``filter_alive`` result, its
    discovery entry must carry ``status="offline"`` — never the stale
    ``agent.status`` value (which the entity no longer carries)."""
    agents = [_agent("a"), _agent("b")]
    executor = _make_executor(search_result=agents, alive_ids=set())
    eq = _make_event_queue()

    await executor._handle_discovery(_discovery_message(), _make_context(), eq)

    data = _artifact_data(eq)
    assert {entry["agent_id"]: entry["status"] for entry in data["agents"]} == {
        "a": "offline",
        "b": "offline",
    }


@pytest.mark.asyncio
async def test_discovery_mixed_alive_state_projects_per_agent():
    """The alive projection is per-agent, not all-or-nothing — pin
    this so a future "batch coarsening" refactor can't collapse the
    contract without breaking a test."""
    agents = [_agent("alive-1"), _agent("dead"), _agent("alive-2")]
    executor = _make_executor(search_result=agents, alive_ids={"alive-1", "alive-2"})
    eq = _make_event_queue()

    await executor._handle_discovery(_discovery_message(), _make_context(), eq)

    data = _artifact_data(eq)
    statuses = {entry["agent_id"]: entry["status"] for entry in data["agents"]}
    assert statuses == {
        "alive-1": "online",
        "dead": "offline",
        "alive-2": "online",
    }


@pytest.mark.asyncio
async def test_discovery_calls_filter_alive_once_with_full_id_list():
    """Liveness lookup must be a single batch call against the entire
    discovered id list, not N individual ``is_alive`` round-trips.

    This pins the perf contract: discovery returning M agents should
    cost O(M) on the agent search and O(1) extra Redis round-trip
    on the liveness projection."""
    agents = [_agent(f"a{i}") for i in range(5)]
    executor = _make_executor(search_result=agents, alive_ids=set())
    eq = _make_event_queue()

    await executor._handle_discovery(_discovery_message(), _make_context(), eq)

    executor.agent_service.repository.filter_alive.assert_awaited_once_with(
        [f"a{i}" for i in range(5)]
    )
