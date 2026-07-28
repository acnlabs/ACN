"""Org knowledge plugins (K3) — ``plugins.knowledge`` = noop | git."""

from __future__ import annotations

from ...core.interfaces.org_knowledge import IOrgKnowledge
from .git_sidecar import GitSidecarKnowledge
from .noop import NoopKnowledge
from .resolve import resolve_knowledge_plugin

__all__ = [
    "GitSidecarKnowledge",
    "IOrgKnowledge",
    "NoopKnowledge",
    "resolve_knowledge_plugin",
]
