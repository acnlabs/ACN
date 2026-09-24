"""On-disk blob bytes. Redis holds metadata; this holds the file."""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path

_BLOB_FILE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class FilesystemBlobStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, blob_id: str) -> Path:
        if "/" in blob_id or "\\" in blob_id or blob_id != Path(blob_id).name:
            raise ValueError("invalid blob_id")
        return self.root / blob_id[:2] / blob_id

    async def put(self, blob_id: str, data: bytes) -> None:
        path = self._path(blob_id)
        await asyncio.to_thread(self._write, path, data)

    async def get(self, blob_id: str) -> bytes | None:
        path = self._path(blob_id)
        return await asyncio.to_thread(self._read, path)

    async def delete(self, blob_id: str) -> None:
        path = self._path(blob_id)
        await asyncio.to_thread(self._unlink, path)

    async def list_ids(self) -> list[str]:
        return await asyncio.to_thread(self._list_ids)

    def _list_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        ids: list[str] = []
        for shard in self.root.iterdir():
            if not shard.is_dir():
                continue
            for path in shard.iterdir():
                if path.is_file() and _BLOB_FILE.fullmatch(path.name):
                    ids.append(path.name)
        return ids

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _read(path: Path) -> bytes | None:
        if not path.is_file():
            return None
        return path.read_bytes()

    @staticmethod
    def _unlink(path: Path) -> None:
        path.unlink(missing_ok=True)
