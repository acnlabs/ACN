"""Execution Workspace SDK surface — doorplate, not a sandbox."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from acn_client.client import ACNClient
from acn_client.models import (
    WorkspaceAttestationCreateRequest,
    WorkspaceCreateRequest,
)


def _make_client_with_stub(request_mock: AsyncMock) -> ACNClient:
    client = ACNClient(base_url="http://acn.test")
    client._request = request_mock  # type: ignore[method-assign]
    return client


@pytest.mark.asyncio
async def test_create_get_close_workspace_paths():
    payload: dict[str, Any] = {
        "workspace_id": "ws_1",
        "owner_agent_id": "agt_a",
        "display_name": "yard",
        "admit": "org",
        "org_id": "org_1",
        "status": "active",
        "execution_env": {"kind": "git", "uri": "https://github.com/acme/squad.git"},
    }
    request_mock = AsyncMock(return_value=payload)
    client = _make_client_with_stub(request_mock)

    created = await client.create_workspace(
        WorkspaceCreateRequest(
            display_name="yard",
            execution_env={"kind": "git", "uri": "https://github.com/acme/squad.git"},
            admit="org",
            org_id="org_1",
        )
    )
    assert created.workspace_id == "ws_1"
    request_mock.assert_awaited_with(
        "POST",
        "/api/v1/workspaces",
        json={
            "display_name": "yard",
            "execution_env": {
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            "admit": "org",
            "org_id": "org_1",
        },
    )

    await client.get_workspace("ws_1")
    request_mock.assert_awaited_with("GET", "/api/v1/workspaces/ws_1")

    await client.close_workspace("ws_1")
    request_mock.assert_awaited_with(
        "POST",
        "/api/v1/workspaces/ws_1/close",
        json={},
    )


@pytest.mark.asyncio
async def test_create_and_get_attestation():
    att = {
        "attestation_id": "att_1",
        "kind": "workspace_owner",
        "workspace_id": "ws_1",
        "agent_id": "agt_b",
        "run_id": "run-9",
        "artifact": {"git_sha": "deadbeef"},
    }
    request_mock = AsyncMock(return_value=att)
    client = _make_client_with_stub(request_mock)

    created = await client.create_workspace_attestation(
        "ws_1",
        WorkspaceAttestationCreateRequest(
            agent_id="agt_b",
            run_id="run-9",
            task_id="task_1",
            artifact={"git_sha": "deadbeef"},
        ),
    )
    assert created.kind == "workspace_owner"
    request_mock.assert_awaited_with(
        "POST",
        "/api/v1/workspaces/ws_1/attestations",
        json={
            "agent_id": "agt_b",
            "run_id": "run-9",
            "task_id": "task_1",
            "artifact": {"git_sha": "deadbeef"},
        },
    )

    await client.get_workspace_attestation("ws_1", "att_1")
    request_mock.assert_awaited_with(
        "GET",
        "/api/v1/workspaces/ws_1/attestations/att_1",
    )
