"""Inbound reachability is decoupled from ``alive``.

``alive`` (the Redis TTL key) conflates *outbound* liveness — heartbeats and
authenticated calls the agent makes — with *inbound* deliverability. An agent
can keep its ``alive`` key fresh by polling ACN while its own inbound A2A
endpoint is wedged/unreachable (the AgentMother production incident).

These tests cover the dedicated inbound-reachability record, which is written
ONLY from real direct-push outcomes (``MessageRouter.route()``) and summarized
into a tri-state ``reachable``:
  - True  → a recent push succeeded
  - False → consecutive pushes are failing (endpoint consistently down)
  - None  → unknown: never pushed, or last success aged out without failures
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fakeredis import aioredis as fakeredis_async

from acn.infrastructure.persistence.redis.agent_repository import RedisAgentRepository
from acn.services import AgentService
from acn.services.agent_service import INBOUND_UNREACHABLE_FAILS


@pytest.fixture
async def service() -> AgentService:
    client = fakeredis_async.FakeRedis(decode_responses=False)
    await client.flushall()
    return AgentService(RedisAgentRepository(client))


@pytest.mark.asyncio
async def test_unknown_when_never_pushed(service: AgentService) -> None:
    health = await service.get_inbound_health("agent-never")
    assert health == {"reachable": None}


@pytest.mark.asyncio
async def test_success_marks_reachable_and_resets_streak(service: AgentService) -> None:
    await service.record_inbound_delivery("a", ok=True, probe_ms=12.3)
    health = await service.get_inbound_health("a")

    assert health["reachable"] is True
    assert health["last_ok_at"]  # stamped
    assert health["consec_fail"] == 0
    assert health["last_probe_ms"] == pytest.approx(12.3)


@pytest.mark.asyncio
async def test_failures_accumulate_then_flip_unreachable(service: AgentService) -> None:
    # One/two failures alone must NOT flip reachable to False (blip tolerance).
    for _ in range(INBOUND_UNREACHABLE_FAILS - 1):
        await service.record_inbound_delivery(
            "a", ok=False, probe_ms=5000.0, error="ConnectTimeout"
        )
    health = await service.get_inbound_health("a")
    assert health["consec_fail"] == INBOUND_UNREACHABLE_FAILS - 1
    assert health["reachable"] is None  # not yet enough to declare dead
    assert health["last_error"] == "ConnectTimeout"

    # Reaching the threshold flips it.
    await service.record_inbound_delivery("a", ok=False, error="ConnectTimeout")
    health = await service.get_inbound_health("a")
    assert health["consec_fail"] == INBOUND_UNREACHABLE_FAILS
    assert health["reachable"] is False


@pytest.mark.asyncio
async def test_success_after_failures_resets(service: AgentService) -> None:
    for _ in range(INBOUND_UNREACHABLE_FAILS + 2):
        await service.record_inbound_delivery("a", ok=False, error="boom")
    assert (await service.get_inbound_health("a"))["reachable"] is False

    await service.record_inbound_delivery("a", ok=True, probe_ms=8.0)
    health = await service.get_inbound_health("a")
    assert health["reachable"] is True
    assert health["consec_fail"] == 0


@pytest.mark.asyncio
async def test_stale_success_without_failures_is_unknown(service: AgentService) -> None:
    """A success older than the reachability window decays to None, not True."""
    await service.record_inbound_delivery("a", ok=True)
    # Backdate last_ok_at far beyond INBOUND_REACHABLE_WINDOW directly in Redis.
    repo = service.repository
    await repo.redis.hset(  # type: ignore[attr-defined]
        "acn:agents:a:inbound", "last_ok_at", "2000-01-01T00:00:00+00:00"
    )

    health = await service.get_inbound_health("a")
    assert health["reachable"] is None
    assert health["consec_fail"] == 0


@pytest.mark.asyncio
async def test_record_is_best_effort_and_swallows_errors() -> None:
    """A Redis blip while recording must never propagate to the caller."""
    repo = AsyncMock()
    repo.record_inbound_delivery.side_effect = RuntimeError("redis down")
    service = AgentService(repo)

    # Must not raise.
    await service.record_inbound_delivery("a", ok=True, probe_ms=1.0)
    repo.record_inbound_delivery.assert_awaited_once()
