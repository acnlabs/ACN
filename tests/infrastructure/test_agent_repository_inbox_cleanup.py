"""Regression test: deleting an agent must also purge its offline inbox."""

from unittest.mock import AsyncMock, call

import pytest

from acn.infrastructure.persistence.redis.agent_repository import RedisAgentRepository


@pytest.mark.asyncio
async def test_delete_agent_purges_inbox(sample_agent):
    """When an agent is deleted, acn:inbox:{agent_id} must be DELed.

    Otherwise a deleted agent would leak up to ~50 messages worth of Redis
    memory per inbox until the 30-day TTL expired.
    """
    fake_redis = AsyncMock()
    repo = RedisAgentRepository(redis_client=fake_redis)

    # find_by_id() is called by delete(); stub it to return our sample agent
    repo.find_by_id = AsyncMock(return_value=sample_agent)  # type: ignore[method-assign]

    result = await repo.delete(sample_agent.agent_id)

    assert result is True

    expected_inbox_delete = call(f"acn:inbox:{sample_agent.agent_id}")
    assert expected_inbox_delete in fake_redis.delete.await_args_list, (
        "delete(agent_id) must call redis.delete on acn:inbox:{agent_id}"
    )
