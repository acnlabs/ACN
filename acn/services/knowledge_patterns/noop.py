"""``plugins.knowledge=noop`` — Org declares no shared knowledge base."""

from __future__ import annotations

from typing import Any

from ...core.interfaces.org_knowledge import IOrgKnowledge


class NoopKnowledge(IOrgKnowledge):
    def plugin_id(self) -> str:
        return "noop"

    def enabled(self) -> bool:
        return False

    def default_refs(self, org_id: str) -> list[dict[str, Any]]:
        return []
