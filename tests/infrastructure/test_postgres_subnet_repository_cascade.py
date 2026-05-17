"""PostgresSubnetRepository — ``delete_with_children`` single-transaction
cascade (ADR-0003 §A.4, issue #54).

Three contracts pinned here:

1. **Happy path** — parent + children DELETE statements all run inside
   the same ``session.begin()`` context manager. The context manager's
   ``__aenter__`` / ``__aexit__`` are both invoked, no out-of-band
   ``session.commit()`` is needed (the ctx manager handles commit).
2. **Rollback path** — a failure in the middle of the cascade propagates
   out of ``delete_with_children`` and SQLAlchemy's ``session.begin()``
   exits with exception info (the contract that triggers PG ROLLBACK in
   a real session). No explicit ``commit()`` is invoked.
3. **Empty children + missing parent** — the cascade still runs the
   parent DELETE; ``rowcount == 0`` surfaces as ``False`` (idempotent
   "parent already gone" path used by cascade retries).

The repository is exercised against an in-memory mock session — no
PostgreSQL needed. Real transactional semantics are guaranteed by
SQLAlchemy itself once the ``session.begin()`` idiom is in place; this
test pins that the idiom is actually being used.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.infrastructure.persistence.postgres.subnet_repository import (
    PostgresSubnetRepository,
)


def _make_session_factory(execute_results: list | None = None):
    """Build a mock ``async_sessionmaker`` whose session exposes a
    ``begin()`` async context manager and a recordable ``execute``.

    The shape mirrors the real ``async_sessionmaker → AsyncSession →
    AsyncSessionTransaction`` chain that ``delete_with_children`` relies
    on. ``execute_results`` lets a caller queue scripted return values
    or exceptions for ``session.execute(...)`` — used by the rollback
    test to inject a mid-cascade failure.
    """
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None

    # ``session.begin()`` is a sync call returning an async ctx manager
    # (SessionTransaction). On clean exit it commits; on exception it
    # rolls back. We mock the ctx manager and assert the right hooks
    # were invoked.
    begin_ctx = AsyncMock()
    begin_ctx.__aenter__.return_value = begin_ctx
    begin_ctx.__aexit__.return_value = None
    session.begin = MagicMock(return_value=begin_ctx)

    if execute_results is not None:
        session.execute.side_effect = execute_results

    factory = MagicMock(return_value=session)
    return factory, session, begin_ctx


def _ok_result(rowcount: int = 1):
    """Build a mock CursorResult with the given rowcount."""
    result = MagicMock()
    result.rowcount = rowcount
    return result


# ---------------------------------------------------------------------------
# 1. Happy path — parent + children deleted in one begin() block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_with_children_happy_path_uses_single_begin_block():
    """All N+1 deletes run under the SAME ``session.begin()`` context."""
    # Three children + one parent → 4 execute results.
    execute_results = [_ok_result(1) for _ in range(4)]
    factory, session, begin_ctx = _make_session_factory(execute_results)
    repo = PostgresSubnetRepository(session_factory=factory)

    ok = await repo.delete_with_children(
        "parent-1", ["child-1", "child-2", "child-3"]
    )

    assert ok is True
    # ``session.begin()`` is the transactional seam — it must be called
    # exactly once (not once per child).
    session.begin.assert_called_once()
    # The transaction context manager enters and exits cleanly.
    begin_ctx.__aenter__.assert_awaited_once()
    begin_ctx.__aexit__.assert_awaited_once()
    # The exit was with no exception (``__aexit__(None, None, None)``)
    # — SQLAlchemy treats this as "commit".
    exit_call = begin_ctx.__aexit__.await_args
    assert exit_call.args[:3] == (None, None, None)
    # Three child DELETEs + one parent DELETE.
    assert session.execute.await_count == 4


@pytest.mark.asyncio
async def test_delete_with_children_empty_list_runs_parent_only():
    """``child_ids=[]`` collapses to a single parent DELETE — used when
    a top-level subnet has no children."""
    factory, session, _ = _make_session_factory(
        execute_results=[_ok_result(1)]
    )
    repo = PostgresSubnetRepository(session_factory=factory)

    ok = await repo.delete_with_children("parent-1", [])

    assert ok is True
    assert session.execute.await_count == 1
    session.begin.assert_called_once()


@pytest.mark.asyncio
async def test_delete_with_children_parent_missing_returns_false():
    """``rowcount == 0`` on the parent DELETE surfaces as ``False`` —
    children, if any, were still deleted in the same transaction
    (idempotent retry path)."""
    factory, _, _ = _make_session_factory(
        execute_results=[_ok_result(1), _ok_result(0)]
    )
    repo = PostgresSubnetRepository(session_factory=factory)

    ok = await repo.delete_with_children("parent-gone", ["child-1"])

    assert ok is False


# ---------------------------------------------------------------------------
# 2. Rollback path — mid-cascade failure aborts the whole transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_with_children_mid_cascade_error_aborts_inside_begin():
    """When the second child DELETE raises, the exception propagates
    out of ``delete_with_children`` and SQLAlchemy's transaction
    context manager sees the exception in ``__aexit__`` — which is the
    real-PG rollback trigger. We pin both the propagation and the
    "exit saw exception" invariants here."""
    # First child OK, second child boom, parent should never be reached.
    boom = RuntimeError("simulated PG failure on second child")
    factory, session, begin_ctx = _make_session_factory(
        execute_results=[_ok_result(1), boom, _ok_result(1)]
    )
    repo = PostgresSubnetRepository(session_factory=factory)

    with pytest.raises(RuntimeError, match="simulated PG failure"):
        await repo.delete_with_children(
            "parent-1", ["child-1", "child-2", "child-3"]
        )

    # Only the first two execute calls fired — third child + parent
    # never got their turn because the exception bubbled.
    assert session.execute.await_count == 2
    # The transaction ctx manager's exit saw the exception — this is
    # the seam that SQLAlchemy turns into ROLLBACK in a real session.
    begin_ctx.__aexit__.assert_awaited_once()
    exit_args = begin_ctx.__aexit__.await_args.args
    assert exit_args[0] is RuntimeError or isinstance(
        exit_args[1], RuntimeError
    )


@pytest.mark.asyncio
async def test_delete_with_children_does_not_call_commit_directly():
    """``async with session.begin()`` owns commit — we must NOT also
    call ``session.commit()`` ourselves, otherwise on a real PG session
    the explicit commit would race the context manager's commit and
    raise ``InvalidRequestError``.
    """
    factory, session, _ = _make_session_factory(
        execute_results=[_ok_result(1), _ok_result(1)]
    )
    repo = PostgresSubnetRepository(session_factory=factory)

    await repo.delete_with_children("parent-1", ["child-1"])

    # No explicit commit on the session — the begin() ctx handles it.
    session.commit.assert_not_called()
