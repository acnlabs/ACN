"""Unit tests for examples/org-knowledge filesystem sidecar."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

EX_DIR = Path(__file__).resolve().parents[2] / "examples" / "org-knowledge"
sys.path.insert(0, str(EX_DIR))

from kb import (  # noqa: E402
    KbRef,
    assert_refs_match_org,
    default_refs_for_org,
    format_bundle,
    org_dir,
    read_ref,
    read_refs,
    resolve_orgkb_uri,
)


@pytest.fixture
def root() -> Path:
    return EX_DIR / "data"


def test_resolve_orgkb_and_read_charter(root: Path) -> None:
    org_id, path = resolve_orgkb_uri("orgkb://org_demo/charter.md", root=root)
    assert org_id == "org_demo"
    assert path.is_file()
    pairs = read_refs([KbRef(uri="orgkb://org_demo/charter.md")], root=root)
    assert "Charter" in pairs[0][1]


def test_resolve_path_form_without_netloc(root: Path) -> None:
    org_id, path = resolve_orgkb_uri("orgkb:/org_demo/sop/release.md", root=root)
    assert org_id == "org_demo"
    assert path.name == "release.md"


def test_reject_path_traversal(root: Path) -> None:
    with pytest.raises(ValueError, match="escapes"):
        resolve_orgkb_uri("orgkb://org_demo/../../etc/passwd", root=root)


def test_reject_bad_org_id(root: Path) -> None:
    with pytest.raises(ValueError, match="invalid org_id"):
        org_dir("../evil", root=root)


def test_reject_cross_org_refs(root: Path) -> None:
    with pytest.raises(ValueError, match="!= expected"):
        assert_refs_match_org(
            [KbRef(uri="orgkb://org_other/charter.md")],
            "org_demo",
            root=root,
        )


def test_file_too_large(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    odir = tmp_path / "orgs" / "org_big"
    odir.mkdir(parents=True)
    big = odir / "huge.md"
    big.write_bytes(b"x" * 1000)
    monkeypatch.setenv("ORG_KB_MAX_FILE_BYTES", "100")
    with pytest.raises(ValueError, match="too large"):
        read_ref(KbRef(uri="orgkb://org_big/huge.md"), root=tmp_path)


def test_default_refs_and_bundle(root: Path) -> None:
    refs = default_refs_for_org("org_demo")
    pairs = read_refs(refs, root=root, expected_org_id="org_demo")
    bundle = format_bundle(pairs, max_chars=500)
    assert "charter" in bundle.lower() or "Charter" in bundle


def test_read_kb_cli(root: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(EX_DIR / "read_kb.py"),
            "--root",
            str(root),
            "--org",
            "org_demo",
            "--json-out",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data and "text" in data[0]


def test_read_kb_cli_rejects_cross_org(root: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(EX_DIR / "read_kb.py"),
            "--root",
            str(root),
            "--org",
            "org_demo",
            "--ref",
            "orgkb://org_other/charter.md",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "ORG_KB_ROOT": str(root)},
    )
    assert proc.returncode == 1
    assert "expected" in proc.stderr or "org_id" in proc.stderr


def test_read_kb_cli_from_json_stdin(root: Path) -> None:
    payload = json.dumps(
        {
            "kb_refs": [
                {"uri": "orgkb://org_demo/sop/release.md", "title": "发版"},
            ]
        }
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(EX_DIR / "read_kb.py"),
            "--root",
            str(root),
            "--org",
            "org_demo",
            "--from-json",
            "-",
        ],
        check=False,
        input=payload,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Release" in proc.stdout or "SOP" in proc.stdout
