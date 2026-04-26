"""Repository-layer tests for ``compare_and_save`` (security audit H3).

We exercise the *Redis* implementation here because its CAS is two-phase
(Lua flips ``status``, then a regular ``save`` rewrites everything else),
and the contract that "no full save runs unless the Lua CAS won" is
worth pinning down explicitly. The PostgreSQL implementation does the
CAS in a single ``UPDATE ... WHERE status=?`` statement, which is
covered indirectly by ``tests/services/test_task_service_h3_concurrency.py``
through the service-layer contract — running real PG here would require
infra we don't have in unit tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from acn.core.entities.task import Task, TaskStatus
from acn.infrastructure.persistence.redis.task_repository import (
    RedisTaskRepository,
)


def _make_task() -> Task:
    return Task(
        task_id="t-cas",
        creator_type="human",
        creator_id="creator-1",
        creator_name="Creator",
        title="cas",
        description="cas test",
        reward="0",
        reward_currency="ap_points",
        max_participants=1,
        status=TaskStatus.COMPLETED,  # the *new* status the caller wants
    )


@pytest.fixture
def redis_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def repo(redis_client: AsyncMock) -> RedisTaskRepository:
    return RedisTaskRepository(redis_client)


class TestRedisCompareAndSave:
    async def test_winning_cas_then_runs_full_save(
        self, repo: RedisTaskRepository, redis_client: AsyncMock
    ) -> None:
        """CAS Lua returns 1 → caller's full ``save`` proceeds."""
        cas_script = AsyncMock(return_value=1)
        repo._cas_status_script = cas_script  # bypass register_script call

        with patch.object(repo, "save", new=AsyncMock()) as save_mock:
            won = await repo.compare_and_save(
                _make_task(), expected_status=TaskStatus.SUBMITTED
            )

        assert won is True
        cas_script.assert_awaited_once()
        # Lua got the right (expected, new) tuple — order matters for the script body
        assert cas_script.await_args.kwargs["args"] == [
            TaskStatus.SUBMITTED.value,
            TaskStatus.COMPLETED.value,
        ]
        save_mock.assert_awaited_once()

    async def test_losing_cas_short_circuits_before_save(
        self, repo: RedisTaskRepository
    ) -> None:
        """CAS Lua returns 0 → ``save`` MUST NOT run.

        This is the security-critical assertion: a losing concurrent caller
        must not overwrite the winner's row, otherwise the indexes (open /
        by_status) drift back to a pre-transition state and downstream
        listings expose a task that was already paid out as still pending.
        """
        cas_script = AsyncMock(return_value=0)
        repo._cas_status_script = cas_script

        with patch.object(repo, "save", new=AsyncMock()) as save_mock:
            won = await repo.compare_and_save(
                _make_task(), expected_status=TaskStatus.SUBMITTED
            )

        assert won is False
        save_mock.assert_not_awaited()

    async def test_lua_script_targets_the_correct_task_key(
        self, repo: RedisTaskRepository
    ) -> None:
        """Sanity: CAS keys argument must be ``acn:task:{task_id}``,
        otherwise we'd be CAS-ing the wrong row entirely."""
        cas_script = AsyncMock(return_value=1)
        repo._cas_status_script = cas_script

        with patch.object(repo, "save", new=AsyncMock()):
            await repo.compare_and_save(
                _make_task(), expected_status=TaskStatus.SUBMITTED
            )

        assert cas_script.await_args.kwargs["keys"] == ["acn:task:t-cas"]
