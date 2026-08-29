"""Org.execution_env pointer (shared workplace; Kernel stores only)."""

from __future__ import annotations

import pytest

from acn.core.entities.org import Org, OrgPrincipal, normalize_execution_env


def test_normalize_none_and_kind_none() -> None:
    assert normalize_execution_env(None) is None
    assert normalize_execution_env({"kind": "none"}) is None
    assert normalize_execution_env({}) is None


def test_normalize_git_and_url() -> None:
    git = normalize_execution_env(
        {
            "kind": "git",
            "uri": "https://github.com/acme/squad.git",
            "hint": "work on main",
        }
    )
    assert git == {
        "kind": "git",
        "uri": "https://github.com/acme/squad.git",
        "hint": "work on main",
    }
    url = normalize_execution_env(
        {"kind": "url", "uri": "https://runner.example/v1"}
    )
    assert url == {"kind": "url", "uri": "https://runner.example/v1"}


def test_normalize_rejects_bad_shape() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        normalize_execution_env("git")
    with pytest.raises(ValueError, match="invalid execution_env.kind"):
        normalize_execution_env({"kind": "sandbox"})
    with pytest.raises(ValueError, match="uri is required"):
        normalize_execution_env({"kind": "git"})
    with pytest.raises(ValueError, match="http"):
        normalize_execution_env({"kind": "git", "uri": "file:///tmp/repo"})


def test_org_round_trip_includes_execution_env() -> None:
    org = Org(
        org_id="org_a",
        display_name="A",
        created_by=OrgPrincipal(kind="agent", subject="agt_s"),
        subnet_id="fence-a",
        steward_agent_id="agt_s",
        execution_env={"kind": "git", "uri": "https://example.com/r.git"},
    )
    d = org.to_dict()
    assert d["execution_env"]["kind"] == "git"
    back = Org.from_dict(d)
    assert back.execution_env == org.execution_env


def test_org_legacy_dict_without_execution_env() -> None:
    org = Org.from_dict(
        {
            "org_id": "org_a",
            "display_name": "A",
            "created_by": {"kind": "agent", "subject": "agt_s"},
            "subnet_id": "fence-a",
            "steward_agent_id": "agt_s",
        }
    )
    assert org.execution_env is None
    assert org.to_dict()["execution_env"] == {"kind": "none"}
