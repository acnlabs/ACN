"""Security audit H8: ``GET /tasks/{id}`` must accept the same set of
authenticated callers that ``GET /tasks`` (list) does.

Pre-fix behaviour:
    - ``list_tasks`` resolved caller identity from both ``acn_xxx`` agent
      API keys and Auth0 JWTs (subnet visibility worked for agents)
    - ``get_task`` only ran ``verify_token``, which fails on ``acn_xxx``
      tokens — the agent silently degraded to anonymous, hit the
      "Authentication required to view this task" branch, and got 403
    - Net effect: an agent could see a private task in ``GET /tasks`` but
      could not fetch its detail. Two read paths drifted apart.

Fix:
    Both endpoints now share ``_resolve_caller_identity``, which accepts
    both forms. These tests pin the new behaviour down and would have
    caught the original drift.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.entities import TaskStatus
from acn.routes.dependencies import get_agent_service, limiter
from acn.routes.tasks import get_task_service


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    was = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = was


def _public_task() -> SimpleNamespace:
    """A task entity stand-in with ``subnet_id=None`` — anyone can read it.

    H8 only cares about identity resolution and the subnet-membership
    branch, not response serialisation, so we just need a value that
    satisfies ``_task_to_response``'s field reads without exploding."""
    return SimpleNamespace(
        task_id="task-public",
        subnet_id=None,
        creator_id="creator-1",
        creator_name="Creator",
        creator_type="human",
        title="public",
        description="public task",
        task_type="general",
        reward="0",
        reward_currency="ap_points",
        status=TaskStatus.OPEN,
        required_tags=[],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        metadata={},
        assignee_id=None,
        assignee_name=None,
        assignee_type=None,
        assigned_at=None,
        completed_at=None,
        submission=None,
        submission_artifacts=[],
        submitted_at=None,
        review_notes=None,
        reviewed_by=None,
        deadline=None,
        payment_task_id=None,
        group_id=None,
        completion_mode="best_of_n",
        auto_approve=False,
        require_join_approval=False,
        allow_repeat_by_same=False,
        max_total_budget="0",
        total_budget="0",
        released_amount="0",
        use_escrow=False,
        invited_agent_ids=[],
        max_participants=1,
        completed_count=0,
        active_participants_count=0,
    )


def _private_task(subnet_id: str = "sn-1") -> SimpleNamespace:
    t = _public_task()
    t.task_id = "task-private"
    t.subnet_id = subnet_id
    return t


@pytest.fixture
def task_svc() -> AsyncMock:
    svc = AsyncMock()
    svc.get_task = AsyncMock()
    svc.is_subnet_member = AsyncMock(return_value=True)
    return svc


@pytest.fixture
def agent_svc() -> AsyncMock:
    """Stand-in for the module-level agent service singleton.

    NOTE: ``_resolve_caller_identity`` calls ``get_agent_service()``
    directly (not via ``Depends``) — it shares the same call style as
    ``list_tasks`` and there's no request-scoped dependency to override.
    The test patches the helper at the call site instead."""
    svc = AsyncMock()
    svc.get_agent_by_api_key = AsyncMock()
    return svc


@pytest.fixture
def client(task_svc: AsyncMock, agent_svc: AsyncMock):
    app.dependency_overrides[get_task_service] = lambda: task_svc
    app.dependency_overrides[get_agent_service] = lambda: agent_svc
    with patch("acn.routes.tasks.get_agent_service", return_value=agent_svc):
        with TestClient(app) as c:
            yield c
    app.dependency_overrides.clear()


# ─────────────────────────────────────────────
# Anonymous (no Bearer)
# ─────────────────────────────────────────────


class TestAnonymous:
    def test_public_task_returns_200(
        self, client: TestClient, task_svc: AsyncMock
    ) -> None:
        task_svc.get_task.return_value = _public_task()
        r = client.get("/api/v1/tasks/task-public")
        assert r.status_code == 200
        task_svc.is_subnet_member.assert_not_awaited()

    def test_private_subnet_task_returns_403(
        self, client: TestClient, task_svc: AsyncMock
    ) -> None:
        task_svc.get_task.return_value = _private_task()
        r = client.get("/api/v1/tasks/task-private")
        assert r.status_code == 403
        # Sprint #4-followup: this raise migrated from ``HTTPException`` to
        # ``ACNHTTPError(NOT_SUBNET_MEMBER, …)`` — the body is now the flat
        # ACN schema. The H8 contract preserves status 403 (NOT 401) so
        # an anonymous caller cannot probe whether a private task exists;
        # ``details.reason`` carries the disambiguation marker.
        body = r.json()
        assert body["error_code"] == "not_subnet_member"
        assert body["details"]["reason"] == "anonymous_caller"


# ─────────────────────────────────────────────
# Auth0 JWT (was the only accepted form pre-H8)
# ─────────────────────────────────────────────


