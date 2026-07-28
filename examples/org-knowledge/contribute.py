"""Org knowledge contribute (write path) — agent proposals with minimal governance.

K4: members may auto-land under sop|skills|playbooks|wiki|sources.
charter.md (and charter/) require --as-owner.
Conflicts (existing different content, no --force) → disputed/<path>.

Trust: this sidecar does not call ACN Membership. The caller asserts
from_agent / as_owner; gate that in the orchestrator or runner in production.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from kb import _safe_file, max_file_bytes, org_dir

_AGENT_ZONE_PREFIXES = (
    "sop/",
    "skills/",
    "playbooks/",
    "wiki/",
    "sources/",
)
_CHARTER_NAMES = frozenset({"charter.md"})
_CHARTER_PREFIXES = ("charter/",)
_MD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+/-]*\.md$")


class ContributeDecision(StrEnum):
    ACCEPTED = "accepted"
    DISPUTED = "disputed"
    REJECTED = "rejected"
    NOOP = "noop"  # identical content already present


@dataclass(frozen=True)
class ContributeProposal:
    org_id: str
    path: str  # relative within org tree, e.g. sop/foo.md
    body: str
    from_agent: str
    work_id: str = ""
    title: str = ""
    as_owner: bool = False
    force: bool = False


@dataclass(frozen=True)
class ContributeResult:
    decision: ContributeDecision
    path: str
    abs_path: str
    reason: str = ""


def normalize_rel_path(path: str) -> str:
    rel = (path or "").strip().replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        raise ValueError(f"invalid path: {path!r}")
    if not _MD_NAME.match(rel):
        raise ValueError(
            f"path must be a safe .md relative path: {path!r}"
        )
    return rel


def is_charter_path(rel: str) -> bool:
    if rel in _CHARTER_NAMES:
        return True
    return any(rel.startswith(p) for p in _CHARTER_PREFIXES)


def is_agent_zone(rel: str) -> bool:
    return any(rel.startswith(p) for p in _AGENT_ZONE_PREFIXES)


def classify_write(rel: str, *, as_owner: bool) -> tuple[bool, str]:
    """Return (allowed, reason)."""
    if is_charter_path(rel):
        if as_owner:
            return True, "owner charter write"
        return False, "charter requires --as-owner / Owner role"
    if is_agent_zone(rel):
        return True, "member zone auto-accept"
    if as_owner:
        return True, "owner unrestricted (within org tree)"
    return (
        False,
        "path not in agent zones (sop|skills|playbooks|wiki|sources); "
        "use Owner or move under an allowed prefix",
    )


def _provenance_footer(prop: ContributeProposal) -> str:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [
        "",
        "---",
        f"<!-- orgkb:contribute agent={prop.from_agent}"
        + (f" work={prop.work_id}" if prop.work_id else "")
        + f" at={ts} -->",
    ]
    if prop.title:
        parts.insert(2, f"<!-- title: {prop.title} -->")
    return "\n".join(parts) + "\n"


def prepare_body(prop: ContributeProposal) -> str:
    body = prop.body if prop.body.endswith("\n") else prop.body + "\n"
    # Avoid duplicating footer if caller already added one.
    if "<!-- orgkb:contribute" in body:
        return body
    return body + _provenance_footer(prop)


def contribute(
    prop: ContributeProposal,
    *,
    root: Path | None = None,
) -> ContributeResult:
    import os

    # K3: runner may mirror Org plugins.knowledge=noop to block writes.
    knowledge = (os.environ.get("ORG_PLUGINS_KNOWLEDGE") or "").strip().lower()
    if knowledge == "noop":
        return ContributeResult(
            decision=ContributeDecision.REJECTED,
            path=(prop.path or "").strip(),
            abs_path="",
            reason="knowledge_plugin_noop",
        )

    if not prop.from_agent.strip():
        raise ValueError("from_agent required")
    rel = normalize_rel_path(prop.path)
    allowed, why = classify_write(rel, as_owner=prop.as_owner)
    if not allowed:
        return ContributeResult(
            decision=ContributeDecision.REJECTED,
            path=rel,
            abs_path="",
            reason=why,
        )

    text = prepare_body(prop)
    limit = max_file_bytes()
    if len(text.encode("utf-8")) > limit:
        return ContributeResult(
            decision=ContributeDecision.REJECTED,
            path=rel,
            abs_path="",
            reason=f"body too large (>{limit} bytes)",
        )

    target = _safe_file(prop.org_id, rel, root=root)
    odir = org_dir(prop.org_id, root=root)

    if target.is_file() and not prop.force:
        existing = target.read_text(encoding="utf-8")
        # Compare without provenance footers for noop detection.
        if _strip_footer(existing) == _strip_footer(text):
            return ContributeResult(
                decision=ContributeDecision.NOOP,
                path=rel,
                abs_path=str(target),
                reason="identical content",
            )
        # Conflict → disputed/
        disputed_rel = f"disputed/{rel}"
        disputed = _safe_file(prop.org_id, disputed_rel, root=root)
        disputed.parent.mkdir(parents=True, exist_ok=True)
        # Unique name if disputed file already exists.
        if disputed.is_file():
            stem = Path(rel).stem
            suffix = Path(rel).suffix
            parent = str(Path(rel).parent).replace("\\", "/")
            if parent == ".":
                parent = ""
            ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            disputed_rel = (
                f"disputed/{parent}/{stem}.{ts}{suffix}"
                if parent
                else f"disputed/{stem}.{ts}{suffix}"
            )
            disputed = _safe_file(prop.org_id, disputed_rel, root=root)
            disputed.parent.mkdir(parents=True, exist_ok=True)
        disputed.write_text(text, encoding="utf-8")
        return ContributeResult(
            decision=ContributeDecision.DISPUTED,
            path=disputed_rel,
            abs_path=str(disputed),
            reason=f"conflict with existing {rel}; wrote disputed copy",
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    # Ensure org root exists.
    odir.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return ContributeResult(
        decision=ContributeDecision.ACCEPTED,
        path=rel,
        abs_path=str(target),
        reason=why,
    )


def _strip_footer(text: str) -> str:
    marker = "\n---\n<!-- orgkb:contribute"
    idx = text.find(marker)
    if idx >= 0:
        return text[:idx].rstrip() + "\n"
    return text
