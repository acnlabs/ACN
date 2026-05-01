"""Unit tests for FollowService.

Covers the business rules from ``docs/features/acn-follow-proposal.md``:
  - Self-follow is rejected (400 surface).
  - Followee must exist (404 surface).
  - Per-agent ceiling of MAX_FOLLOWS (10 000) → 429 surface.
  - Idempotency on both follow and unfollow paths.
  - Cleanup on agent unregistration.
"""

import pytest

from acn.core.exceptions import AgentNotFoundException
from acn.services import (
    FollowLimitExceededError,
    FollowService,
    SelfFollowError,
)
from acn.services.follow_service import MAX_FOLLOWS


@pytest.mark.asyncio
async def test_follow_creates_new_edge(mock_follow_repository, mock_agent_repository):
    mock_agent_repository.exists.return_value = True
    mock_follow_repository.count_following.return_value = 0
    mock_follow_repository.is_following.return_value = False
    mock_follow_repository.add.return_value = True  # Newly created

    svc = FollowService(mock_follow_repository, mock_agent_repository)
    created = await svc.follow("a", "b")

    assert created is True
    mock_follow_repository.add.assert_awaited_once_with("a", "b")


@pytest.mark.asyncio
async def test_follow_repeat_is_idempotent(mock_follow_repository, mock_agent_repository):
    mock_agent_repository.exists.return_value = True
    mock_follow_repository.count_following.return_value = 5
    mock_follow_repository.is_following.return_value = True
    # Repository returns False because the edge already existed.
    mock_follow_repository.add.return_value = False

    svc = FollowService(mock_follow_repository, mock_agent_repository)
    created = await svc.follow("a", "b")

    assert created is False, "repeat-follow must NOT count as newly created"


@pytest.mark.asyncio
async def test_self_follow_rejected(mock_follow_repository, mock_agent_repository):
    svc = FollowService(mock_follow_repository, mock_agent_repository)

    with pytest.raises(SelfFollowError):
        await svc.follow("a", "a")

    # Existence check / repository write must be skipped.
    mock_agent_repository.exists.assert_not_awaited()
    mock_follow_repository.add.assert_not_awaited()


@pytest.mark.asyncio
async def test_follow_unknown_followee_raises(mock_follow_repository, mock_agent_repository):
    mock_agent_repository.exists.return_value = False

    svc = FollowService(mock_follow_repository, mock_agent_repository)
    with pytest.raises(AgentNotFoundException):
        await svc.follow("a", "ghost")

    mock_follow_repository.add.assert_not_awaited()


@pytest.mark.asyncio
async def test_follow_limit_blocks_new_edges(mock_follow_repository, mock_agent_repository):
    mock_agent_repository.exists.return_value = True
    mock_follow_repository.count_following.return_value = MAX_FOLLOWS
    mock_follow_repository.is_following.return_value = False  # would be a NEW edge

    svc = FollowService(mock_follow_repository, mock_agent_repository)
    with pytest.raises(FollowLimitExceededError):
        await svc.follow("a", "b")


@pytest.mark.asyncio
async def test_follow_limit_does_not_block_repeat_at_ceiling(
    mock_follow_repository, mock_agent_repository
):
    """At ceiling, *repeating* an existing follow must still succeed.

    Without this, a follower at exactly MAX_FOLLOWS could no longer
    re-issue any follow API call (e.g. on a reconnection retry) because
    every request would 429 — even ones that would not grow the index.
    """
    mock_agent_repository.exists.return_value = True
    mock_follow_repository.count_following.return_value = MAX_FOLLOWS
    mock_follow_repository.is_following.return_value = True  # already-following
    mock_follow_repository.add.return_value = False

    svc = FollowService(mock_follow_repository, mock_agent_repository)
    created = await svc.follow("a", "b")

    assert created is False


@pytest.mark.asyncio
async def test_unfollow_removes_edge(mock_follow_repository, mock_agent_repository):
    mock_follow_repository.remove.return_value = True

    svc = FollowService(mock_follow_repository, mock_agent_repository)
    removed = await svc.unfollow("a", "b")

    assert removed is True


@pytest.mark.asyncio
async def test_unfollow_repeat_is_idempotent(mock_follow_repository, mock_agent_repository):
    mock_follow_repository.remove.return_value = False  # nothing to remove

    svc = FollowService(mock_follow_repository, mock_agent_repository)
    removed = await svc.unfollow("a", "b")

    assert removed is False


@pytest.mark.asyncio
async def test_get_counts_returns_pair(mock_follow_repository, mock_agent_repository):
    mock_follow_repository.count_following.return_value = 7
    mock_follow_repository.count_followers.return_value = 12

    svc = FollowService(mock_follow_repository, mock_agent_repository)
    following, followers = await svc.get_counts("a")

    assert (following, followers) == (7, 12)


@pytest.mark.asyncio
async def test_get_counts_batch_passthrough(mock_follow_repository, mock_agent_repository):
    mock_follow_repository.count_follows_batch.return_value = {
        "a": (1, 2),
        "b": (3, 4),
    }

    svc = FollowService(mock_follow_repository, mock_agent_repository)
    counts = await svc.get_counts_batch(["a", "b"])

    assert counts == {"a": (1, 2), "b": (3, 4)}
    mock_follow_repository.count_follows_batch.assert_awaited_once_with(["a", "b"])


@pytest.mark.asyncio
async def test_cleanup_agent_delegates_to_repo(mock_follow_repository, mock_agent_repository):
    svc = FollowService(mock_follow_repository, mock_agent_repository)
    await svc.cleanup_agent("a")

    mock_follow_repository.cleanup_agent.assert_awaited_once_with("a")
