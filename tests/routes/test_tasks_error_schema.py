"""Tasks routes — flat ACN error schema contract tests.

Phase 2 review v2 P1 #11 sprint roadmap row #4 — pin the migrated
4xx sites in ``acn/routes/tasks.py`` to the canonical ``ACNHTTPError``
flat schema after their conversion from raw ``HTTPException``.

#4 scope (this file): the 13 4xx sites that all raise the **same**
``ACNHTTPError(TASK_NOT_FOUND, 404, {"task_id": …}) from None`` —
the most uniform sprint of the migration so far. Every site sits in
an identical ``except TaskNotFoundException: raise … from None``
branch reached when ``TaskService.<op>`` cannot find the task by
its path-parameter ``task_id``. Because the raise shape is uniform,
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

Out of scope (deferred to later sprints, see BACKLOG row
#4-followup):

* 26 4xx sites that need new catalog codes — 9× ``PermissionError``
  re-raises (403), 9× ``ValueError`` re-raises (400 — body / status
  validation), 2× tag/status validation (400 inside
  ``list_tasks`` / ``match_tasks_for_agent``), 2× subnet-membership
  enforcement on private tasks (403 inside ``get_task``), 1× missing
  agent API key (401), 1× missing acn:write JWT permission (403).
  These pick up alongside sprint rows #2b/#2c once the auth /
  permission codes settle on names — the ownership/permission
  semantics overlap heavily with registry's deferred set.

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
