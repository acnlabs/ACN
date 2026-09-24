"""A2A file parts for send_content / build_message."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from acn_client.client import ACNClient
from acn_client.message_parts import (
    MAX_INLINE_FILE_BYTES,
    FileTooLargeError,
    InvalidFileUriError,
    build_message,
    file_part_from_bytes,
    file_part_from_path,
    file_part_from_uri,
)
from acn_client.models import SendMessageRequest


def test_text_only_keeps_legacy_shape():
    assert build_message(text="hello") == {"text": "hello", "type": "text"}


def test_inline_file_part():
    part = file_part_from_bytes(b"hello", name="hi.txt")
    assert part["kind"] == "file"
    assert part["file"]["name"] == "hi.txt"
    assert part["file"]["mimeType"] == "text/plain"
    assert part["file"]["bytes"] == base64.b64encode(b"hello").decode("ascii")


def test_inline_file_over_cap():
    with pytest.raises(FileTooLargeError):
        file_part_from_bytes(b"x" * (MAX_INLINE_FILE_BYTES + 1), name="big.bin")


def test_file_part_from_path(tmp_path: Path):
    p = tmp_path / "sketch.png"
    p.write_bytes(b"png")
    part = file_part_from_path(p)
    assert part["file"]["name"] == "sketch.png"
    assert part["file"]["mimeType"] == "image/png"


def test_uri_file_part():
    part = file_part_from_uri("https://example.com/a.png")
    assert part == {
        "kind": "file",
        "file": {
            "uri": "https://example.com/a.png",
            "name": "a.png",
            "mimeType": "image/png",
        },
    }


def test_uri_rejects_file_scheme():
    with pytest.raises(InvalidFileUriError):
        file_part_from_uri("file:///tmp/a.png")


def test_parse_blob_uri_id_and_signed_url():
    from acn_client.message_parts import parse_blob_uri

    blob_id = "11111111-1111-1111-1111-111111111111"
    assert parse_blob_uri(blob_id) == (blob_id, None)
    assert parse_blob_uri(f"https://acn.test/api/v1/blobs/{blob_id}?sig=ab") == (
        blob_id,
        "ab",
    )


@pytest.mark.asyncio
async def test_extend_blob_posts_sig_from_uri():
    request_mock = AsyncMock(return_value={"id": "b", "retained": True})
    client = ACNClient(base_url="http://acn.test")
    client._request = request_mock  # type: ignore[method-assign]
    blob_id = "11111111-1111-1111-1111-111111111111"
    await client.extend_blob(f"https://acn.test/api/v1/blobs/{blob_id}?sig=ab", 7)
    method, path = request_mock.await_args.args[:2]
    body = request_mock.await_args.kwargs["json"]
    assert method == "POST"
    assert path == f"/api/v1/blobs/{blob_id}/extend"
    assert body == {"extra_days": 7, "sig": "ab"}


@pytest.mark.asyncio
async def test_download_blob_gets_bytes_with_sig():
    client = ACNClient(base_url="http://acn.test")
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.content = b"hello"
    client._client.get = AsyncMock(return_value=mock_resp)
    blob_id = "11111111-1111-1111-1111-111111111111"
    data = await client.download_blob(f"https://acn.test/api/v1/blobs/{blob_id}?sig=ab")
    assert data == b"hello"
    args, kwargs = client._client.get.await_args
    assert args[0] == f"/api/v1/blobs/{blob_id}"
    assert kwargs["params"] == {"sig": "ab"}


def test_build_message_with_file():
    part = file_part_from_uri("https://example.com/a.png")
    msg = build_message(text="diagram", parts=[part])
    assert msg["role"] == "user"
    assert msg["parts"][0] == {"kind": "text", "text": "diagram"}
    assert msg["parts"][1]["file"]["uri"] == "https://example.com/a.png"


@pytest.mark.asyncio
async def test_send_content_uploads_file_then_posts_uri(tmp_path: Path):
    request_mock = AsyncMock(return_value={"status": "sent"})
    client = ACNClient(base_url="http://acn.test")
    client._request = request_mock  # type: ignore[method-assign]
    client.upload_blob = AsyncMock(  # type: ignore[method-assign]
        return_value={"uri": "https://acn.test/api/v1/blobs/x?exp=1&sig=ab"}
    )
    sketch = tmp_path / "sketch.png"
    sketch.write_bytes(b"png")

    await client.send_content("agent-a", "agent-b", text="diagram", file_path=str(sketch))

    client.upload_blob.assert_awaited_once()
    body = request_mock.await_args.kwargs["json"]
    assert body["message"]["parts"][1]["file"]["uri"] == (
        "https://acn.test/api/v1/blobs/x?exp=1&sig=ab"
    )


@pytest.mark.asyncio
async def test_send_content_posts_file_uri():
    request_mock = AsyncMock(return_value={"status": "sent"})
    client = ACNClient(base_url="http://acn.test")
    client._request = request_mock  # type: ignore[method-assign]

    await client.send_content(
        "agent-a",
        "agent-b",
        text="diagram",
        file_uri="https://example.com/a.png",
    )

    request_mock.assert_awaited_once()
    method, path = request_mock.await_args.args[:2]
    body = request_mock.await_args.kwargs["json"]
    assert method == "POST"
    assert path == "/api/v1/communication/send"
    assert body["from_agent"] == "agent-a"
    assert body["target_agent"] == "agent-b"
    SendMessageRequest.model_validate(body)
    assert body["message"]["parts"][1]["file"]["uri"] == "https://example.com/a.png"
