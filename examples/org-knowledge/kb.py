"""Org knowledge base (filesystem sidecar) — resolve orgkb:// and read paths.

Layout (under ORG_KB_ROOT, default: alongside this package):

  orgs/<org_id>/
    charter.md
    sop/
    playbooks/
    skills/

Trust boundary: this sidecar does **not** call ACN Membership. Anyone who can
run the process and read ORG_KB_ROOT can read any org tree under it. Do not put
multiple tenants' trees on one root shared with untrusted runners.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

ORGKB_SCHEME = "orgkb"
DEFAULT_ENTRYPOINTS = ("charter.md",)
# Reject whole-file reads above this (bytes). Override with ORG_KB_MAX_FILE_BYTES.
DEFAULT_MAX_FILE_BYTES = 512_000

_ORG_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class KbRef:
    """One knowledge pointer (from wake/work or CLI)."""

    uri: str
    title: str = ""

    @classmethod
    def from_mapping(cls, raw: dict) -> KbRef:
        uri = str(raw.get("uri") or "").strip()
        if not uri:
            raise ValueError("kb_ref missing uri")
        return cls(uri=uri, title=str(raw.get("title") or "").strip())


def max_file_bytes() -> int:
    raw = os.environ.get("ORG_KB_MAX_FILE_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_FILE_BYTES
    n = int(raw)
    if n < 1:
        raise ValueError("ORG_KB_MAX_FILE_BYTES must be >= 1")
    return n


def default_kb_root() -> Path:
    env = os.environ.get("ORG_KB_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return (Path(__file__).resolve().parent / "data").resolve()


def org_dir(org_id: str, root: Path | None = None) -> Path:
    if not _ORG_ID_RE.match(org_id or ""):
        raise ValueError(f"invalid org_id: {org_id!r}")
    base = (root or default_kb_root()).resolve()
    return (base / "orgs" / org_id).resolve()


def resolve_orgkb_uri(uri: str, *, root: Path | None = None) -> tuple[str, Path]:
    """Return (org_id, absolute file path) for orgkb://org_id/rel/path.

    Rejects path traversal outside that org's tree (including symlinks out).
    """
    raw = (uri or "").strip()
    if not raw:
        raise ValueError("empty uri")

    # Bare relative paths when ORG_KB_ORG_ID is set (CLI convenience).
    if not raw.startswith(f"{ORGKB_SCHEME}:"):
        org_id = os.environ.get("ORG_KB_ORG_ID", "").strip()
        if not org_id:
            raise ValueError(
                f"relative path {raw!r} needs orgkb://… or ORG_KB_ORG_ID"
            )
        return org_id, _safe_file(org_id, raw, root=root)

    # Accept orgkb://org/path, orgkb:/org/path, orgkb:///org/path
    normalized = raw if "://" in raw else raw.replace("orgkb:", "orgkb://", 1)
    parsed = urlparse(normalized)
    if parsed.scheme != ORGKB_SCHEME:
        raise ValueError(f"unsupported scheme: {parsed.scheme!r} (want orgkb)")
    org_id = unquote(parsed.netloc or "").strip()
    rel = unquote(parsed.path or "").lstrip("/")
    if not org_id:
        path_parts = [p for p in rel.split("/") if p]
        if len(path_parts) < 2:
            raise ValueError(f"orgkb uri missing org_id: {raw!r}")
        org_id, rel = path_parts[0], "/".join(path_parts[1:])
    if not rel:
        raise ValueError(f"orgkb uri missing path: {raw!r}")
    return org_id, _safe_file(org_id, rel, root=root)


def _safe_file(org_id: str, rel: str, *, root: Path | None = None) -> Path:
    odir = org_dir(org_id, root=root)
    candidate = (odir / rel).resolve()
    try:
        candidate.relative_to(odir)
    except ValueError as e:
        raise ValueError(f"path escapes org tree: {rel!r}") from e
    return candidate


def assert_refs_match_org(
    refs: Iterable[KbRef | str | dict],
    expected_org_id: str,
    *,
    root: Path | None = None,
) -> None:
    """Reject any ref whose URI resolves to a different org_id."""
    if not expected_org_id:
        raise ValueError("expected_org_id required")
    for item in refs:
        if isinstance(item, dict):
            ref = KbRef.from_mapping(item)
        elif isinstance(item, KbRef):
            ref = item
        else:
            ref = KbRef(uri=str(item))
        org_id, _path = resolve_orgkb_uri(ref.uri, root=root)
        if org_id != expected_org_id:
            raise ValueError(
                f"kb_ref org_id {org_id!r} != expected {expected_org_id!r} ({ref.uri})"
            )


def read_ref(
    ref: KbRef | str,
    *,
    root: Path | None = None,
    max_bytes: int | None = None,
) -> tuple[KbRef, str]:
    kb_ref = ref if isinstance(ref, KbRef) else KbRef(uri=str(ref))
    _org_id, path = resolve_orgkb_uri(kb_ref.uri, root=root)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    limit = max_file_bytes() if max_bytes is None else max_bytes
    size = path.stat().st_size
    if size > limit:
        raise ValueError(
            f"file too large ({size} bytes > {limit}): {path}"
        )
    text = path.read_text(encoding="utf-8")
    return kb_ref, text


def read_refs(
    refs: Iterable[KbRef | str | dict],
    *,
    root: Path | None = None,
    expected_org_id: str | None = None,
    max_bytes: int | None = None,
) -> list[tuple[KbRef, str]]:
    items = list(refs)
    if expected_org_id:
        assert_refs_match_org(items, expected_org_id, root=root)
    out: list[tuple[KbRef, str]] = []
    for item in items:
        if isinstance(item, dict):
            ref = KbRef.from_mapping(item)
        elif isinstance(item, KbRef):
            ref = item
        else:
            ref = KbRef(uri=str(item))
        out.append(read_ref(ref, root=root, max_bytes=max_bytes))
    return out


def default_refs_for_org(org_id: str) -> list[KbRef]:
    """Minimal defaults when wake/work has no kb_refs."""
    return [
        KbRef(uri=f"orgkb://{org_id}/{name}", title=name)
        for name in DEFAULT_ENTRYPOINTS
    ]


def default_refs_as_dicts(org_id: str) -> list[dict]:
    return [{"uri": r.uri, "title": r.title} for r in default_refs_for_org(org_id)]


def format_bundle(pairs: list[tuple[KbRef, str]], *, max_chars: int = 24_000) -> str:
    """Concatenate refs for L1 prompt injection; truncate fairly if huge."""
    parts: list[str] = []
    used = 0
    for ref, body in pairs:
        header = f"### {ref.title or ref.uri}\n"
        chunk = header + body.rstrip() + "\n"
        if used + len(chunk) > max_chars and parts:
            parts.append(
                f"\n… truncated ({len(pairs) - len(parts)} more ref(s) omitted)\n"
            )
            break
        if used + len(chunk) > max_chars:
            remain = max_chars - used - len(header) - 20
            chunk = header + body[: max(0, remain)] + "\n… truncated\n"
            parts.append(chunk)
            break
        parts.append(chunk)
        used += len(chunk)
    return "\n".join(parts)
