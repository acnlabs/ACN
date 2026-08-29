"""Execution Workspace entity + Org execution_env.workspace_id."""

from __future__ import annotations

import pytest

from acn.core.entities.org import Org, OrgPrincipal, normalize_execution_env
from acn.core.entities.workspace import (
    Workspace,
    WorkspaceAttestation,
    normalize_workspace_execution_env,
)


def test_normalize_keeps_workspace_id() -> None:
    env = normalize_execution_env(
        {
            "kind": "git",
            "uri": "https://github.com/acme/squad.git",
            "workspace_id": "ws_abc",
        }
    )
    assert env is not None
    assert env["workspace_id"] == "ws_abc"


def test_normalize_rejects_bad_workspace_id() -> None:
    with pytest.raises(ValueError, match="workspace_id"):
        normalize_execution_env(
            {
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
                "workspace_id": "not-a-ws",
            }
        )


def test_workspace_env_strips_nested_id() -> None:
    env = normalize_workspace_execution_env(
        {
            "kind": "git",
            "uri": "https://github.com/acme/squad.git",
            "workspace_id": "ws_nested",
        }
    )
    assert "workspace_id" not in env


def test_workspace_round_trip() -> None:
    ws = Workspace(
        workspace_id="ws_1",
        owner_agent_id="agt_owner",
        display_name="Squad",
        execution_env={"kind": "git", "uri": "https://github.com/acme/squad.git"},
        admit="allowlist",
        allowlist=["agt_a"],
    )
    back = Workspace.from_dict(ws.to_dict())
    assert back.owner_agent_id == "agt_owner"
    assert back.allowlist == ["agt_a"]


def test_workspace_org_admit_requires_org_id() -> None:
    with pytest.raises(ValueError, match="org_id"):
        Workspace(
            workspace_id="ws_1",
            owner_agent_id="agt_owner",
            display_name="Squad",
            execution_env={"kind": "git", "uri": "https://github.com/acme/squad.git"},
            admit="org",
        )


def test_attestation_kind() -> None:
    att = WorkspaceAttestation(
        attestation_id="att_1",
        workspace_id="ws_1",
        agent_id="agt_worker",
        run_id="run-1",
        artifact={"git_sha": "abc"},
    )
    assert att.to_dict()["kind"] == "workspace_owner"


def test_org_round_trip_workspace_id() -> None:
    org = Org(
        org_id="org_a",
        display_name="A",
        created_by=OrgPrincipal(kind="agent", subject="agt_s"),
        subnet_id="fence-a",
        steward_agent_id="agt_s",
        execution_env={
            "kind": "git",
            "uri": "https://example.com/r.git",
            "workspace_id": "ws_abc",
        },
    )
    assert org.to_dict()["execution_env"]["workspace_id"] == "ws_abc"
    assert Org.from_dict(org.to_dict()).execution_env["workspace_id"] == "ws_abc"
