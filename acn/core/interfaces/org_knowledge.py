"""IOrgKnowledge — Org shared knowledge Port (not Kernel).

K3 wires ``plugins.knowledge`` to ``noop`` | ``git``. Content remains in an
external sidecar (see ``examples/org-knowledge/``); this ABC documents the
Port surface for future in-process adapters.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class IOrgKnowledge(ABC):
    """Organization knowledge Port — agent-authored shared assets."""

    @abstractmethod
    def plugin_id(self) -> str:
        """Canonical plugin id (``noop`` or ``git``)."""

    @abstractmethod
    def enabled(self) -> bool:
        """False for ``noop`` — wake/contribute should skip the sidecar."""

    def default_refs(self, org_id: str) -> list[dict[str, Any]]:
        """Optional default ``kb_refs`` when work has none. Empty if disabled."""
        return []
