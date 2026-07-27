"""File-backed wake idempotency store."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class IdempotencyStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = {"sent": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text())
            if isinstance(raw, dict) and isinstance(raw.get("sent"), dict):
                self._data = raw
        except (OSError, json.JSONDecodeError):
            self._data = {"sent": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.path)

    def has(self, key: str) -> bool:
        return key in self._data["sent"]

    def mark(self, key: str, *, work_id: str, assignee: str) -> None:
        self._data["sent"][key] = {
            "work_id": work_id,
            "assignee": assignee,
            "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._save()
