"""ACL V6 Scope B — task endpoint security tests.

Three ACL rules introduced in Scope B:

  B-S  submission redaction: GET /tasks/{id} and GET /tasks list hide
       ``submission`` + ``submission_artifacts`` from callers who are not
       the task creator, the assignee, acn:admin, or an internal token.

  B-A  accept gate: POST /tasks/{id}/accept and POST /tasks/agent/{id}/accept
       reject non-subnet-members with 403 NOT_SUBNET_MEMBER when the task
       has a ``subnet_id``.

  B-C  create gate: POST /tasks (and POST /tasks/agent/create) reject a
       creator who is not a member of the specified ``subnet_id`` with
       403 NOT_SUBNET_MEMBER.

Implementation notes for dev_mode interactions
-----------------------------------------------
- Anonymous read endpoints (GET /tasks/{id}) use ``_resolve_caller_identity``
  which calls ``get_agent_service()`` *directly* (not via FastAPI DI), so we
  must also patch ``acn.routes.tasks.get_agent_service`` for the API-key
  resolver to use the stub. The DI override alone is insufficient.

- Accept / create subnet gates check ``"acn:admin" in payload["permissions"]``.
  In dev_mode, ``require_task_write_auth`` returns ``acn:admin`` for every
  caller, which bypasses the gate. We disable dev_mode via monkeypatch for the
  relevant tests and ensure the resolver still finds agent stubs through the
  same direct-call patch.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.routes.dependencies import get_agent_service
from acn.routes.tasks import get_task_service
from acn.services import TaskNotFoundException  # noqa: F401 (imported for completeness)

# ─── helpers ─────────────────────────────────────────────────────────────────

def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _task_id() -> str:
    return str(uuid.uuid4())


# ─── constants ───────────────────────────────────────────────────────────────

_AGENT_A = "agent-alpha"
_AGENT_B = "agent-beta"
_API_KEY_A = "acn_key_alpha"
_API_KEY_B = "acn_key_beta"
_SUBNET_ID = "subnet-private"

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


# ─── entity factories ────────────────────────────────────────────────────────

def _make_task(
    *,
    task_id: str | None = None,
    creator_id: str = _AGENT_A,
    assignee_id: str | None = None,
    subnet_id: str | None = None,
    submission: str | None = "secret-work",
) -> MagicMock:
    t = MagicMock()
    t.task_id = task_id or _task_id()
    t.status = MagicMock()
    t.status.value = "in_progress"
    t.creator_type = "agent"
    t.creator_id = creator_id
    t.creator_name = "Alpha"
    t.assignee_id = assignee_id
    t.assignee_name = assignee_id
    t.assignee_type = "agent" if assignee_id else None
    t.title = "Test task"
    t.description = "Do the thing"
    t.task_type = "general"
    t.required_tags = []
    t.reward = "10"
    t.reward_currency = "credits"
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
    t.created_at = _NOW
    t.deadline = None
    t.group_id = None
    t.metadata = {}
    t.submission = submission
    t.submission_artifacts = []
    t.subnet_id = subnet_id
    t.max_resubmit_attempts = None
    return t


def _make_agent(agent_id: str, api_key: str) -> MagicMock:
    a = MagicMock()
    a.agent_id = agent_id
    a.name = agent_id
    a.api_key = api_key
    return a


def _make_agent_service() -> MagicMock:
    """Stub agent service that maps the two test API keys to agents."""
    svc = MagicMock()

    async def _by_api_key(key: str):
        if key == _API_KEY_A:
            return _make_agent(_AGENT_A, _API_KEY_A)
        if key == _API_KEY_B:
            return _make_agent(_AGENT_B, _API_KEY_B)
        return None

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)
    return svc


# ─── shared fixtures ─────────────────────────────────────────────────────────

@pytest.fixture()
def agent_svc():
    return _make_agent_service()


@pytest.fixture(autouse=True)
def _override_agent_service_di(agent_svc):
    """Register the agent stub in FastAPI DI (used by require_task_write_auth)."""
    app.dependency_overrides[get_agent_service] = lambda: agent_svc
    yield
    app.dependency_overrides.pop(get_agent_service, None)


@pytest.fixture()
def stub_task_service():
    svc = MagicMock()
    app.dependency_overrides[get_task_service] = lambda: svc
    yield svc
    app.dependency_overrides.pop(get_task_service, None)


# ─────────────────────────────────────────────────────────────────────────────
#  B-S: submission redaction
# ─────────────────────────────────────────────────────────────────────────────

class TestSubmissionRedaction:
    """GET /tasks/{id} — submission field visibility."""

    def test_anonymous_cannot_see_submission(self, stub_task_service):
        """Anonymous caller: submission must be null."""
        task = _make_task(submission="secret-work")
        stub_task_service.get_task = AsyncMock(return_value=task)

        with TestClient(app) as client:
            r = client.get(f"/api/v1/tasks/{task.task_id}")

        assert r.status_code == 200, r.text
        assert r.json()["submission"] is None
        assert r.json()["submission_artifacts"] == []

    def test_creator_sees_submission(self, monkeypatch, agent_svc, stub_task_service):
        """Creator (agent API key) sees their own submission.

        We also patch the direct ``get_agent_service`` call inside
        ``_resolve_caller_identity`` (bypasses DI) so it uses the stub.
        """
        monkeypatch.setattr("acn.routes.tasks.get_agent_service", lambda: agent_svc)

        task = _make_task(creator_id=_AGENT_A, submission="my-work")
        stub_task_service.get_task = AsyncMock(return_value=task)

        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/tasks/{task.task_id}",
                headers=_auth(_API_KEY_A),
            )

        assert r.status_code == 200, r.text
        assert r.json()["submission"] == "my-work"

    def test_assignee_sees_submission(self, monkeypatch, agent_svc, stub_task_service):
        """Assignee (agent API key) sees the submission."""
        monkeypatch.setattr("acn.routes.tasks.get_agent_service", lambda: agent_svc)

        task = _make_task(
            creator_id=_AGENT_A,
            assignee_id=_AGENT_B,
            submission="their-work",
        )
        stub_task_service.get_task = AsyncMock(return_value=task)

        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/tasks/{task.task_id}",
                headers=_auth(_API_KEY_B),
            )

        assert r.status_code == 200, r.text
        assert r.json()["submission"] == "their-work"

    def test_unrelated_agent_cannot_see_submission(self, monkeypatch, agent_svc, stub_task_service):
        """Unrelated agent: submission must be null."""
        monkeypatch.setattr("acn.routes.tasks.get_agent_service", lambda: agent_svc)

        task = _make_task(creator_id=_AGENT_A, submission="secret-work")
        stub_task_service.get_task = AsyncMock(return_value=task)

        with TestClient(app) as client:
            r = client.get(
                f"/api/v1/tasks/{task.task_id}",
                headers=_auth(_API_KEY_B),
            )

        assert r.status_code == 200, r.text
        assert r.json()["submission"] is None

    def test_list_tasks_hides_submission_from_anon(self, stub_task_service):
        """GET /tasks list: anonymous caller sees null submissions."""
        tasks = [
            _make_task(creator_id=_AGENT_A, submission="work-1"),
            _make_task(creator_id=_AGENT_B, submission="work-2"),
        ]
        stub_task_service.list_tasks = AsyncMock(return_value=tasks)

        with TestClient(app) as client:
            r = client.get("/api/v1/tasks")

        assert r.status_code == 200, r.text
        for t in r.json()["tasks"]:
            assert t["submission"] is None

    def test_list_tasks_creator_sees_own_submission(self, monkeypatch, agent_svc, stub_task_service):
        """GET /tasks list: creator sees their own submission."""
        monkeypatch.setattr("acn.routes.tasks.get_agent_service", lambda: agent_svc)

        tasks = [
            _make_task(creator_id=_AGENT_A, submission="alpha-work"),
        ]
        stub_task_service.list_tasks = AsyncMock(return_value=tasks)

        with TestClient(app) as client:
            r = client.get("/api/v1/tasks", headers=_auth(_API_KEY_A))

        assert r.status_code == 200, r.text
        assert r.json()["tasks"][0]["submission"] == "alpha-work"


# ─────────────────────────────────────────────────────────────────────────────
#  B-A: accept gate — subnet membership required
#
#  These tests disable dev_mode (which otherwise grants acn:admin to every
#  caller and skips the gate) and patch get_agent_service so the direct
#  call inside require_task_write_auth's resolver uses the stub.
# ─────────────────────────────────────────────────────────────────────────────

def _disable_dev_mode(monkeypatch, agent_svc):
    """Shared setup: disable dev_mode + patch direct agent service lookup."""
    import acn.auth.middleware as _mw
    from acn.config import get_settings

    prod_settings = get_settings()
    monkeypatch.setattr(prod_settings, "dev_mode", False)
    monkeypatch.setattr(_mw, "_get_settings", lambda: prod_settings)
    monkeypatch.setattr(
        "acn.routes.dependencies.get_agent_service", lambda: agent_svc
    )


class TestAcceptGate:
    """POST /tasks/{id}/accept — subnet membership check."""

    def test_non_member_cannot_accept_private_task(self, monkeypatch, agent_svc, stub_task_service):
        """Agent that is not in the task's subnet gets 403."""
        _disable_dev_mode(monkeypatch, agent_svc)

        task = _make_task(subnet_id=_SUBNET_ID)
        stub_task_service.get_task = AsyncMock(return_value=task)
        stub_task_service.is_subnet_member = AsyncMock(return_value=False)

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/tasks/{task.task_id}/accept",
                headers=_auth(_API_KEY_B),
                json={},
            )

        assert r.status_code == 403, r.text
        assert r.json()["error_code"] == "not_subnet_member"
        stub_task_service.accept_task.assert_not_called()

    def test_member_can_accept_private_task(self, monkeypatch, agent_svc, stub_task_service):
        """Agent that is a subnet member can accept the task."""
        _disable_dev_mode(monkeypatch, agent_svc)

        task = _make_task(creator_id=_AGENT_A, subnet_id=_SUBNET_ID)
        stub_task_service.get_task = AsyncMock(return_value=task)
        stub_task_service.is_subnet_member = AsyncMock(return_value=True)
        stub_task_service.accept_task = AsyncMock(return_value=(task, "part-123"))

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/tasks/{task.task_id}/accept",
                headers=_auth(_API_KEY_B),
                json={},
            )

        assert r.status_code == 200, r.text
        stub_task_service.accept_task.assert_awaited_once()

    def test_public_task_can_be_accepted_without_membership(self, stub_task_service):
        """Public tasks (no subnet_id) require no subnet membership check."""
        task = _make_task(creator_id=_AGENT_A, subnet_id=None)
        stub_task_service.get_task = AsyncMock(return_value=task)
        stub_task_service.accept_task = AsyncMock(return_value=(task, None))

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/tasks/{task.task_id}/accept",
                headers=_auth(_API_KEY_B),
                json={},
            )

        assert r.status_code == 200, r.text
        stub_task_service.is_subnet_member.assert_not_called()

    def test_agent_endpoint_non_member_gets_403(self, monkeypatch, agent_svc, stub_task_service):
        """POST /tasks/agent/{id}/accept — non-member gets 403."""
        _disable_dev_mode(monkeypatch, agent_svc)

        task = _make_task(subnet_id=_SUBNET_ID)
        stub_task_service.get_task = AsyncMock(return_value=task)
        stub_task_service.is_subnet_member = AsyncMock(return_value=False)

        with TestClient(app) as client:
            r = client.post(
                f"/api/v1/tasks/agent/{task.task_id}/accept",
                headers=_auth(_API_KEY_B),
            )

        assert r.status_code == 403, r.text
        assert r.json()["error_code"] == "not_subnet_member"


