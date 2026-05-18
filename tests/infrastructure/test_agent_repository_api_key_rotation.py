"""Regression: RedisAgentRepository.save must drop the stale by_api_key
index entry whenever an agent's API-key hash changes (H1 rotation).

Bug origin
----------
Before the fix, ``save()`` *added* an index entry under the new hash but
left the old hash's entry in place. The result: after rotation, the
gateway's ``get_agent_by_api_key(old_key)`` lookup still hit the old
index entry and resolved the old key to the same agent_id, so the
rotated-away key kept working indefinitely (until the agent was deleted
or some other mutation happened to invalidate the index for unrelated
reasons). That defeats the entire purpose of rotation and was caught
end-to-end by the v0.12.0 SDK smoke test.

What this file pins
-------------------
* **The happy path** (no key change): ``save()`` writes exactly one
  index entry and DOES NOT issue a ``DEL`` against any by_api_key key
  — we don't want to thrash the index on every heartbeat / status
  update.
* **The rotation path** (existing.api_key != new agent.api_key):
  ``save()`` writes the new index entry AND issues a single ``DEL``
  against the old hash's index key. The order is deferred — we don't
  care which happens first as long as the final state is "only the
  new key indexed".
"""

from __future__ import annotations

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
async def test_save_with_unchanged_api_key_does_not_purge_index():
    """Steady-state saves must not generate spurious DEL traffic.

    A first registration leaves only one ``by_api_key:`` index; a
    subsequent unchanged save (e.g. heartbeat-driven status update)
    must reuse the same key and NOT delete it. Anything else would be
    a silent index churn that defeats the in-memory cache and shows
    up as Redis load.
    """
    fake_redis = AsyncMock()
    repo = RedisAgentRepository(redis_client=fake_redis)

    agent = _make_agent(api_key="hash_v1")
    # Existing has the *same* api_key — pure no-op for the index.
    repo.find_by_id = AsyncMock(return_value=agent)  # type: ignore[method-assign]

    await repo.save(agent)

    deleted_index_keys = [
        c.args[0]
        for c in fake_redis.delete.await_args_list
        if c.args
        and isinstance(c.args[0], str)
        and c.args[0].startswith("acn:agents:by_api_key:")
    ]
    assert deleted_index_keys == [], (
        f"steady-state save must not DEL any by_api_key index, got: "
        f"{deleted_index_keys}"
    )
    # New index entry still gets written (idempotent SET is fine).
    assert (
        call("acn:agents:by_api_key:hash_v1", "agent-001")
        in fake_redis.set.await_args_list
    )


@pytest.mark.asyncio
async def test_save_with_rotated_api_key_purges_stale_index():
    """The H1 invariant: rotating the API key must drop the old index.

    Otherwise ``find_by_api_key(old_key_hash)`` keeps returning the
    same agent_id, the gateway accepts the rotated-away key, and the
    whole rotation is theatre.
    """
    fake_redis = AsyncMock()
    repo = RedisAgentRepository(redis_client=fake_redis)

    # Old state: hash_v1 indexed against the agent.
    existing = _make_agent(api_key="hash_v1")
    # New state after rotate_api_key(): same agent_id, fresh hash.
    new = _make_agent(api_key="hash_v2")
    repo.find_by_id = AsyncMock(return_value=existing)  # type: ignore[method-assign]

    await repo.save(new)

    # The new index entry was written.
    assert (
        call("acn:agents:by_api_key:hash_v2", "agent-001")
        in fake_redis.set.await_args_list
    )
    # The old index entry was deleted — this is the regression pin.
    assert (
        call("acn:agents:by_api_key:hash_v1")
        in fake_redis.delete.await_args_list
    ), (
        "save() must DEL the previous by_api_key index when the hash "
        "changes; otherwise rotated-away keys keep authenticating"
    )


@pytest.mark.asyncio
async def test_save_brand_new_agent_does_not_attempt_to_delete_anything():
    """First-time registration: existing is None, so there is no old
    index to clean up. We mustn't DEL the empty-string variant
    (``by_api_key:``), which would either no-op or, worse, clobber a
    shared namespace key in some Redis layouts."""
    fake_redis = AsyncMock()
    repo = RedisAgentRepository(redis_client=fake_redis)

    repo.find_by_id = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await repo.save(_make_agent(api_key="hash_fresh"))

    deleted_index_keys = [
        c.args[0]
        for c in fake_redis.delete.await_args_list
        if c.args
        and isinstance(c.args[0], str)
        and c.args[0].startswith("acn:agents:by_api_key:")
    ]
    assert deleted_index_keys == [], (
        f"first-time save must not DEL any by_api_key index, got: "
        f"{deleted_index_keys}"
    )
