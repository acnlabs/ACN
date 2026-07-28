"""Org Harness work-pattern plugins (Phase 2a)."""

from __future__ import annotations

from ...core.interfaces.work_pattern import IWorkPattern
from .builtin import BuiltinWorkPattern
from .registry import (
    DEFAULT_ORG_PLUGINS,
    canonicalize_knowledge_plugin,
    canonicalize_work_plugin,
    normalize_org_plugins,
    resolve_work_pattern,
    validate_org_plugins,
)

__all__ = [
    "BuiltinWorkPattern",
    "DEFAULT_ORG_PLUGINS",
    "IWorkPattern",
    "canonicalize_knowledge_plugin",
    "canonicalize_work_plugin",
    "normalize_org_plugins",
    "resolve_work_pattern",
    "validate_org_plugins",
]
