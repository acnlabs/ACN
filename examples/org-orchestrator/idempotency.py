"""File-backed wake idempotency store (flock + reload; disk before memory)."""

from __future__ import annotations

# fcntl is POSIX-only; orchestrator examples target Unix runners (same as smoke).
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any


class IdempotencyStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._data: dict[str, Any] = {"sent": {}}

    def _load_unlocked(self) -> None:
        if not self.path.is_file():
            self._data = {"sent": {}}
            return
        try:
            raw = json.loads(self.path.read_text())
            if isinstance(raw, dict) and isinstance(raw.get("sent"), dict):
                self._data = raw
            else:
                self._data = {"sent": {}}
        except (OSError, json.JSONDecodeError):
            self._data = {"sent": {}}

    def _save_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(self._data, indent=2, sort_keys=True) + "\n"
        tmp.write_text(payload)
        os.replace(tmp, self.path)

    def _with_lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        return open(self.lock_path, "a+", encoding="utf-8")

    def has(self, key: str) -> bool:
        with self._with_lock() as lockf:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
            try:
                self._load_unlocked()
                return key in self._data["sent"]
            finally:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)

    def mark(self, key: str, *, work_id: str, assignee: str) -> None:
        """Persist then update memory. Raises OSError if disk write fails."""
        entry = {
            "work_id": work_id,
            "assignee": assignee,
            "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with self._with_lock() as lockf:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
            try:
                self._load_unlocked()
                # Copy-on-write style: mutate a working dict, save, then adopt.
                working = {
                    "sent": dict(self._data.get("sent") or {}),
                }
                working["sent"][key] = entry
                prev = self._data
                self._data = working
                try:
                    self._save_unlocked()
                except OSError:
                    self._data = prev
                    raise
            finally:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)

    def try_claim(self, key: str, *, work_id: str, assignee: str) -> bool:
        """Atomically claim key if absent. True = caller should send; False = skip.

        Claims before send to reduce multi-instance double-send. On send failure,
        caller should ``release`` so a later tick can retry.
        """
        entry = {
            "work_id": work_id,
            "assignee": assignee,
            "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "pending": True,
        }
        with self._with_lock() as lockf:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
            try:
                self._load_unlocked()
                if key in self._data["sent"]:
                    return False
                working = {"sent": dict(self._data.get("sent") or {})}
                working["sent"][key] = entry
                prev = self._data
                self._data = working
                try:
                    self._save_unlocked()
                except OSError:
                    self._data = prev
                    raise
                return True
            finally:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)

    def confirm(self, key: str) -> None:
        """Clear pending after successful send."""
        with self._with_lock() as lockf:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
            try:
                self._load_unlocked()
                row = self._data.get("sent", {}).get(key)
                if not isinstance(row, dict):
                    return
                working = {"sent": dict(self._data.get("sent") or {})}
                working["sent"][key] = {**row, "pending": False}
                prev = self._data
                self._data = working
                try:
                    self._save_unlocked()
                except OSError:
                    self._data = prev
                    raise
            finally:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)

    def release(self, key: str) -> None:
        """Drop claim after failed send so another tick can retry."""
        with self._with_lock() as lockf:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
            try:
                self._load_unlocked()
                if key not in self._data.get("sent", {}):
                    return
                working = {"sent": dict(self._data.get("sent") or {})}
                working["sent"].pop(key, None)
                prev = self._data
                self._data = working
                try:
                    self._save_unlocked()
                except OSError:
                    self._data = prev
                    raise
            finally:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
