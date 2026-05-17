"""RedisSubnetRepository — ``delete_with_children`` sequential cascade
(ADR-0003 §A.4 Redis branch, issue #54).

Redis has no cross-method MULTI/EXEC, so the contract is best-effort
with a clear breadcrumb on partial failure. Pinned here:

1. Happy path — each child deleted in order, then the parent.
2. Partial failure — second child's ``delete()`` returns ``False`` →
   raises ``RuntimeError`` *before* touching the parent + emits a
   ``delete_with_children_partial`` warning log.
3. Idempotent parent-already-gone — children all deleted but parent
   was already missing → returns ``False`` (callers treat as "cascade
   already raced through, nothing to retry").

Uses ``monkeypatch`` to swap the repository's own ``delete()`` method
with a scripted ``AsyncMock``. The contract under test is exactly "the
cascade delegates to ``self.delete`` in order and short-circuits on
falsy returns", so a method-level mock is more honest than a full
Redis-client mock that would muddy the assertion.
"""

import logging
from unittest.mock import AsyncMock

import pytest

from acn.infrastructure.persistence.redis.subnet_repository import (
    RedisSubnetRepository,
)


def _make_repo() -> RedisSubnetRepository:
    """A repository whose Redis client is irrelevant — every test
    patches ``self.delete`` with a scripted mock."""
    # AsyncMock as the redis client is fine: it's never reached when
    # ``self.delete`` is monkeypatched.
    return RedisSubnetRepository(redis_client=AsyncMock())


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_with_children_happy_path_sequential_order(monkeypatch):
    repo = _make_repo()
    delete_mock = AsyncMock(side_effect=[True, True, True, True])
    monkeypatch.setattr(repo, "delete", delete_mock)

    ok = await repo.delete_with_children(
        "parent-1", ["child-1", "child-2", "child-3"]
    )

    assert ok is True
    # 3 children + 1 parent in that exact order — parent is last.
    delete_call_args = [c.args[0] for c in delete_mock.await_args_list]
    assert delete_call_args == ["child-1", "child-2", "child-3", "parent-1"]


@pytest.mark.asyncio
async def test_delete_with_children_empty_list_calls_parent_once(monkeypatch):
    repo = _make_repo()
    delete_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(repo, "delete", delete_mock)

    ok = await repo.delete_with_children("parent-1", [])

    assert ok is True
    delete_mock.assert_awaited_once_with("parent-1")


# ---------------------------------------------------------------------------
# 2. Partial-failure breadcrumb path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_with_children_child_failure_raises_and_skips_parent(
    monkeypatch, caplog
):
    """Second child delete returns ``False`` → ``RuntimeError`` and the
    parent delete is NEVER attempted. Operator-visible breadcrumb is
    emitted at ``warning`` level with stable structured fields."""
    repo = _make_repo()
    # child-1 OK, child-2 ghost (returns False), child-3 + parent must
    # never be called.
    delete_mock = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(repo, "delete", delete_mock)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="child-2"):
            await repo.delete_with_children(
                "parent-1", ["child-1", "child-2", "child-3"]
            )

    # Only two deletes attempted — child-3 and parent untouched.
    assert delete_mock.await_count == 2
    delete_call_args = [c.args[0] for c in delete_mock.await_args_list]
    assert delete_call_args == ["child-1", "child-2"]

    # Breadcrumb log present + carries the structured fields ops need
    # to locate the orphan ("parent X, child Y, reason").
    partial_records = [
        r
        for r in caplog.records
        if r.message == "delete_with_children_partial"
    ]
    assert len(partial_records) == 1, (
        f"expected exactly one breadcrumb log, got {len(partial_records)} "
        f"in {[r.message for r in caplog.records]!r}"
    )
    record = partial_records[0]
    assert record.levelname == "WARNING"
    assert getattr(record, "parent_subnet_id", None) == "parent-1"
    assert getattr(record, "child_subnet_id", None) == "child-2"
    assert getattr(record, "reason", None) == "child_delete_returned_false"


@pytest.mark.asyncio
async def test_delete_with_children_first_child_failure_skips_everything(
    monkeypatch, caplog
):
    """Failure on the FIRST child — only one delete fires and the
    parent is preserved."""
    repo = _make_repo()
    delete_mock = AsyncMock(side_effect=[False])
    monkeypatch.setattr(repo, "delete", delete_mock)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="child-1"):
            await repo.delete_with_children(
                "parent-1", ["child-1", "child-2"]
            )

    assert delete_mock.await_count == 1
    assert any(
        r.message == "delete_with_children_partial" for r in caplog.records
    )


# ---------------------------------------------------------------------------
# 3. Idempotent — parent already gone after a successful child sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_with_children_parent_already_gone_returns_false(
    monkeypatch,
):
    """All children deleted, but parent was racing-deleted by another
    caller → parent ``delete()`` returns ``False``. We surface that
    truthy/falsy value so cascade retries can decide "already done"."""
    repo = _make_repo()
    delete_mock = AsyncMock(side_effect=[True, True, False])
    monkeypatch.setattr(repo, "delete", delete_mock)

    ok = await repo.delete_with_children(
        "parent-ghost", ["child-1", "child-2"]
    )

    assert ok is False
    delete_call_args = [c.args[0] for c in delete_mock.await_args_list]
    assert delete_call_args == ["child-1", "child-2", "parent-ghost"]
