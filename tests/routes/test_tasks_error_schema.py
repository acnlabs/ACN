"""Tasks routes — flat ACN error schema contract tests.

Phase 2 review v2 P1 #11 sprint roadmap rows #4 + #4-followup —
pin the migrated 4xx sites in ``acn/routes/tasks.py`` to the
canonical ``ACNHTTPError`` flat schema after their conversion
from raw ``HTTPException``.

Sprint #4 scope (legacy section): the 13 4xx sites that all raise
the **same** ``ACNHTTPError(TASK_NOT_FOUND, 404, {"task_id": …})
from None`` — the most uniform sprint of the migration so far.
Every site sits in an identical
``except TaskNotFoundException: raise … from None`` branch reached
when ``TaskService.<op>`` cannot find the task by its
path-parameter ``task_id``. Because the raise shape is uniform,
exhaustively pinning each of the 13 endpoints would produce
near-identical tests; instead we cover **three representative
authentication shapes** so a regression on any single auth path
fails loudly:

* ``GET /tasks/{task_id}`` — public discovery, optional bearer
  (auth-less path is allowed). Most-used 404 surface for SDK
  clients listing then drilling into a missing task.
* ``POST /tasks/{task_id}/accept`` — write path gated by
  ``require_task_write_auth()``. Dev-mode bypass (``settings.dev_mode``
  default in the test env) accepts any non-empty Bearer token, so
  the migration path through the JWT/agent-key dispatcher is
  exercised without standing up Auth0.
* ``POST /tasks/agent/{task_id}/accept`` — the dedicated
  agent-API-key surface (``AgentApiKeyDep``). Pins that the
  ``acn_xxx``-bearer auth path also surfaces ``TASK_NOT_FOUND``
  with the flat schema, not a stale legacy ``{"detail": "..."}``.

Sprint #4-followup scope (cross-module catalog): the 26 deferred
4xx sites adopt the cross-module ErrorCode catalog from
sprint #2b. Three raise-shape patterns dominate:

* **PermissionError pair** (×10) — every write endpoint that
  goes through ``task_service`` raises
  ``ACNHTTPError(OWNERSHIP_MISMATCH, 403, {task_id, reason})``.
  ``replace_all=true`` migration pinned all 10 sites with
  byte-identical bodies; one representative test in this file.
* **ValueError pair** (×10) — paired 1:1 with the PermissionError
  sites (except for ``list_participations`` and the dedicated
  ``acn:write`` JWT permission gate);
  ``ACNHTTPError(INVALID_REQUEST, 400, {task_id, reason})``.
  Same ``replace_all=true`` migration; one representative test.
* **Endpoint-specific 4xx** (×6) — ``require_task_write_auth``
  agent-key 401 + JWT 403, ``list_tasks`` invalid-status 400,
  ``match_tasks_for_agent`` empty-tag 400, and the two
  ``get_task`` private-subnet 403 sites.

Out of scope (still deferred):

* 1 5xx site (``create_task`` catch-all) — kept on raw
  ``HTTPException`` per the sanitisation contract.

Why ``from None`` (not ``from e``)
  Each ``TaskNotFoundException(task_id)`` is a thin wrapper that
  carries no information beyond the ``task_id`` already exposed
  in ``details``. Suppressing the cause keeps internal class
  names out of any structured-logging downstream pipelines that
  serialise tracebacks. The choice is **module-local consistency**:
  ``tasks.py`` was already ``from None`` across all 13 sites
  before this migration, and the ACN error-schema migration's
  policy is to preserve each module's existing cause-chain
  conventions.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException
from acn.routes.dependencies import get_agent_service
from acn.routes.tasks import get_task_service
from acn.services import TaskNotFoundException
from tests.routes.conftest import _assert_flat_shape


@pytest.fixture
def stub_task_service():
    """``get_task`` and ``accept_task`` raise ``TaskNotFoundException``
    for any ``task_id`` — the only behaviour these tests exercise.
    Returning a real task is unnecessary; the migrated raise is
    triggered before any happy-path code runs."""
    svc = AsyncMock()
    svc.get_task = AsyncMock(side_effect=TaskNotFoundException("task-missing"))
    svc.accept_task = AsyncMock(side_effect=TaskNotFoundException("task-missing"))
    return svc


@pytest.fixture
def stub_agent_service():
    """Wires ``owner-key`` → ``agent-target`` for the
    agent-API-key path of ``POST /tasks/agent/{task_id}/accept``.
    """
    svc = AsyncMock()
    target = MagicMock()
    target.agent_id = "agent-target"
    target.name = "Target"
    target.wallet_address = None

    async def _by_api_key(key: str):
        if key == "owner-key":
            return target
        return None

    async def _get_agent(agent_id: str):
        if agent_id == "agent-target":
            return target
        raise AgentNotFoundException(agent_id)

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)
    svc.get_agent = AsyncMock(side_effect=_get_agent)
    return svc


def _wire(task_svc, agent_svc) -> None:
    app.dependency_overrides[get_task_service] = lambda: task_svc
    app.dependency_overrides[get_agent_service] = lambda: agent_svc


class TestTasksFlatErrorSchema:
    """Pin response shape for the three representative endpoints
    described in the module docstring."""

    def test_get_task_404_public_read_flat_shape(
        self, stub_task_service, stub_agent_service
    ):
        """``GET /api/v1/tasks/{task_id}`` — optional-bearer public
        read. No auth headers needed; the migrated raise fires from
        the first ``await task_service.get_task(task_id)``."""
        _wire(stub_task_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/tasks/task-missing")

        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "task_not_found"
        assert body["details"] == {"task_id": "task-missing"}
        assert r.headers.get("X-Request-ID") == body["request_id"]

    def test_accept_task_404_jwt_path_flat_shape(
        self, stub_task_service, stub_agent_service
    ):
        """``POST /api/v1/tasks/{task_id}/accept`` — write path
        gated by ``require_task_write_auth()``. Dev-mode auth
        bypass (see ``acn/auth/middleware.py::verify_token``)
        accepts any non-empty Bearer token in the test env, so we
        exercise the JWT/agent-key dispatcher's success branch
        before the migrated ``except TaskNotFoundException`` fires.

        Auth bypass note
            Identical mechanism to ``test_registry_error_schema.py``'s
            ``test_unregister_returns_404_with_flat_shape`` — see
            that file's class docstring for the rationale and for
            why ``app.dependency_overrides`` would be more fragile
            than the dev-mode pathway here.
        """
        _wire(stub_task_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/tasks/task-missing/accept",
                headers={"Authorization": "Bearer dev-mode-any-token"},
                json={},
            )

        assert r.status_code != 401, (
            "POST /tasks/{id}/accept returned 401 — dev_mode auth "
            "bypass is no longer in effect. Restore the dev-mode "
            "default in the test environment, or rewrite this test "
            "against the new auth surface (sprint row #10)."
        )
        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "task_not_found"
        assert body["details"] == {"task_id": "task-missing"}

    def test_agent_accept_task_404_agent_api_key_flat_shape(
        self, stub_task_service, stub_agent_service
    ):
        """``POST /api/v1/tasks/agent/{task_id}/accept`` — dedicated
        agent-API-key endpoint (``AgentApiKeyDep``). Distinct from
        the JWT-path test because this surface authenticates by
        ``acn_xxx`` Bearer key resolution against the agent
        service, not by Auth0 JWT or dev-mode bypass.

        Tests the second of the two bearer-auth paths through the
        same ``ACNHTTPError(TASK_NOT_FOUND, 404)`` raise; pinning
        both ensures a future split / merge of the two bearer-auth
        dispatchers can't quietly diverge their 404 wire shapes.
        """
        _wire(stub_task_service, stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/tasks/agent/task-missing/accept",
                headers={"Authorization": "Bearer owner-key"},
                json={},
            )

        assert r.status_code == 404
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "task_not_found"
        assert body["details"] == {"task_id": "task-missing"}


# ─────────────────────────────────────────────
# Sprint #4-followup — cross-module catalog
# ─────────────────────────────────────────────


@pytest.fixture
def stub_task_service_for_followup():
    """Tasks fixture for #4-followup tests.

    Distinct from ``stub_task_service`` above because the followup
    tests need the *task-found* happy path to reach ownership /
    validation / subnet-membership gates rather than the 404 path.
    Individual tests override ``side_effect`` per case (e.g.
    PermissionError, ValueError, private subnet behaviour).
    """
    svc = AsyncMock()

    target = MagicMock()
    target.task_id = "task-target"
    target.creator_id = "user-1"
    # Step 2 complete: entity attribute is now ``subnet_slug``.
    target.subnet_slug = None  # public task by default
    target.status = "open"
    target.mode = "single"
    target.title = "Test"
    target.description = "Test task"
    target.tags = []
    target.created_at = "2025-01-01T00:00:00Z"
    target.updated_at = "2025-01-01T00:00:00Z"
    target.assignee_id = None
    target.metadata = {}
    target.budget = None
    target.deadline = None
    target.payment_terms = None
    target.attachments = []
    target.acceptance_criteria = None
    target.deliverable_format = None

    svc.get_task = AsyncMock(return_value=target)
    svc.is_subnet_member = AsyncMock(return_value=False)
    svc.accept_task = AsyncMock(return_value=(target, "participation-1"))
    return svc


class TestTasksFlatErrorSchemaCrossModule:
    """Sprint #4-followup — pin the 26 cross-module raise sites.

    Coverage choice rationale
        Sprint #4-followup adds 26 new raise sites — 20 of them in
        two ``replace_all=true`` cohorts (10× ``OWNERSHIP_MISMATCH``
        + 10× ``INVALID_REQUEST``) where every site is byte-identical.
        For those cohorts a single representative test is sufficient
        — pinning all 10 sites would balloon the test file with
        zero added regression coverage. The remaining 6
        endpoint-specific sites get their own per-site test because
        each has a distinct ``details`` shape.

    Auth-gate test environments
        Two of the 6 endpoint-specific sites
        (``require_task_write_auth`` 401 invalid-key + 403
        missing-acn:write) are inside the dev-mode bypass logic;
        ``settings.dev_mode=True`` accepts any non-empty Bearer
        token. To exercise the production paths we
        ``monkeypatch.setattr`` ``settings.dev_mode = False`` for
        those tests only — see each test's docstring.
    """

    # ─── 6 endpoint-specific tests ────────────────────────────────

    def test_require_task_write_auth_invalid_agent_key_returns_authentication_required(
        self, stub_task_service_for_followup, stub_agent_service, monkeypatch
    ):
        """``POST /tasks/{id}/accept`` with ``Bearer acn_…`` whose
        key resolution returns ``None`` — pins
        ``AUTHENTICATION_REQUIRED`` with the
        ``invalid_agent_api_key`` reason.

        Dev-mode bypass would accept any Bearer token and route
        around the ``acn_xxx`` resolution; we disable it so the
        production agent-key dispatcher branch fires.
        """
        from acn.routes.tasks import settings as tasks_settings

        monkeypatch.setattr(tasks_settings, "dev_mode", False)
        _wire(stub_task_service_for_followup, stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/tasks/task-target/accept",
                headers={"Authorization": "Bearer acn_unknown-key"},
                json={},
            )

        assert r.status_code == 401
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "authentication_required"
        assert body["details"] == {"reason": "invalid_agent_api_key"}

    def test_require_task_write_auth_missing_acn_write_returns_missing_permission(
        self, stub_task_service_for_followup, stub_agent_service, monkeypatch
    ):
        """``POST /tasks/{id}/accept`` via Auth0 JWT path with a
        token that lacks the ``acn:write`` permission — pins
        ``MISSING_PERMISSION``.

        We disable dev-mode (which would grant ``acn:admin``) and
        monkeypatch ``verify_token`` to return a controlled
        non-write payload, exercising the JWT-path 403 in
        ``require_task_write_auth``.
        """
        from acn.routes.tasks import settings as tasks_settings

        async def _fake_verify_token(*args, **kwargs):
            return {"sub": "user-1", "permissions": ["acn:read"]}

        monkeypatch.setattr(tasks_settings, "dev_mode", False)
        monkeypatch.setattr(
            "acn.routes.tasks.verify_token", _fake_verify_token
        )
        _wire(stub_task_service_for_followup, stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/tasks/task-target/accept",
                headers={"Authorization": "Bearer non-write-jwt"},
                json={},
            )

        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "missing_permission"
        assert body["details"] == {"required_permission": "acn:write"}

    def test_list_tasks_invalid_status_returns_invalid_request(
        self, stub_task_service_for_followup, stub_agent_service
    ):
        """``GET /tasks?status=bogus`` — pins ``INVALID_REQUEST``
        with field/value/allowed details for the rejected status
        enum value."""
        stub_task_service_for_followup.list_tasks = AsyncMock(return_value=[])
        _wire(stub_task_service_for_followup, stub_agent_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/tasks", params={"status": "bogus"})

        assert r.status_code == 400
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "invalid_request"
        assert body["details"]["field"] == "status"
        assert body["details"]["value"] == "bogus"
        assert isinstance(body["details"]["allowed"], list)
        assert len(body["details"]["allowed"]) > 0

    def test_match_tasks_empty_tags_returns_invalid_request(
        self, stub_task_service_for_followup, stub_agent_service
    ):
        """``GET /tasks/match?tags=,,`` — pins ``INVALID_REQUEST``
        with field/reason for the empty-tag validation."""
        _wire(stub_task_service_for_followup, stub_agent_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/tasks/match", params={"tags": ",,"})

        assert r.status_code == 400
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "invalid_request"
        assert body["details"] == {"field": "tags", "reason": "tag_list_empty"}

    def test_get_task_private_subnet_anonymous_returns_not_subnet_member(
        self, stub_task_service_for_followup, stub_agent_service
    ):
        """``GET /tasks/{id}`` against a task whose ``slug``
        is set, with no authentication — pins ``NOT_SUBNET_MEMBER``
        with ``reason="anonymous_caller"``.

        The 403 status (rather than 401) is intentional: surfacing
        401 would let an attacker probe whether a task exists by
        observing the auth-gate behaviour. ``NOT_SUBNET_MEMBER`` at
        403 is the unified semantic — ``details.reason``
        disambiguates anonymous vs non-member callers for the SDK.
        """
        target = MagicMock()
        target.subnet_slug = "subnet-private"
        target.creator_id = "user-1"
        stub_task_service_for_followup.get_task = AsyncMock(return_value=target)
        _wire(stub_task_service_for_followup, stub_agent_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/tasks/task-target")

        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "not_subnet_member"
        assert body["details"] == {
            "task_id": "task-target",
            "slug": "subnet-private",
            "reason": "anonymous_caller",
        }

    def test_get_task_private_subnet_not_member_returns_not_subnet_member(
        self, stub_task_service_for_followup, stub_agent_service, monkeypatch
    ):
        """``GET /tasks/{id}`` against a private-subnet task by an
        authenticated caller who is *not* a member — pins
        ``NOT_SUBNET_MEMBER`` with ``reason="not_member"`` and
        ``agent_id`` populated.

        We monkeypatch ``_resolve_caller_identity`` to return a
        known caller and stub ``is_subnet_member=False`` so the
        private-subnet gate's second branch fires.
        """
        target = MagicMock()
        target.subnet_slug = "subnet-private"
        target.creator_id = "user-1"
        stub_task_service_for_followup.get_task = AsyncMock(return_value=target)
        stub_task_service_for_followup.is_subnet_member = AsyncMock(
            return_value=False
        )

        async def _fake_resolve(request, credentials):
            return "user-2"

        monkeypatch.setattr(
            "acn.routes.tasks._resolve_caller_identity", _fake_resolve
        )
        _wire(stub_task_service_for_followup, stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/tasks/task-target",
                headers={"Authorization": "Bearer some-non-member-jwt"},
            )

        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "not_subnet_member"
        assert body["details"] == {
            "task_id": "task-target",
            # See sibling test — error payload field still uses
            "slug": "subnet-private",
            "agent_id": "user-2",
            "reason": "not_member",
        }

    # ─── 2 representative-cohort tests ────────────────────────────

    def test_accept_task_permission_error_returns_ownership_mismatch(
        self, stub_task_service_for_followup, stub_agent_service
    ):
        """``POST /tasks/{id}/accept`` when ``task_service.accept_task``
        raises ``PermissionError`` — pins ``OWNERSHIP_MISMATCH``
        with ``task_id`` + ``reason``. Representative for the 10
        identical ``except PermissionError`` sites that the
        ``replace_all=true`` migration pinned to a single body."""
        stub_task_service_for_followup.accept_task = AsyncMock(
            side_effect=PermissionError("Task is not open for acceptance.")
        )
        _wire(stub_task_service_for_followup, stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/tasks/task-target/accept",
                headers={"Authorization": "Bearer dev-mode-any-token"},
                json={},
            )

        assert r.status_code == 403
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "ownership_mismatch"
        assert body["details"] == {
            "task_id": "task-target",
            "reason": "permission_denied",
        }

    def test_accept_task_value_error_returns_invalid_request(
        self, stub_task_service_for_followup, stub_agent_service
    ):
        """``POST /tasks/{id}/accept`` when ``task_service.accept_task``
        raises ``ValueError`` — pins ``INVALID_REQUEST`` with
        ``task_id`` + ``reason``. Representative for the 10
        identical ``except ValueError`` sites that the
        ``replace_all=true`` migration pinned to a single body."""
        stub_task_service_for_followup.accept_task = AsyncMock(
            side_effect=ValueError("Task already in terminal state.")
        )
        _wire(stub_task_service_for_followup, stub_agent_service)

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/tasks/task-target/accept",
                headers={"Authorization": "Bearer dev-mode-any-token"},
                json={},
            )

        assert r.status_code == 400
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "invalid_request"
        assert body["details"] == {
            "task_id": "task-target",
            "reason": "invalid_request",
        }
