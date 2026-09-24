"""Filesystem blob bytes."""

from __future__ import annotations

from acn.infrastructure.blob_store import FilesystemBlobStore


async def test_put_get_delete_round_trip(tmp_path) -> None:
    store = FilesystemBlobStore(tmp_path)
    blob_id = "11111111-1111-1111-1111-111111111111"
    await store.put(blob_id, b"hello")
    assert await store.get(blob_id) == b"hello"
    nested = tmp_path / blob_id[:2] / blob_id
    assert nested.is_file()
    await store.delete(blob_id)
    assert await store.get(blob_id) is None


async def test_list_ids_skips_junk(tmp_path) -> None:
    store = FilesystemBlobStore(tmp_path)
    blob_id = "11111111-1111-1111-1111-111111111111"
    await store.put(blob_id, b"hello")
    junk = tmp_path / "ab" / "not-a-uuid"
    junk.parent.mkdir(parents=True, exist_ok=True)
    junk.write_bytes(b"nope")
    assert await store.list_ids() == [blob_id]
