"""Thin HTTP smoke for Execution Workspace GET attestation / close."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from acn.api import app
from acn.core.entities.workspace import Workspace, WorkspaceAttestation
from acn.routes.dependencies import get_workspace_service
from acn.routes.orgs import OrgAuthDep, OrgAuthReadDep
from acn.services.workspace_service import WorkspaceConflictError, WorkspaceNotFoundError


def _client(svc: AsyncMock, *, sub: str = "agt_owner") -> TestClient:
    async def _auth() -> dict:
        return {"type": "agent", "sub": sub, "permissions": ["acn:write"]}

    app.dependency_overrides[get_workspace_service] = lambda: svc
    app.dependency_overrides[OrgAuthDep.dependency] = _auth
    app.dependency_overrides[OrgAuthReadDep.dependency] = _auth
    return TestClient(app)


def test_get_attestation_ok():
    svc = AsyncMock()
    att = WorkspaceAttestation(
        attestation_id="att_1",
        workspace_id="ws_1",
        agent_id="agt_worker",
        run_id="r1",
        issued_at=datetime(2026, 8, 28, tzinfo=UTC),
    )
    svc.get_attestation = AsyncMock(return_value=att)
    client = _client(svc)
    resp = client.get("/api/v1/workspaces/ws_1/attestations/att_1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "workspace_owner"
    assert body["attestation_id"] == "att_1"


def test_get_attestation_404_same_shape_as_workspace_miss():
    svc = AsyncMock()
    svc.get_attestation = AsyncMock(side_effect=WorkspaceNotFoundError("ws_1"))
    client = _client(svc)
    resp = client.get("/api/v1/workspaces/ws_1/attestations/att_missing")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_code"] == "workspace_not_found"
    assert body["details"] == {"workspace_id": "ws_1"}


def test_get_workspace_ok():
    svc = AsyncMock()
    svc.get_workspace = AsyncMock(
        return_value=Workspace(
            workspace_id="ws_1",
            owner_agent_id="agt_owner",
            display_name="Squad",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/s.git",
            },
            admit="allowlist",
        )
    )
    client = _client(svc)
    resp = client.get("/api/v1/workspaces/ws_1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["workspace_id"] == "ws_1"


def test_close_ok():
    svc = AsyncMock()
    svc.close_workspace = AsyncMock(
        return_value=Workspace(
            workspace_id="ws_1",
            owner_agent_id="agt_owner",
            display_name="Squad",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/s.git",
            },
            admit="allowlist",
            status="closed",
        )
    )
    client = _client(svc)
    resp = client.post("/api/v1/workspaces/ws_1/close")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "closed"


def test_create_conflict_409():
    svc = AsyncMock()
    svc.create_workspace = AsyncMock(
        side_effect=WorkspaceConflictError(
            "task_workspace_active",
            "task already has active workspace ws_1",
        )
    )
    client = _client(svc)
    resp = client.post(
        "/api/v1/workspaces",
        json={
            "display_name": "Yard",
            "execution_env": {
                "kind": "git",
                "uri": "https://github.com/acme/s.git",
            },
            "admit": "task",
            "task_id": "task-001",
        },
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error_code"] == "resource_conflict"
    assert body["details"] == {"reason": "task_workspace_active"}
