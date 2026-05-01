"""Redis follow repository tests.

These tests pin down the wire-level Redis call sequence so future
refactors of ``RedisFollowRepository`` cannot silently change the
storage contract documented in
``docs/features/acn-follow-proposal.md``:

  ZSET acn:follows:{follower}    member=followee  score=created_at
  ZSET acn:followers:{followee}  member=follower   score=created_at

We use a lightweight in-process pipeline mock instead of ``fakeredis``
because the assertions we care about are operation-level (which keys
were touched, in which order, on which structure type), not full ZSET
semantics — and the rest of the test suite already follows this
convention with ``AsyncMock``.
"""

from unittest.mock import AsyncMock

import pytest

from acn.infrastructure.persistence.redis.follow_repository import (
    RedisFollowRepository,
)


def _pipeline_recorder(execute_returns: list | None = None):
    """Return ``(pipe_mock, recorded_calls)`` for assertions.

    ``recorded_calls`` is a list of ``(method_name, args, kwargs)``
    tuples in the order the repository invoked them.
    """
    recorded: list[tuple[str, tuple, dict]] = []

    class _PipeProxy:
        def __init__(self):
            pass

        def _record(self, name: str):
            def _impl(*args, **kwargs):
                recorded.append((name, args, kwargs))
                return self

            return _impl

        def __getattr__(self, name):
            # Any pipeline method returns the proxy itself (chaining).
            return self._record(name)

        async def execute(self):
            return execute_returns if execute_returns is not None else []

    return _PipeProxy(), recorded


@pytest.mark.asyncio
async def test_add_writes_both_indexes_with_same_score():
    fake_redis = AsyncMock()
    pipe, calls = _pipeline_recorder(execute_returns=[1, 1])
    fake_redis.pipeline = lambda: pipe

    repo = RedisFollowRepository(fake_redis)
    created = await repo.add("a", "b")

    assert created is True

    # Two ZADDs, on the two ZSET keys, with identical timestamp scores
    # so the dual-index stays consistent on creation-time ordering.
    zadd_calls = [c for c in calls if c[0] == "zadd"]
    assert len(zadd_calls) == 2
    keys = [c[1][0] for c in zadd_calls]
    assert "acn:follows:a" in keys
    assert "acn:followers:b" in keys
    score_a = list(zadd_calls[0][1][1].values())[0]
    score_b = list(zadd_calls[1][1][1].values())[0]
    assert score_a == score_b


@pytest.mark.asyncio
async def test_add_uses_nx_to_preserve_original_timestamp():
    """Repeat-follow MUST NOT bump the score back to "now".

    Without ``nx=True``, a user accidentally double-clicking the
    follow button would re-stamp the edge timestamp and reorder it to
    the top of "most recently followed" — silently distorting the feed.
    NX means ZADD only writes if the member is absent; existing
    ``created_at`` scores are preserved.
    """
    fake_redis = AsyncMock()
    pipe, calls = _pipeline_recorder(execute_returns=[0, 0])
    fake_redis.pipeline = lambda: pipe

    repo = RedisFollowRepository(fake_redis)
    await repo.add("a", "b")

    zadd_calls = [c for c in calls if c[0] == "zadd"]
    assert len(zadd_calls) == 2
    for name, _args, kwargs in zadd_calls:
        assert kwargs.get("nx") is True, (
            f"ZADD on {name} must use nx=True to keep follow ordering "
            f"intent-faithful; got kwargs={kwargs}"
        )


@pytest.mark.asyncio
async def test_add_returns_false_for_repeat():
    fake_redis = AsyncMock()
    # First entry of execute() return value drives the "newly created?"
    # signal — a 0 means the member already existed in the follows ZSET.
    pipe, _ = _pipeline_recorder(execute_returns=[0, 0])
    fake_redis.pipeline = lambda: pipe

    repo = RedisFollowRepository(fake_redis)
    created = await repo.add("a", "b")

    assert created is False, "repeat-add must signal idempotent path"


