"""Use real Redis Lua to verify conditional submission/withdrawal ordering."""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import redis.asyncio as redis

from acn.core.entities.task import Participation
from acn.infrastructure.persistence.redis.task_repository import RedisTaskRepository

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get("REDIS_URL"), reason="needs test Redis",
)]


@pytest.fixture
async def redis_repo():
    client = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    task_id, pid = "test-withdraw-" + uuid4().hex, "test-part-" + uuid4().hex
    repo = RedisTaskRepository(client)
    part = Participation(
        participation_id=pid, task_id=task_id, participant_id="test-solver",
        participant_name="Solver", joined_at=datetime.now(UTC),
    )
    keys = [f"acn:participation:{pid}", f"acn:task:{task_id}",
            f"acn:task:{task_id}:active_count"]
    try:
        await repo.save_participation(part)
        await client.set(keys[2], 1)
        yield repo, part
    finally:
        await client.delete(*keys)
        await client.aclose()


async def test_submission_wins_over_withdrawal(redis_repo):
    repo, part = redis_repo
    part.submit("delivered")
    await repo.save_participation(part, expected_status="active")
    with pytest.raises(ValueError, match="expected status"):
        await repo.atomic_cancel_participation(
            part.participation_id, part.task_id, expected_status="active",
        )
    stored = await repo.find_participation_by_id(part.participation_id)
    assert stored.status.value == "submitted"


async def test_withdrawal_wins_over_stale_submission(redis_repo):
    repo, part = redis_repo
    await repo.atomic_cancel_participation(
        part.participation_id, part.task_id, expected_status="active",
    )
    part.submit("stale delivery")
    with pytest.raises(ValueError, match="expected status"):
        await repo.save_participation(part, expected_status="active")
    stored = await repo.find_participation_by_id(part.participation_id)
    assert stored.status.value == "cancelled"
