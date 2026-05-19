"""Regression tests for P1-7: RedisAgentRepository.delete must also purge
the ERC-8004 reverse index and the alive signal key.

Before the fix:
- `acn:agents:by_erc8004_id:{token_id}` was a plain SET with no TTL; deleting
  an agent left it in place, permanently blocking re-binding that token_id
  to a replacement agent (save() checks for duplicate bind).
- `acn:agents:{id}:alive` had a 90s TTL so it would eventually evaporate on
  its own, but for 90 seconds after deletion `filter_alive()` could still
  "see" the dead agent — letting it surface in ``search_agents(status='online')``
  even though the underlying row was already gone.
"""

from datetime import datetime
from unittest.mock import AsyncMock, call

import pytest

from acn.core.entities.agent import Agent
from acn.infrastructure.persistence.redis.agent_repository import (
    RedisAgentRepository,
)


def _make_agent(**overrides) -> Agent:
    defaults: dict = {
        "agent_id": "agent-001",
        "owner": "user-001",
        "name": "bot",
        "endpoint": "https://bot.example.com",
        "tags": [],
        "subnet_ids": ["public"],
        "registered_at": datetime(2026, 1, 1),
    }
    defaults.update(overrides)
    return Agent(**defaults)


@pytest.mark.asyncio
async def test_delete_purges_alive_key():
    fake_redis = AsyncMock()
    repo = RedisAgentRepository(redis_client=fake_redis)
    repo.find_by_id = AsyncMock(return_value=_make_agent())  # type: ignore[method-assign]

    assert await repo.delete("agent-001") is True

    assert (
        call("acn:agents:agent-001:alive")
        in fake_redis.delete.await_args_list
    )


@pytest.mark.asyncio
async def test_delete_purges_erc8004_reverse_index_when_bound():
    fake_redis = AsyncMock()
    repo = RedisAgentRepository(redis_client=fake_redis)
    repo.find_by_id = AsyncMock(  # type: ignore[method-assign]
        return_value=_make_agent(erc8004_agent_id="42", erc8004_chain="ethereum")
    )

    assert await repo.delete("agent-001") is True

    assert (
        call("acn:agents:by_erc8004_id:42")
        in fake_redis.delete.await_args_list
    )


@pytest.mark.asyncio
async def test_delete_does_not_touch_erc8004_index_when_unbound():
    """Agents without an on-chain binding must not issue a DEL for the
    (possibly shared) reverse-index namespace.

    Reverse-index keys are keyed by token_id, not agent_id; issuing a
    DEL based on a missing/empty erc8004_agent_id could stomp on
    `acn:agents:by_erc8004_id:` (the empty-string key) or raise.
    """
    fake_redis = AsyncMock()
    repo = RedisAgentRepository(redis_client=fake_redis)
    repo.find_by_id = AsyncMock(return_value=_make_agent())  # type: ignore[method-assign]

    await repo.delete("agent-001")

    bad_keys = [
        c.args[0]
        for c in fake_redis.delete.await_args_list
        if c.args and isinstance(c.args[0], str) and c.args[0].startswith(
            "acn:agents:by_erc8004_id"
        )
    ]
    assert bad_keys == [], (
        f"delete() must not DEL any by_erc8004_id key when unbound, got: {bad_keys}"
    )
