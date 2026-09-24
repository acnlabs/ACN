"""A2A message parts for agent-to-agent content (text + file).

Matches the CLI: small files go inline as base64; anything over
``MAX_INLINE_FILE_BYTES`` must be a URL the other agent can fetch.
"""

from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

MAX_INLINE_FILE_BYTES = 160 * 1024

_BLOB_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_MIME_BY_EXT: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".json": "application/json",
    ".zip": "application/zip",
}


class FileTooLargeError(ValueError):
    """Local file exceeds the inline JSON cap; use a fetchable URI instead."""


class InvalidFileUriError(ValueError):
    """URI is missing or is not http(s)."""


def mime_from_filename(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in _MIME_BY_EXT:
        return _MIME_BY_EXT[ext]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def file_part_from_bytes(
    data: bytes,
    *,
    name: str,
    mime_type: str | None = None,
) -> dict[str, Any]:
    """Build a ``kind=file`` part with inline base64 bytes."""
    if len(data) > MAX_INLINE_FILE_BYTES:
        raise FileTooLargeError(
            f"file is {len(data)} bytes; inline limit is {MAX_INLINE_FILE_BYTES}. "
            "Host it and pass file_uri instead."
        )
    file_name = Path(name).name or "file"
    return {
        "kind": "file",
        "file": {
            "bytes": base64.b64encode(data).decode("ascii"),
            "mimeType": mime_type or mime_from_filename(file_name),
            "name": file_name,
        },
    }


def file_part_from_path(
    path: str | Path,
    *,
    name: str | None = None,
    mime_type: str | None = None,
) -> dict[str, Any]:
    """Read a local file and build an inline file part."""
    file_path = Path(path)
    return file_part_from_bytes(
        file_path.read_bytes(),
        name=name or file_path.name,
        mime_type=mime_type,
    )


def file_part_from_uri(
    uri: str,
    *,
    name: str | None = None,
    mime_type: str | None = None,
) -> dict[str, Any]:
    """Build a ``kind=file`` part that points at a URL the other agent fetches."""
    trimmed = (uri or "").strip()
    parsed = urlparse(trimmed)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise InvalidFileUriError(
            "file_uri must be an http(s) URL the other agent can fetch."
        )
    file_name = (name or "").strip() or Path(parsed.path).name or None
    body: dict[str, Any] = {"uri": trimmed}
    if file_name:
        body["name"] = file_name
        body["mimeType"] = mime_type or mime_from_filename(file_name)
    elif mime_type:
        body["mimeType"] = mime_type
    else:
        body["mimeType"] = "application/octet-stream"
    return {"kind": "file", "file": body}


def parse_blob_uri(uri: str) -> tuple[str, str | None]:
    """Return ``(blob_id, sig)`` from a blob id or signed GET URI."""
    trimmed = (uri or "").strip()
    if _BLOB_ID.match(trimmed):
        return trimmed, None
    parsed = urlparse(trimmed)
    blob_id = Path(parsed.path).name
    if not _BLOB_ID.match(blob_id):
        raise InvalidFileUriError("not an ACN blob id or blob URI")
    qs = parse_qs(parsed.query)
    sig_vals = qs.get("sig") or []
    sig = sig_vals[0] if sig_vals else None
    return blob_id, sig or None


def build_message(
    *,
    text: str | None = None,
    parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the ``message`` dict for ``SendMessageRequest``.

    Text-only keeps the legacy ``{"text", "type"}`` shape. Any file or data
    part uses the A2A envelope ``{"role", "parts"}``.
    """
    assembled: list[dict[str, Any]] = []
    if text is not None:
        assembled.append({"kind": "text", "text": text})
    if parts:
        assembled.extend(parts)
    if not assembled:
        raise ValueError("provide text and/or parts")
    if parts in (None, []) and len(assembled) == 1 and assembled[0].get("kind") == "text":
        return {"text": text, "type": "text"}
    return {"role": "user", "parts": assembled}
