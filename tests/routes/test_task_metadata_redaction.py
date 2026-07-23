"""Public TaskResponse must never expose metadata.harness_secret."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from acn.routes.tasks import _public_task_metadata, _task_to_response


def test_public_task_metadata_strips_harness_secret() -> None:
    out = _public_task_metadata(
        {
            "org_id": "org_x",
            "org_publish": True,
            "harness_url": "https://hooks.example/acn",
            "harness_secret": "s3cr3t",
        }
    )
    assert out["org_id"] == "org_x"
    assert out["harness_url"] == "https://hooks.example/acn"
    assert "harness_secret" not in out


def test_task_to_response_redacts_harness_secret() -> None:
    t = MagicMock()
    t.task_id = "task_1"
    t.status = SimpleNamespace(value="open")
    t.creator_type = "agent"
    t.creator_id = "agent_a"
    t.creator_name = "A"
    t.title = "t"
    t.description = "d" * 12
    t.task_type = "general"
    t.required_tags = ["smoke"]
    t.assignee_id = None
    t.assignee_name = None
    t.assignee_type = None
    t.reward = "0"
    t.reward_currency = "ap_points"
    t.total_budget = "0"
    t.released_amount = "0"
    t.max_participants = 1
    t.completion_mode = "independent"
    t.max_total_budget = None
    t.require_join_approval = False
    t.auto_approve = False
    t.allow_repeat_by_same = False
    t.use_escrow = False
    t.invited_agent_ids = []
    t.active_participants_count = 0
    t.completed_count = 0
    t.created_at = datetime(2026, 7, 23, tzinfo=UTC)
    t.deadline = None
    t.group_id = None
    t.metadata = {
        "org_id": "org_x",
        "harness_secret": "s3cr3t",
        "ui_spec": {"x": 1},
    }
    t.submission = None
    t.submission_artifacts = []
    t.subnet_slug = "fence-slug"
    t.max_resubmit_attempts = None

    resp = _task_to_response(t)
    assert resp.metadata.get("org_id") == "org_x"
    assert "harness_secret" not in resp.metadata
    assert resp.ui_spec == {"x": 1}
