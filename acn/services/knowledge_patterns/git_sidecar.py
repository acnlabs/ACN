"""``plugins.knowledge=git`` — enable filesystem/git sidecar (examples/org-knowledge).

In-process ACN does not host the vault; runners call ``read_kb`` / ``contribute_kb``.
This adapter only signals that the Org opted into the sidecar contract.
"""

from __future__ import annotations

from typing import Any

from ...core.interfaces.org_knowledge import IOrgKnowledge


class GitSidecarKnowledge(IOrgKnowledge):
    def plugin_id(self) -> str:
        return "git"

    def enabled(self) -> bool:
        return True

    def default_refs(self, org_id: str) -> list[dict[str, Any]]:
        oid = (org_id or "").strip()
        if not oid:
            return []
        return [{"uri": f"orgkb://{oid}/charter.md", "title": "charter.md"}]
