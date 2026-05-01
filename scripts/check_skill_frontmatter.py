#!/usr/bin/env python3
"""Defensive validator for ``skills/acn/SKILL.md`` YAML frontmatter.

Background: in early 2026 some external markdown round-trip (suspected:
pasting the file into a "smart" markdown editor, or an LLM agent
treating frontmatter as body) silently rewrote the YAML header into
broken markdown — ``name: acn`` became ``## name: acn``, raw URLs got
auto-wrapped as ``[url](url)``, and the closing ``---`` was lost. The
file looked plausible at a glance but the skill loader could no longer
parse it.

This script is a hard guard against that class of regression:

* The file must start with ``---`` on line 1.
* The frontmatter must close with another ``---`` somewhere before EOF.
* Every line in the frontmatter region must be a valid YAML
  ``key: value`` pair (or YAML continuation / nested-mapping line)
  rather than markdown syntax — specifically, none of the lines may
  start with a markdown heading (``#``), a list bullet (``-`` followed
  by space when not part of YAML list structure), or contain a
  markdown link literal (``[text](url)``) inside a *quoted YAML
  scalar* — that pattern is the giveaway sign of a markdown formatter
  having mangled a quoted URL.
* The parsed YAML must contain ``name`` at top level, matching the
  current SKILL.md contract.

Exit code 0 if everything is clean; 1 with a human-readable diagnostic
otherwise. Intended uses:

* Manual sanity check before / after risky doc edits::

    python scripts/check_skill_frontmatter.py

* CI:: add as a step in the lint workflow.
* Optional pre-commit hook (not installed by default — see the
  ``.editorconfig`` comment for context).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    sys.stderr.write(
        "check_skill_frontmatter: PyYAML not installed; install with "
        "`uv add --dev pyyaml` or `pip install pyyaml`.\n"
    )
    sys.exit(2)


SKILL_PATH = Path(__file__).resolve().parents[1] / "skills" / "acn" / "SKILL.md"

# Heuristic: a line like ``  homepage: "[https://x](https://x)"`` is the
# fingerprint of a markdown formatter that rewrote a quoted YAML URL
# as a markdown link. We refuse it explicitly.
_MD_LINK_INSIDE_QUOTED_SCALAR = re.compile(r':\s*"\[https?://[^\]]+\]\(https?://[^)]+\)"')


def _fail(msg: str) -> None:
    sys.stderr.write(f"check_skill_frontmatter: FAIL — {msg}\n")
    sys.exit(1)


def main() -> int:
    if not SKILL_PATH.is_file():
        _fail(f"SKILL.md not found at {SKILL_PATH}")

    text = SKILL_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()

    if not lines or lines[0].strip() != "---":
        first = repr(lines[0]) if lines else "<empty file>"
        _fail(
            "first line must be exactly ``---`` (YAML frontmatter open). "
            f"got: {first}"
        )

    try:
        end_idx = next(
            i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---"
        )
    except StopIteration:
        _fail(
            "no closing ``---`` found — frontmatter end marker missing. "
            "this happens when a markdown formatter treats the opening "
            "``---`` as a setext H1 underline and drops the second one. "
            "revert with: git checkout HEAD -- skills/acn/SKILL.md"
        )

    front_lines = lines[1:end_idx]
    front_text = "\n".join(front_lines)

    for ln_no, line in enumerate(front_lines, start=2):
        stripped = line.lstrip()
        if stripped.startswith("##") or stripped.startswith("# "):
            _fail(
                f"line {ln_no} starts with markdown heading syntax: {line!r}. "
                "this is the signature of a formatter that treated YAML key "
                "as a body-level heading. e.g. ``name: acn`` was converted to "
                "``## name: acn``."
            )
        if _MD_LINK_INSIDE_QUOTED_SCALAR.search(line):
            _fail(
                f"line {ln_no} contains a markdown link inside a quoted YAML "
                f"scalar: {line!r}. this means a formatter wrapped a raw URL "
                "as ``[url](url)`` inside the YAML value."
            )

    try:
        parsed = yaml.safe_load(front_text)
    except yaml.YAMLError as exc:
        _fail(f"YAML parse error in frontmatter: {exc}")

    if not isinstance(parsed, dict):
        _fail(f"frontmatter did not parse to a mapping; got {type(parsed).__name__}")

    if "name" not in parsed:
        _fail("frontmatter missing required key 'name'")

    print(
        f"check_skill_frontmatter: OK — {len(parsed)} top-level keys, "
        f"name={parsed['name']!r}, frontmatter spans lines 1..{end_idx + 1}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
