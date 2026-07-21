"""Resolve ``org.plugins.work`` / normalize plugin maps (Phase 2a)."""

from __future__ import annotations

from ...core.exceptions import OrgConflictError
from ...core.interfaces.org_repository import IOrgRepository
from ...core.interfaces.work_pattern import IWorkPattern
from .builtin import BuiltinWorkPattern

# Canonical defaults (phase2-work-port-v0).
DEFAULT_ORG_PLUGINS: dict[str, str] = {
    "work": "builtin_work",
    "loop": "heartbeat",
    "memory": "noop",
}

# Legacy Phase 1 ids still accepted when reading stored Orgs.
_WORK_ALIASES: dict[str, str] = {
    "minimal": "builtin_work",
    "builtin_work": "builtin_work",
}

_LOOP_ALIASES: dict[str, str] = {
    "thin": "heartbeat",
    "heartbeat": "heartbeat",
}

# Implemented work plugins in this release.
_WORK_IMPLEMENTED: frozenset[str] = frozenset({"builtin_work"})

# Known but not yet wired (P2b+) — rejected with a clear reason.
_WORK_KNOWN_UNAVAILABLE: frozenset[str] = frozenset({"task_pool", "paperclip"})

_LOOP_KNOWN: frozenset[str] = frozenset({"heartbeat"})
_MEMORY_KNOWN: frozenset[str] = frozenset({"noop"})


def canonicalize_work_plugin(plugin_id: str) -> str:
    return _WORK_ALIASES.get(plugin_id, plugin_id)


def canonicalize_loop_plugin(plugin_id: str) -> str:
    return _LOOP_ALIASES.get(plugin_id, plugin_id)


def normalize_org_plugins(plugins: dict[str, str] | None) -> dict[str, str]:
    """Merge with defaults and canonicalize known aliases."""
    merged = dict(DEFAULT_ORG_PLUGINS)
    if plugins:
        merged.update(plugins)
    merged["work"] = canonicalize_work_plugin(merged.get("work", "builtin_work"))
    merged["loop"] = canonicalize_loop_plugin(merged.get("loop", "heartbeat"))
    if "memory" not in merged or not merged["memory"]:
        merged["memory"] = "noop"
    return merged


def validate_org_plugins(plugins: dict[str, str]) -> None:
    """Raise ``OrgConflictError`` for unknown / unavailable plugin ids."""
    work = canonicalize_work_plugin(plugins.get("work", "builtin_work"))
    if work in _WORK_IMPLEMENTED:
        pass
    elif work in _WORK_KNOWN_UNAVAILABLE:
        raise OrgConflictError(
            "plugin_unavailable",
            f"work plugin '{work}' is not available yet (Phase 2b+)",
        )
    else:
        raise OrgConflictError(
            "unknown_plugin",
            f"unknown work plugin: {plugins.get('work')!r}",
        )

    loop = canonicalize_loop_plugin(plugins.get("loop", "heartbeat"))
    if loop not in _LOOP_KNOWN:
        raise OrgConflictError(
            "unknown_plugin",
            f"unknown loop plugin: {plugins.get('loop')!r}",
        )

    memory = plugins.get("memory", "noop")
    if memory not in _MEMORY_KNOWN:
        raise OrgConflictError(
            "unknown_plugin",
            f"unknown memory plugin: {memory!r}",
        )


def resolve_work_pattern(
    plugin_id: str,
    repository: IOrgRepository,
) -> IWorkPattern:
    """Return an ``IWorkPattern`` for ``plugin_id`` (after alias canonicalize)."""
    canonical = canonicalize_work_plugin(plugin_id)
    if canonical == "builtin_work":
        return BuiltinWorkPattern(repository)
    if canonical in _WORK_KNOWN_UNAVAILABLE:
        raise OrgConflictError(
            "plugin_unavailable",
            f"work plugin '{canonical}' is not available yet (Phase 2b+)",
        )
    raise OrgConflictError(
        "unknown_plugin",
        f"unknown work plugin: {plugin_id!r}",
    )
