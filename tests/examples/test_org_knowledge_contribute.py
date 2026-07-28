"""Tests for examples/org-knowledge contribute (K4)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

EX_DIR = Path(__file__).resolve().parents[2] / "examples" / "org-knowledge"
sys.path.insert(0, str(EX_DIR))

from contribute import (  # noqa: E402
    ContributeDecision,
    ContributeProposal,
    contribute,
    normalize_rel_path,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path


def test_member_accepts_sop(root: Path) -> None:
    r = contribute(
        ContributeProposal(
            org_id="org_x",
            path="sop/tip.md",
            body="# tip\n\nDo X.\n",
            from_agent="agt_1",
            work_id="work_1",
        ),
        root=root,
    )
    assert r.decision == ContributeDecision.ACCEPTED
    text = Path(r.abs_path).read_text(encoding="utf-8")
    assert "Do X." in text
    assert "orgkb:contribute" in text
    assert "agt_1" in text


def test_knowledge_plugin_noop_rejects_write(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORG_PLUGINS_KNOWLEDGE", "noop")
    r = contribute(
        ContributeProposal(
            org_id="org_x",
            path="sop/tip.md",
            body="# tip\n",
            from_agent="agt_1",
        ),
        root=root,
    )
    assert r.decision == ContributeDecision.REJECTED
    assert r.reason == "knowledge_plugin_noop"


def test_member_rejected_on_charter(root: Path) -> None:
    r = contribute(
        ContributeProposal(
            org_id="org_x",
            path="charter.md",
            body="# bad\n",
            from_agent="agt_1",
        ),
        root=root,
    )
    assert r.decision == ContributeDecision.REJECTED


def test_owner_can_write_charter(root: Path) -> None:
    r = contribute(
        ContributeProposal(
            org_id="org_x",
            path="charter.md",
            body="# Charter\n",
            from_agent="agt_owner",
            as_owner=True,
        ),
        root=root,
    )
    assert r.decision == ContributeDecision.ACCEPTED


def test_conflict_goes_to_disputed(root: Path) -> None:
    p = ContributeProposal(
        org_id="org_x",
        path="sop/a.md",
        body="# v1\n",
        from_agent="agt_1",
    )
    assert contribute(p, root=root).decision == ContributeDecision.ACCEPTED
    r2 = contribute(
        ContributeProposal(
            org_id="org_x",
            path="sop/a.md",
            body="# v2\n",
            from_agent="agt_2",
        ),
        root=root,
    )
    assert r2.decision == ContributeDecision.DISPUTED
    assert r2.path.startswith("disputed/")
    assert Path(r2.abs_path).is_file()
    # original preserved
    assert (root / "orgs" / "org_x" / "sop" / "a.md").read_text(
        encoding="utf-8"
    ).startswith("# v1")


def test_force_overwrites(root: Path) -> None:
    contribute(
        ContributeProposal(
            org_id="org_x",
            path="skills/s.md",
            body="# old\n",
            from_agent="agt_1",
        ),
        root=root,
    )
    r = contribute(
        ContributeProposal(
            org_id="org_x",
            path="skills/s.md",
            body="# new\n",
            from_agent="agt_1",
            force=True,
        ),
        root=root,
    )
    assert r.decision == ContributeDecision.ACCEPTED
    assert "# new" in Path(r.abs_path).read_text(encoding="utf-8")


def test_noop_identical(root: Path) -> None:
    body = "# same\n"
    contribute(
        ContributeProposal(
            org_id="org_x",
            path="playbooks/p.md",
            body=body,
            from_agent="agt_1",
        ),
        root=root,
    )
    r = contribute(
        ContributeProposal(
            org_id="org_x",
            path="playbooks/p.md",
            body=body,
            from_agent="agt_1",
        ),
        root=root,
    )
    assert r.decision == ContributeDecision.NOOP


def test_reject_bad_path() -> None:
    with pytest.raises(ValueError):
        normalize_rel_path("../etc/passwd.md")


def test_cli_json(root: Path) -> None:
    payload = {
        "org_id": "org_cli",
        "path": "wiki/note.md",
        "body": "# from json\n",
        "from_agent": "agt_cli",
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(EX_DIR / "contribute_kb.py"),
            "--root",
            str(root),
            "--from-json",
            "-",
            "--json-out",
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "accepted"
