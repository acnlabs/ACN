"""Redis hashes store every field as text. Counts must come back as ints."""

from acn.core.entities.task import TaskStatus
from acn.infrastructure.persistence.redis.task_repository import RedisTaskRepository


def test_string_resubmit_count_loads_as_int_and_can_increment():
    repo = RedisTaskRepository(None)  # type: ignore[arg-type]
    task = repo._dict_to_task({
        "task_id": "task-1",
        "creator_type": "human",
        "creator_id": "creator-1",
        "creator_name": "Creator",
        "title": "t",
        "description": "d",
        "status": "rejected",
        "resubmit_count": "1",
        "max_resubmit_attempts": "3",
        "completed_count": "0",
        "active_participants_count": "0",
    })

    assert task.resubmit_count == 1
    assert task.max_resubmit_attempts == 3
    task.resubmit("second try")
    assert task.resubmit_count == 2
    assert task.status == TaskStatus.SUBMITTED
