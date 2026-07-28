"""Resolve ``plugins.knowledge`` ids to ``IOrgKnowledge`` adapters."""

from __future__ import annotations

from ...core.exceptions import OrgConflictError
from ...core.interfaces.org_knowledge import IOrgKnowledge
from .git_sidecar import GitSidecarKnowledge
from .noop import NoopKnowledge

_KNOWN = frozenset({"noop", "git"})


def resolve_knowledge_plugin(plugin_id: str) -> IOrgKnowledge:
    pid = (plugin_id or "noop").strip() or "noop"
    if pid == "noop":
        return NoopKnowledge()
    if pid == "git":
        return GitSidecarKnowledge()
    raise OrgConflictError(
        "unknown_plugin",
        f"unknown knowledge plugin: {plugin_id!r}",
    )


def knowledge_enabled(plugins: dict[str, str] | None) -> bool:
    """True when Org ``plugins.knowledge`` is an enabled adapter (today: ``git``)."""
    if not plugins:
        return False
    pid = (plugins.get("knowledge") or "noop").strip() or "noop"
    if pid not in _KNOWN:
        return False
    return resolve_knowledge_plugin(pid).enabled()
