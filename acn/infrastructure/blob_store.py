"""Blob bytes. Redis holds metadata; filesystem or S3 holds the file.

ACN still serves HMAC-signed GET. Object storage is the disk, not the public URI.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path
from typing import Any

_BLOB_FILE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def require_blob_id(blob_id: str) -> str:
    if not _BLOB_FILE.fullmatch(blob_id or ""):
        raise ValueError("invalid blob_id")
    return blob_id


def _s3_missing(exc: BaseException) -> bool:
    resp = getattr(exc, "response", None) or {}
    code = str((resp.get("Error") or {}).get("Code") or "")
    if code in {"404", "NoSuchKey", "NotFound"}:
        return True
    return (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode") == 404


class FilesystemBlobStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, blob_id: str) -> Path:
        blob_id = require_blob_id(blob_id)
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


class S3BlobStore:
    """Private bucket. Callers still GET through ACN's signed URI."""

    def __init__(self, *, bucket: str, prefix: str, client: Any) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = client

    def _key(self, blob_id: str) -> str:
        blob_id = require_blob_id(blob_id)
        return f"{self.prefix}/{blob_id}" if self.prefix else blob_id

    def _id_from_key(self, key: str) -> str | None:
        name = key.rsplit("/", 1)[-1]
        if not _BLOB_FILE.fullmatch(name):
            return None
        return name if key == self._key(name) else None

    async def put(self, blob_id: str, data: bytes) -> None:
        key = self._key(blob_id)
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType="application/octet-stream",
        )

    async def get(self, blob_id: str) -> bytes | None:
        key = self._key(blob_id)
        return await asyncio.to_thread(self._get, key)

    def _get(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError

        try:
            body = self._client.get_object(Bucket=self.bucket, Key=key)["Body"]
            try:
                return body.read()
            finally:
                close = getattr(body, "close", None)
                if close is not None:
                    close()
        except ClientError as exc:
            if _s3_missing(exc):
                return None
            raise

    async def delete(self, blob_id: str) -> None:
        key = self._key(blob_id)
        await asyncio.to_thread(
            self._client.delete_object, Bucket=self.bucket, Key=key
        )

    async def list_ids(self) -> list[str]:
        return await asyncio.to_thread(self._list_ids)

    def _list_ids(self) -> list[str]:
        ids: list[str] = []
        token: str | None = None
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "MaxKeys": 1000}
        if self.prefix:
            kwargs["Prefix"] = f"{self.prefix}/"
        while True:
            if token:
                kwargs["ContinuationToken"] = token
            page = self._client.list_objects_v2(**kwargs)
            for item in page.get("Contents") or []:
                blob_id = self._id_from_key(str(item.get("Key") or ""))
                if blob_id:
                    ids.append(blob_id)
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
            if not token:
                break
        return ids

    @classmethod
    def from_settings(cls, settings: Any) -> S3BlobStore:
        import boto3
        from botocore.config import Config as BotoConfig

        bucket = (getattr(settings, "blob_s3_bucket", None) or "").strip()
        if not bucket:
            raise RuntimeError("BLOB_S3_BUCKET is required when BLOB_STORE_BACKEND=s3")
        endpoint = (getattr(settings, "blob_s3_endpoint_url", None) or "").strip() or None
        region = (getattr(settings, "blob_s3_region", None) or "auto").strip() or "auto"
        access = (getattr(settings, "blob_s3_access_key", None) or "").strip() or None
        secret = (getattr(settings, "blob_s3_secret_key", None) or "").strip() or None
        prefix = (getattr(settings, "blob_s3_prefix", None) or "blobs").strip()
        # boto3 1.36+ signs CRC checksums by default; R2/MinIO reject them.
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=access,
            aws_secret_access_key=secret,
            config=BotoConfig(
                signature_version="s3v4",
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        return cls(bucket=bucket, prefix=prefix, client=client)


def build_blob_store(settings: Any) -> FilesystemBlobStore | S3BlobStore:
    kind = (getattr(settings, "blob_store_backend", None) or "filesystem").strip().lower()
    if kind in {"s3", "r2", "minio"}:
        return S3BlobStore.from_settings(settings)
    if kind in {"filesystem", "fs", ""}:
        return FilesystemBlobStore(getattr(settings, "blob_store_path", "./data/blobs"))
    raise RuntimeError(f"Unknown BLOB_STORE_BACKEND={kind!r} (filesystem or s3)")