@pytest.mark.asyncio
async def test_remove_zrems_both_indexes():
    fake_redis = AsyncMock()
    pipe, calls = _pipeline_recorder(execute_returns=[1, 1])
    fake_redis.pipeline = lambda: pipe

    repo = RedisFollowRepository(fake_redis)
    removed = await repo.remove("a", "b")

    assert removed is True
    zrem_keys = [c[1][0] for c in calls if c[0] == "zrem"]
    assert "acn:follows:a" in zrem_keys
    assert "acn:followers:b" in zrem_keys


@pytest.mark.asyncio
async def test_is_following_reads_zscore():
    fake_redis = AsyncMock()
    fake_redis.zscore.return_value = 1700_000_000.0  # any non-None

    repo = RedisFollowRepository(fake_redis)
    assert await repo.is_following("a", "b") is True
    fake_redis.zscore.assert_awaited_once_with("acn:follows:a", "b")


@pytest.mark.asyncio
async def test_is_following_returns_false_when_absent():
    fake_redis = AsyncMock()
    fake_redis.zscore.return_value = None

    repo = RedisFollowRepository(fake_redis)
    assert await repo.is_following("a", "b") is False


@pytest.mark.asyncio
async def test_list_following_uses_zrevrange_for_recency_first():
    fake_redis = AsyncMock()
    fake_redis.zrevrange.return_value = ["b", "c"]

    repo = RedisFollowRepository(fake_redis)
    out = await repo.list_following("a", limit=2, offset=0)

    assert out == ["b", "c"]
    # offset=0, end=offset+limit-1=1 → ZREVRANGE 0 1
    fake_redis.zrevrange.assert_awaited_once_with("acn:follows:a", 0, 1)


@pytest.mark.asyncio
async def test_count_follows_batch_pipelines_zcards():
    fake_redis = AsyncMock()
    pipe, calls = _pipeline_recorder(execute_returns=[1, 2, 3, 4])
    fake_redis.pipeline = lambda: pipe

    repo = RedisFollowRepository(fake_redis)
    out = await repo.count_follows_batch(["a", "b"])

    assert out == {"a": (1, 2), "b": (3, 4)}

    # Verify each agent gets BOTH zcard calls in the (follows, followers) order
    # expected by the unpack logic.
    zcard_keys = [c[1][0] for c in calls if c[0] == "zcard"]
    assert zcard_keys == [
        "acn:follows:a",
        "acn:followers:a",
        "acn:follows:b",
        "acn:followers:b",
    ]


@pytest.mark.asyncio
async def test_cleanup_agent_purges_reverse_pointers():
    fake_redis = AsyncMock()
    fake_redis.zrange.side_effect = [["b", "c"], ["x"]]  # following, followers
    pipe, calls = _pipeline_recorder()
    fake_redis.pipeline = lambda: pipe

    repo = RedisFollowRepository(fake_redis)
    await repo.cleanup_agent("a")

    zrem_pairs = [(c[1][0], c[1][1]) for c in calls if c[0] == "zrem"]
    # Reverse follower index forgets "a" — driven by followers list ["x"]
    assert ("acn:follows:x", "a") in zrem_pairs
    # Reverse following index forgets "a" — driven by following list ["b", "c"]
    assert ("acn:followers:b", "a") in zrem_pairs
    assert ("acn:followers:c", "a") in zrem_pairs

    # Then both own-side ZSETs are deleted
    delete_keys = [c[1][0] for c in calls if c[0] == "delete"]
    assert "acn:follows:a" in delete_keys
    assert "acn:followers:a" in delete_keys


@pytest.mark.asyncio
async def test_cleanup_agent_noop_when_no_edges():
    fake_redis = AsyncMock()
    fake_redis.zrange.side_effect = [[], []]
    fake_redis.pipeline = AsyncMock()

    repo = RedisFollowRepository(fake_redis)
    await repo.cleanup_agent("a")

    # No pipeline operations needed: skips pipeline construction entirely
    # so an unfollowed agent's deletion stays a single-RTT no-op.
    fake_redis.pipeline.assert_not_called()
