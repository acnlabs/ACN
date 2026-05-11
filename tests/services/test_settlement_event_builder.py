"""Producer-side gating tests for ``TaskService._build_review_pass_event``.

Scope: verify the contract between the producer (which decides
``step_status`` at enqueue time) and the worker (which trusts it).
Any drift here breaks Todo 6 worker correctness — e.g. producer
enqueuing ``escrow_release=pending`` for a zero-reward task would
cause the worker to ``raise StepHandlerError`` and DLQ.

These tests stay deliberately narrow: only the dict output of
``_build_review_pass_event`` is examined. They do NOT exercise
``complete_task``, the saga path, or DB I/O.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from acn.core.entities.task import Task, TaskStatus
from acn.core.interfaces.task_repository import ITaskRepository
from acn.services.task_service import TaskService


def _build_task(
    *,
    task_id: str = "task-1",
    creator_id: str = "user-creator",
    assignee_id: str | None = "agent-worker",
    reward: str = "100",
    reward_currency: str = "ap_points",
    use_escrow: bool = True,
    max_participants: int | None = 1,
    metadata: dict | None = None,
) -> Task:
    """Construct a Task in a state representative of "submitted,
    ready for review" — that's the only stage from which
    ``_build_review_pass_event`` is called.
    """
    return Task(
        task_id=task_id,
        creator_type="human",
        creator_id=creator_id,
        creator_name="creator-display",
        title="test-title",
        description="test-desc",
        status=TaskStatus.SUBMITTED,
        assignee_id=assignee_id,
        assignee_name="agent-display" if assignee_id else None,
        assignee_type="agent" if assignee_id else None,
        reward=reward,
        reward_currency=reward_currency,
        use_escrow=use_escrow,
        max_participants=max_participants,
        metadata=metadata or {},
    )


def _service() -> TaskService:
    """Minimum TaskService. ``_build_review_pass_event`` reads no
    instance state — only its ``task`` argument — so a repository
    placeholder is enough."""
    return TaskService(repository=MagicMock(spec=ITaskRepository))


# =============================================================================
# step_status gating contract — happy combinations
# =============================================================================


def test_main_path_escrow_and_reward_both_pending() -> None:
    """The mainline production case: use_escrow=True + ap_points +
    positive reward + assignee present. All three steps pending.
    """
    event = _service()._build_review_pass_event(
        _build_task(use_escrow=True, reward="100"),
        approver_id="user-creator",
        notes=None,
    )
    assert event.step_status == {
        "escrow_release": "pending",
        "reward_distribute": "pending",
        "reputation_write": "pending",
    }
    assert event.payload["use_escrow"] is True
    assert event.payload["is_multi"] is False


def test_off_chain_reward_keeps_reward_pending_but_skips_escrow() -> None:
    """``use_escrow=False`` + reward > 0: producer must NOT mark
    escrow_release=pending (worker would 404 on get_by_task), but
    reward_distribute stays pending so the worker's logged no-op
    captures the off-chain bookkeeping intent.
    """
    event = _service()._build_review_pass_event(
        _build_task(use_escrow=False, reward="100"),
        approver_id="user-creator",
        notes=None,
    )
    assert event.step_status["escrow_release"] == "skipped"
    assert event.step_status["reward_distribute"] == "pending"
    assert event.step_status["reputation_write"] == "pending"
    assert event.payload["use_escrow"] is False


def test_zero_reward_escrow_task_skips_both_payment_steps() -> None:
    """Regression for Bug B-2: ``use_escrow=True`` + ``reward=0`` must
    NOT mark escrow_release=pending. Otherwise the worker hits
    ``amount <= 0`` and DLQs after 12 retries on a non-action.
    """
    event = _service()._build_review_pass_event(
        _build_task(use_escrow=True, reward="0"),
        approver_id="user-creator",
        notes=None,
    )
    assert event.step_status["escrow_release"] == "skipped"
    assert event.step_status["reward_distribute"] == "skipped"
    # Reputation still applies — task was completed, agent earns
    # reputation even with no monetary reward.
    assert event.step_status["reputation_write"] == "pending"


def test_non_ap_points_currency_skips_reward_and_escrow() -> None:
    """Non-ap_points reward: ACN v0.1 escrow only supports ap_points
    (see ``AgentPlanetEscrowProvider``), so reward_distribute is
    skipped and — per B-2 — escrow_release is too. This avoids
    enqueuing events for currencies the worker can't actually
    settle."""
    event = _service()._build_review_pass_event(
        _build_task(use_escrow=True, reward="100", reward_currency="USD"),
        approver_id="user-creator",
        notes=None,
    )
    assert event.step_status["escrow_release"] == "skipped"
    assert event.step_status["reward_distribute"] == "skipped"
    assert event.step_status["reputation_write"] == "pending"


def test_missing_assignee_skips_every_step() -> None:
    """Auto-approve flows can complete a task without ever assigning
    an agent (creator's own work, or admin override). All three
    steps must be skipped — there's no one to release funds to or
    write reputation for."""
    event = _service()._build_review_pass_event(
        _build_task(use_escrow=True, reward="100", assignee_id=None),
        approver_id="user-creator",
        notes=None,
    )
    assert event.step_status == {
        "escrow_release": "skipped",
        "reward_distribute": "skipped",
        "reputation_write": "skipped",
    }


# =============================================================================
# payload snapshot contract
# =============================================================================


def test_payload_freezes_is_multi_for_single_participant() -> None:
    """is_multi must be captured at enqueue time — the worker
    branches on it to pick release vs release_partial."""
    event = _service()._build_review_pass_event(
        _build_task(max_participants=1),
        approver_id="user-creator",
        notes=None,
    )
    assert event.payload["is_multi"] is False


@pytest.mark.parametrize("max_participants", [None, 3, 10])
def test_payload_freezes_is_multi_for_multi_participant(
    max_participants: int | None,
) -> None:
    """``max_participants is None`` (unlimited) and ``> 1`` (fixed
    multi) both flag as multi for worker routing."""
    event = _service()._build_review_pass_event(
        _build_task(max_participants=max_participants),
        approver_id="user-creator",
        notes=None,
    )
    assert event.payload["is_multi"] is True


def test_payload_carries_smoke_test_metadata() -> None:
    """``task.metadata`` snapshot is the channel the producer uses
    to propagate the smoke_test flag into the saga. Worker
    forwards it to ``ReputationService.record_feedback`` so
    smoke traffic gets stamped and can be filtered on read."""
    event = _service()._build_review_pass_event(
        _build_task(metadata={"smoke_test": True, "other": "x"}),
        approver_id="user-creator",
        notes=None,
    )
    assert event.payload["metadata"] == {"smoke_test": True, "other": "x"}


def test_payload_metadata_is_a_copy_not_a_reference() -> None:
    """Mutating ``task.metadata`` AFTER enqueue must not bleed into
    the event payload. ``_build_review_pass_event`` defensively
    copies the dict; this regression guard catches anyone who
    optimizes that away."""
    original = {"smoke_test": False, "tag": "x"}
    task = _build_task(metadata=original)
    event = _service()._build_review_pass_event(task, approver_id="user-creator", notes=None)
    original["smoke_test"] = True
    assert event.payload["metadata"]["smoke_test"] is False


def test_event_id_is_deterministic_for_same_task() -> None:
    """Idempotency contract: two calls on the same task produce
    the SAME event_id. The DB UNIQUE on ``event_id`` then silently
    drops the duplicate enqueue — see ``enqueue`` docstring."""
    task = _build_task()
    e1 = _service()._build_review_pass_event(task, approver_id="a", notes=None)
    e2 = _service()._build_review_pass_event(task, approver_id="b", notes="x")
    assert e1.event_id == e2.event_id