# ─────────────────────────────────────────────────────────────────────────────
#  B-C: create gate — creator must be subnet member
# ─────────────────────────────────────────────────────────────────────────────

_VALID_TASK_BODY: dict[str, Any] = {
    "title": "Scope B create test",
    "description": "A sufficiently long description for the task creation gate test.",
    "deadline_hours": 24,
    "reward": "0",
    "subnet_id": _SUBNET_ID,
}


class TestCreateGate:
    """POST /tasks — subnet membership required when subnet_id is set."""

    def test_non_member_cannot_create_subnet_task(self, monkeypatch, agent_svc, stub_task_service):
        """Agent not in the subnet cannot create a task inside it."""
        _disable_dev_mode(monkeypatch, agent_svc)

        stub_task_service.is_subnet_member = AsyncMock(return_value=False)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/tasks",
                headers=_auth(_API_KEY_A),
                json=_VALID_TASK_BODY,
            )

        assert r.status_code == 403, r.text
        assert r.json()["error_code"] == "not_subnet_member"
        stub_task_service.create_task.assert_not_called()

    def test_member_can_create_subnet_task(self, monkeypatch, agent_svc, stub_task_service):
        """Agent in the subnet can create a task."""
        _disable_dev_mode(monkeypatch, agent_svc)

        task = _make_task(creator_id=_AGENT_A, subnet_id=_SUBNET_ID)
        stub_task_service.is_subnet_member = AsyncMock(return_value=True)
        stub_task_service.create_task = AsyncMock(return_value=task)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/tasks",
                headers=_auth(_API_KEY_A),
                json=_VALID_TASK_BODY,
            )

        assert r.status_code == 200, r.text
        stub_task_service.create_task.assert_awaited_once()

    def test_no_subnet_id_skips_membership_check(self, stub_task_service):
        """Creating a public task (no subnet_id) skips the membership check."""
        task = _make_task(creator_id=_AGENT_A, subnet_id=None)
        stub_task_service.create_task = AsyncMock(return_value=task)

        body = {k: v for k, v in _VALID_TASK_BODY.items() if k != "subnet_id"}
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/tasks",
                headers=_auth(_API_KEY_A),
                json=body,
            )

        assert r.status_code == 200, r.text
        stub_task_service.is_subnet_member.assert_not_called()

    def test_agent_endpoint_non_member_gets_403(self, monkeypatch, agent_svc, stub_task_service):
        """POST /tasks/agent/create — non-member gets 403."""
        _disable_dev_mode(monkeypatch, agent_svc)

        stub_task_service.is_subnet_member = AsyncMock(return_value=False)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/tasks/agent/create",
                headers=_auth(_API_KEY_A),
                json=_VALID_TASK_BODY,
            )

        assert r.status_code == 403, r.text
        assert r.json()["error_code"] == "not_subnet_member"