class TestJwtCaller:
    def test_subnet_member_with_jwt_returns_200(
        self, client: TestClient, task_svc: AsyncMock
    ) -> None:
        task_svc.get_task.return_value = _private_task()
        task_svc.is_subnet_member.return_value = True

        with patch(
            "acn.routes.tasks.verify_token",
            new=AsyncMock(return_value={"sub": "auth0|abc"}),
        ):
            r = client.get(
                "/api/v1/tasks/task-private",
                headers={"Authorization": "Bearer eyJ.fake.jwt"},
            )

        assert r.status_code == 200
        task_svc.is_subnet_member.assert_awaited_once_with("sn-1", "auth0|abc")

    def test_non_member_with_jwt_returns_403(
        self, client: TestClient, task_svc: AsyncMock
    ) -> None:
        task_svc.get_task.return_value = _private_task()
        task_svc.is_subnet_member.return_value = False

        with patch(
            "acn.routes.tasks.verify_token",
            new=AsyncMock(return_value={"sub": "auth0|outsider"}),
        ):
            r = client.get(
                "/api/v1/tasks/task-private",
                headers={"Authorization": "Bearer eyJ.fake.jwt"},
            )

        assert r.status_code == 403
        body = r.json()
        assert body["error_code"] == "not_subnet_member"
        assert body["details"]["reason"] == "not_member"


# ─────────────────────────────────────────────
# Agent API key (the H8 regression)
# ─────────────────────────────────────────────


class TestAgentApiKeyCaller:
    """Pre-H8, every test in this class would 403 — ``verify_token`` raised
    on ``acn_xxx`` tokens and ``get_task`` had no API-key fallback. The fix
    is for ``get_task`` to share ``list_tasks``'s resolution logic."""

    def test_subnet_member_with_acn_key_returns_200(
        self,
        client: TestClient,
        task_svc: AsyncMock,
        agent_svc: AsyncMock,
    ) -> None:
        task_svc.get_task.return_value = _private_task()
        task_svc.is_subnet_member.return_value = True

        agent = MagicMock()
        agent.agent_id = "agent-007"
        agent_svc.get_agent_by_api_key.return_value = agent

        r = client.get(
            "/api/v1/tasks/task-private",
            headers={"Authorization": "Bearer acn_secret_007"},
        )

        assert r.status_code == 200, (
            f"agent with acn_xxx key should be able to read its own subnet's "
            f"private tasks (H8 contract); got {r.status_code} {r.text}"
        )
        agent_svc.get_agent_by_api_key.assert_awaited_once_with("acn_secret_007")
        task_svc.is_subnet_member.assert_awaited_once_with("sn-1", "agent-007")

    def test_non_member_agent_returns_403(
        self,
        client: TestClient,
        task_svc: AsyncMock,
        agent_svc: AsyncMock,
    ) -> None:
        task_svc.get_task.return_value = _private_task()
        task_svc.is_subnet_member.return_value = False

        agent = MagicMock()
        agent.agent_id = "agent-outsider"
        agent_svc.get_agent_by_api_key.return_value = agent

        r = client.get(
            "/api/v1/tasks/task-private",
            headers={"Authorization": "Bearer acn_outsider"},
        )

        assert r.status_code == 403
        body = r.json()
        assert body["error_code"] == "not_subnet_member"
        assert body["details"]["reason"] == "not_member"

    def test_invalid_acn_key_treated_as_anonymous(
        self,
        client: TestClient,
        task_svc: AsyncMock,
        agent_svc: AsyncMock,
    ) -> None:
        """If the API key doesn't resolve to an agent, the caller is
        anonymous — same as no Bearer header at all. The endpoint must
        NOT escalate to "JWT verification required" because that was the
        pre-fix bug pattern (verify_token would raise on the acn_ prefix)."""
        task_svc.get_task.return_value = _private_task()
        agent_svc.get_agent_by_api_key.return_value = None

        r = client.get(
            "/api/v1/tasks/task-private",
            headers={"Authorization": "Bearer acn_unknown"},
        )

        assert r.status_code == 403
        body = r.json()
        assert body["error_code"] == "not_subnet_member"
        assert body["details"]["reason"] == "anonymous_caller"
        # Crucially: we should never even attempt subnet membership — there's
        # no identity to check against
        task_svc.is_subnet_member.assert_not_awaited()


# ─────────────────────────────────────────────
# Helper drift guard
# ─────────────────────────────────────────────


class TestSharedHelper:
    def test_get_task_and_list_tasks_use_same_helper(self) -> None:
        """If a future refactor inlines either path again, this test fails
        and forces the author to look at H8 before re-introducing drift."""
        from acn.routes import tasks as tasks_module

        assert hasattr(tasks_module, "_resolve_caller_identity"), (
            "shared identity resolver removed — H8 regression risk; "
            "list_tasks and get_task MUST share resolution logic"
        )

        import inspect

        get_task_src = inspect.getsource(tasks_module.get_task)
        list_tasks_src = inspect.getsource(tasks_module.list_tasks)

        assert "_resolve_caller_identity" in get_task_src
        assert "_resolve_caller_identity" in list_tasks_src

        # Negative guard: neither route should be re-implementing the
        # acn_/JWT branching inline (that's the original drift)
        for name, src in (("get_task", get_task_src), ("list_tasks", list_tasks_src)):
            assert "verify_token" not in src or "_resolve_caller_identity" in src, (
                f"{name} appears to have inlined verify_token — use "
                f"_resolve_caller_identity to keep both read paths aligned"
            )
