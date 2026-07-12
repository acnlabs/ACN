"""ACN accept：驯养相关任务元数据启发式（与 Labs is_cultivator_work 对齐）。"""

from unittest.mock import MagicMock

import pytest

from acn.routes.tasks import _task_requires_cultivator_human


def _fake_task(*, xp_reward=None, task_type="general"):
    task = MagicMock()
    meta = {}
    if xp_reward is not None:
        meta["xp_reward"] = xp_reward
    task.metadata = meta
    task.task_type = task_type
    return task


@pytest.mark.parametrize(
    "xp_reward,task_type,expected",
    [
        (10, "general", True),
        (0, "general", False),
        (None, "agent_feedback", True),
        (None, "general", False),
        ("25", "general", True),
        ("x", "general", False),
    ],
)
def test_task_requires_cultivator_human_heuristic(xp_reward, task_type, expected):
    assert (
        _task_requires_cultivator_human(_fake_task(xp_reward=xp_reward, task_type=task_type))
        is expected
    )
