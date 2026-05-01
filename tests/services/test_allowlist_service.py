"""Unit tests for AllowlistService (Phase 2 PR #2).

Pins the contract owners depend on:

* ``add`` is idempotent and returns ``False`` on the duplicate path.
* ``add`` blocks self-allowlist (400 surface) and unknown targets (404).
* ``add`` enforces the per-owner capacity ceiling.
* ``add`` clips ``reason`` defensively.
* ``remove`` is idempotent and follows the Redis-first ordering.
* ``add`` follows the PG-first ordering.
* Cache failures (Redis side) are best-effort: PG remains canonical
  and the route does not surface a 5xx for cache-side flakes.
* PG-only deployments (``redis_repo=None``) work — every method
  delegates to PG.

Repositories are mocked at the ``IAllowlistRepository`` level so
each branch can verify call ordering with ``MagicMock.method_calls``.
``AgentRepository`` is mocked just enough for the existence check.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.exceptions import AgentNotFoundException
from acn.core.interfaces import AllowlistEntry, IAgentRepository, IAllowlistRepository
from acn.services import (
    AllowlistCapacityExceededError,
    AllowlistService,
    SelfAllowlistError,
)
from acn.services.allowlist_service import MAX_ALLOWLIST_SIZE, MAX_REASON_LEN

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_repo() -> IAllowlistRepository:
    """Mock PG repository — the source-of-truth side."""
    repo = AsyncMock(spec=IAllowlistRepository)
    # Sensible defaults — individual tests override.
    repo.add.return_value = True
    repo.remove.return_value = True
    repo.is_member.return_value = False
    repo.count_for_owner.return_value = 0
    repo.list_targets.return_value = []
    return repo


@pytest.fixture
def redis_repo() -> IAllowlistRepository:
    """Mock Redis repository — the cache side."""
    repo = AsyncMock(spec=IAllowlistRepository)
    repo.add.return_value = True
    repo.remove.return_value = True
    return repo


@pytest.fixture
def agent_repo() -> IAgentRepository:
    """Mock agent repo, default to "every target exists"."""
    repo = AsyncMock(spec=IAgentRepository)
    repo.exists.return_value = True
    return repo


@pytest.fixture
def service(pg_repo, redis_repo, agent_repo) -> AllowlistService:
    return AllowlistService(
        pg_repo=pg_repo,
        redis_repo=redis_repo,
        agent_repository=agent_repo,
    )


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


async def test_add_writes_pg_first_then_redis(service, pg_repo, redis_repo):
    """The dual-write order is PG → Redis (see service docstring).

    A failure with PG-first ordering is recoverable: cache will pull
    the new member on next miss. The reverse order would create a
    safety hole — the cache could expose a "trusted" sender before PG
    actually committed the row.
    """
    # MagicMock's ``mock_calls`` covers cross-mock ordering when both
    # mocks share a parent. Using a parent mock to track call order
    # across instances.
    parent = MagicMock()
    parent.attach_mock(pg_repo.add, "pg_add")
    parent.attach_mock(redis_repo.add, "redis_add")

    created = await service.add("owner-1", "target-1", reason="trusted")

    assert created is True
    # Sequence: PG, then Redis.
    assert [c[0] for c in parent.mock_calls] == ["pg_add", "redis_add"]


async def test_add_self_allowlist_rejected(service, pg_repo):
    """Self-allowlisting is meaningless — sender == recipient never
    enters the policy gate. Reject early with a clear error so the
    route can map to 400."""
    with pytest.raises(SelfAllowlistError):
        await service.add("owner-1", "owner-1")
    # No PG write attempted.
    pg_repo.add.assert_not_called()


async def test_add_unknown_target_404s(service, agent_repo, pg_repo):
    """Mirrors FollowService: target_id must exist in agents.
    Owner can't enumerate other agents because this endpoint is
    owner-only auth, but we still 404 for clarity."""
    agent_repo.exists.return_value = False

    with pytest.raises(AgentNotFoundException):
        await service.add("owner-1", "ghost-2")
    pg_repo.add.assert_not_called()


async def test_add_at_capacity_blocks_new_target(service, pg_repo):
    """The 501-th distinct target must be rejected with 429 before
    any PG INSERT fires. Already-existing targets bypass the cap."""
    pg_repo.count_for_owner.return_value = MAX_ALLOWLIST_SIZE
    pg_repo.is_member.return_value = False  # new target

    with pytest.raises(AllowlistCapacityExceededError):
        await service.add("owner-1", "new-target")
    pg_repo.add.assert_not_called()


async def test_add_capacity_trigger_propagates_under_concurrency(
    service, pg_repo, redis_repo
):
    """TOCTOU race regression (PR #2 v3 P2-A5).

    The service-layer pre-check
    ``count_for_owner() < MAX_ALLOWLIST_SIZE`` happens in a separate
    round-trip from ``pg_repo.add``. With concurrent callers both
    observing ``count = 499`` (one slot remaining), both proceed past
    the service guard and both call into the PG repo. The PG trigger
    ``trg_agent_allowlist_capacity`` (alembic ``f6a7b8c9d0e1``) closes
    the hole at the database with ``pg_advisory_xact_lock`` per
    owner: the lock-holder INSERTs, the contender re-counts under
    the same lock, sees the cap is now breached, and ``RAISE``s
    SQLSTATE 23514 — which the Postgres repo translates into
    ``AllowlistCapacityExceededError``.

    Real PostgreSQL TOCTOU coverage lives in the staging verify
    script (``scripts/_tmp_verify_pr2_allowlist.py``) and was
    confirmed 6/6 against PG 14.19. This test pins the **service
    layer's contract** under that scenario without needing a real
    DB:

    1. Trigger-side ``AllowlistCapacityExceededError`` propagates
       cleanly to the caller (no rewrap, no swallow).
    2. The Redis dual-write does NOT fire when PG rejects — i.e.
       a rejected sender never makes it into the cache. (Otherwise
       the next ``is_member`` would falsely return ``True`` for the
       30s TTL window, exactly the security regression PR #2 closes.)
    3. Multiple concurrent callers settle into a deterministic mix
       of one success + N capacity errors, never any other exception
       type.
    """
    pg_repo.count_for_owner.return_value = MAX_ALLOWLIST_SIZE - 1
    pg_repo.is_member.return_value = False

    # Simulate the trigger: the first INSERT lands, every subsequent
    # one is rejected by the cap re-check inside the advisory-lock.
    call_counter = {"n": 0}

    async def add_side_effect(*_args, **_kwargs):
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            return True
        raise AllowlistCapacityExceededError(
            "agent_allowlist capacity exceeded for owner owner-1 (limit 500)"
        )

    pg_repo.add.side_effect = add_side_effect

    results = await asyncio.gather(
        *(service.add("owner-1", f"target-{i}") for i in range(5)),
        return_exceptions=True,
    )

    successes = [r for r in results if r is True]
    failures = [r for r in results if isinstance(r, AllowlistCapacityExceededError)]
    others = [
        r for r in results
        if r is not True and not isinstance(r, AllowlistCapacityExceededError)
    ]

    assert len(successes) == 1, f"expected 1 winner, got {successes!r}"
    assert len(failures) == 4, f"expected 4 capacity errors, got {failures!r}"
    assert others == [], f"unexpected exception types: {others!r}"

    # The 4 rejections never reach the cache layer — Redis must stay
    # consistent with PG's rejected state, otherwise we'd materialise
    # phantom trusted senders for the 30s TTL window.
    assert redis_repo.add.await_count == 1, (
        f"Redis must not be written for capacity-rejected adds; "
        f"got {redis_repo.add.await_count} cache writes"
    )


async def test_add_at_capacity_allows_idempotent_readd(service, pg_repo):
    """Already-allowlisted target re-add must succeed even AT
    capacity — capacity is on distinct entries, not on the API call.
    Otherwise a recipient at the cap would not be able to "touch" an
    existing entry (e.g. update its reason) without first removing
    something."""
    pg_repo.count_for_owner.return_value = MAX_ALLOWLIST_SIZE
    pg_repo.is_member.return_value = True
    pg_repo.add.return_value = False  # ON CONFLICT DO NOTHING

    created = await service.add("owner-1", "existing")

    assert created is False
    pg_repo.add.assert_awaited_once()


async def test_add_idempotent_returns_false(service, pg_repo):
    """Re-adding an existing edge succeeds with ``changed=False``
    semantics — the PG layer signals "no row inserted" and we
    propagate that."""
    pg_repo.is_member.return_value = True
    pg_repo.add.return_value = False

    created = await service.add("owner-1", "target-1")

    assert created is False


async def test_add_clips_reason_to_max_len(service, pg_repo):
    """Defence in depth: route layer also caps, but the service
    must not blindly trust its caller. Long reason gets clipped
    before persistence."""
    long_reason = "x" * (MAX_REASON_LEN + 50)

    await service.add("owner-1", "target-1", reason=long_reason)

    pg_repo.add.assert_awaited_once()
    forwarded_reason = pg_repo.add.call_args.kwargs["reason"]
    assert len(forwarded_reason) == MAX_REASON_LEN


async def test_add_redis_failure_is_best_effort(service, pg_repo, redis_repo):
    """Cache write failure must NOT propagate as an exception. PG
    is committed, the durable guarantee is met, the next is_member
    miss will rebuild the cache. The service logs but stays quiet."""
    redis_repo.add.side_effect = RuntimeError("Redis flake")

    # Must not raise.
    created = await service.add("owner-1", "target-1")

    assert created is True
    pg_repo.add.assert_awaited_once()


async def test_add_works_without_redis(pg_repo, agent_repo):
    """Rollout-opt-out path: ``redis_repo=None`` means PG-only.
    Every read/write delegates to PG; no exceptions."""
    svc = AllowlistService(
        pg_repo=pg_repo,
        redis_repo=None,
        agent_repository=agent_repo,
    )

    created = await svc.add("owner-1", "target-1")

    assert created is True
    pg_repo.add.assert_awaited_once()


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


async def test_remove_writes_redis_first_then_pg(service, pg_repo, redis_repo):
    """The dual-write order is INVERTED for remove (Redis → PG).

    Reasoning: with PG-first remove, a brief window exists where PG
    has dropped the member but cache still says "trusted" — letting
    a freshly-revoked sender keep delivering for up to 30s of TTL.
    Redis-first closes that hole; if PG fails, the next is_member
    miss will repopulate from PG and re-add the member, but at no
    point does the cache claim a removed sender is trusted.
    """
    parent = MagicMock()
    parent.attach_mock(redis_repo.remove, "redis_remove")
    parent.attach_mock(pg_repo.remove, "pg_remove")

    removed = await service.remove("owner-1", "target-1")

    assert removed is True
    assert [c[0] for c in parent.mock_calls] == ["redis_remove", "pg_remove"]


async def test_remove_idempotent_returns_false(service, pg_repo):
    """No-op remove succeeds with False — drives the route's
    repeat-DELETE → 200 idempotent contract."""
    pg_repo.remove.return_value = False

    removed = await service.remove("owner-1", "target-1")

    assert removed is False


async def test_remove_redis_failure_still_writes_pg(service, pg_repo, redis_repo):
    """A flaky Redis SREM must NOT block PG DELETE. The durable side
    must always be attempted so a retry from the client converges
    to the correct state on the canonical layer."""
    redis_repo.remove.side_effect = RuntimeError("Redis flake")

    removed = await service.remove("owner-1", "target-1")

    assert removed is True
    pg_repo.remove.assert_awaited_once()


# ---------------------------------------------------------------------------
# is_member
# ---------------------------------------------------------------------------


async def test_is_member_uses_redis_when_wired(service, redis_repo, pg_repo):
    """Hot path: with Redis wired, the call routes to the cache
    layer (which itself does cache → PG-fallback)."""
    redis_repo.is_member.return_value = True

    result = await service.is_member("owner-1", "target-1")

    assert result is True
    redis_repo.is_member.assert_awaited_once_with("owner-1", "target-1")
    pg_repo.is_member.assert_not_called()


async def test_is_member_falls_back_to_pg_without_redis(pg_repo, agent_repo):
    """Rollout-opt-out path: ``redis_repo=None`` ⇒ go straight to
    PG. Functional but slower — only acceptable for dev / test."""
    svc = AllowlistService(
        pg_repo=pg_repo,
        redis_repo=None,
        agent_repository=agent_repo,
    )
    pg_repo.is_member.return_value = True

    result = await svc.is_member("owner-1", "target-1")

    assert result is True
    pg_repo.is_member.assert_awaited_once()


async def test_is_member_propagates_repository_failure(service, redis_repo):
    """Failures must propagate so the policy service can apply its
    P0-3 fail-closed branch (divert to manifest). Swallowing here
    would convert a Redis outage into a silent inbox-bypass — the
    exact security regression we're guarding against."""
    redis_repo.is_member.side_effect = RuntimeError("Redis down")

    with pytest.raises(RuntimeError, match="Redis down"):
        await service.is_member("owner-1", "target-1")


# ---------------------------------------------------------------------------
# list / count
# ---------------------------------------------------------------------------


async def test_list_targets_always_uses_pg(service, pg_repo, redis_repo):
    """Listings always go through PG (cache lacks created_at /
    reason). Pin this so a refactor doesn't accidentally try the
    cache for the listing path."""
    pg_repo.list_targets.return_value = [
        AllowlistEntry(
            target_id="alice",
            created_at=datetime(2026, 4, 30, tzinfo=UTC),
            reason="trusted",
        )
    ]

    entries = await service.list_targets("owner-1", limit=50, offset=0)

    assert len(entries) == 1
    pg_repo.list_targets.assert_awaited_once_with("owner-1", limit=50, offset=0)
    redis_repo.list_targets.assert_not_called()


async def test_count_always_uses_pg(service, pg_repo):
    pg_repo.count_for_owner.return_value = 7

    assert await service.count("owner-1") == 7
    pg_repo.count_for_owner.assert_awaited_once_with("owner-1")
