"""Blob bytes: filesystem and S3-compatible stores."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from acn.infrastructure.blob_store import (
    FilesystemBlobStore,
    S3BlobStore,
    build_blob_store,
    require_blob_id,
)

_BLOB = "11111111-1111-1111-1111-111111111111"


class MemoryS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, ContentType: str | None = None
    ) -> None:
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        data = self.objects.get((Bucket, Key))
        if data is None:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "missing"}},
                "GetObject",
            )
        return {"Body": _Body(data)}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.objects.pop((Bucket, Key), None)

    def list_objects_v2(
        self,
        *,
        Bucket: str,
        Prefix: str = "",
        MaxKeys: int = 1000,
        ContinuationToken: str | None = None,
    ) -> dict[str, object]:
        keys = sorted(
            k for (b, k) in self.objects if b == Bucket and k.startswith(Prefix)
        )
        start = int(ContinuationToken) if ContinuationToken else 0
        page = keys[start : start + MaxKeys]
        truncated = start + len(page) < len(keys)
        out: dict[str, object] = {
            "Contents": [{"Key": k} for k in page],
            "IsTruncated": truncated,
        }
        if truncated:
            out["NextContinuationToken"] = str(start + len(page))
        return out


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


async def test_put_get_delete_round_trip(tmp_path) -> None:
    store = FilesystemBlobStore(tmp_path)
    await store.put(_BLOB, b"hello")
    assert await store.get(_BLOB) == b"hello"
    nested = tmp_path / _BLOB[:2] / _BLOB
    assert nested.is_file()
    await store.delete(_BLOB)
    assert await store.get(_BLOB) is None


async def test_list_ids_skips_junk(tmp_path) -> None:
    store = FilesystemBlobStore(tmp_path)
    await store.put(_BLOB, b"hello")
    junk = tmp_path / "ab" / "not-a-uuid"
    junk.parent.mkdir(parents=True, exist_ok=True)
    junk.write_bytes(b"nope")
    assert await store.list_ids() == [_BLOB]


def test_require_blob_id_rejects_path() -> None:
    with pytest.raises(ValueError, match="invalid blob_id"):
        require_blob_id("../etc/passwd")
    with pytest.raises(ValueError, match="invalid blob_id"):
        require_blob_id("not-a-uuid")


async def test_s3_put_get_delete_round_trip() -> None:
    store = S3BlobStore(bucket="acn-blobs", prefix="blobs", client=MemoryS3())
    await store.put(_BLOB, b"hello")
    assert await store.get(_BLOB) == b"hello"
    await store.delete(_BLOB)
    assert await store.get(_BLOB) is None


async def test_s3_get_missing_is_none() -> None:
    store = S3BlobStore(bucket="acn-blobs", prefix="blobs", client=MemoryS3())
    assert await store.get(_BLOB) is None


async def test_s3_get_http_404_is_none() -> None:
    class Missing:
        def get_object(self, **kwargs: object) -> None:
            raise ClientError(
                {"Error": {"Code": ""}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "GetObject",
            )

    store = S3BlobStore(bucket="acn-blobs", prefix="blobs", client=Missing())
    assert await store.get(_BLOB) is None


async def test_s3_list_ids_skips_junk() -> None:
    client = MemoryS3()
    store = S3BlobStore(bucket="acn-blobs", prefix="blobs", client=client)
    await store.put(_BLOB, b"hello")
    client.put_object(Bucket="acn-blobs", Key="blobs/not-a-uuid", Body=b"nope")
    client.put_object(Bucket="acn-blobs", Key="other/prefix/file", Body=b"nope")
    client.put_object(Bucket="acn-blobs", Key=f"blobs/nested/{_BLOB}", Body=b"nope")
    assert await store.list_ids() == [_BLOB]


async def test_s3_list_ids_paginates() -> None:
    client = MemoryS3()
    store = S3BlobStore(bucket="acn-blobs", prefix="blobs", client=client)
    ids = [f"11111111-1111-1111-1111-{i:012d}" for i in range(3)]
    for blob_id in ids:
        await store.put(blob_id, b"x")

    orig = client.list_objects_v2

    def paged(**kwargs: object) -> dict[str, object]:
        kwargs["MaxKeys"] = 1
        return orig(**kwargs)

    client.list_objects_v2 = paged  # type: ignore[method-assign]
    assert sorted(await store.list_ids()) == sorted(ids)


def test_build_blob_store_filesystem(tmp_path) -> None:
    store = build_blob_store(
        SimpleNamespace(blob_store_backend="filesystem", blob_store_path=str(tmp_path))
    )
    assert isinstance(store, FilesystemBlobStore)


def test_build_blob_store_unknown() -> None:
    with pytest.raises(RuntimeError, match="Unknown BLOB_STORE_BACKEND"):
        build_blob_store(SimpleNamespace(blob_store_backend="gcs"))


async def test_s3_get_reraises_other_errors() -> None:
    class Boom:
        def get_object(self, **kwargs: object) -> None:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "no"}},
                "GetObject",
            )

    store = S3BlobStore(bucket="acn-blobs", prefix="blobs", client=Boom())
    with pytest.raises(ClientError):
        await store.get(_BLOB)


def test_from_settings_requires_bucket() -> None:
    with pytest.raises(RuntimeError, match="BLOB_S3_BUCKET"):
        S3BlobStore.from_settings(
            SimpleNamespace(
                blob_s3_bucket="",
                blob_s3_endpoint_url=None,
                blob_s3_region="auto",
                blob_s3_access_key="ak",
                blob_s3_secret_key="sk",
                blob_s3_prefix="blobs",
            )
        )


def test_from_settings_builds_client(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    captured: dict[str, object] = {}

    def fake_client(service: str, **kwargs: object) -> MemoryS3:
        captured["service"] = service
        captured.update(kwargs)
        return MemoryS3()

    monkeypatch.setattr(boto3, "client", fake_client)
    store = S3BlobStore.from_settings(
        SimpleNamespace(
            blob_s3_bucket="acn-blobs",
            blob_s3_endpoint_url="https://example.r2.cloudflarestorage.com",
            blob_s3_region="auto",
            blob_s3_access_key="ak",
            blob_s3_secret_key="sk",
            blob_s3_prefix="blobs",
        )
    )
    assert isinstance(store, S3BlobStore)
    assert store.bucket == "acn-blobs"
    assert captured["service"] == "s3"
    assert captured["endpoint_url"] == "https://example.r2.cloudflarestorage.com"
    cfg = captured["config"]
    assert cfg.signature_version == "s3v4"
    assert cfg.request_checksum_calculation == "when_required"
    assert cfg.response_checksum_validation == "when_required"


def test_build_blob_store_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    monkeypatch.setattr(boto3, "client", lambda *a, **k: MemoryS3())
    store = build_blob_store(
        SimpleNamespace(
            blob_store_backend="s3",
            blob_s3_bucket="acn-blobs",
            blob_s3_endpoint_url=None,
            blob_s3_region="auto",
            blob_s3_access_key="ak",
            blob_s3_secret_key="sk",
            blob_s3_prefix="blobs",
        )
    )
    assert isinstance(store, S3BlobStore)
