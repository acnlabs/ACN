#!/usr/bin/env python3
"""Unit checks for chat_attach collect / payload (no network)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chat_attach import collect, complete_payload, media_path, valid_chat_id


def test_media_path_and_chat_id() -> None:
    assert valid_chat_id("ae5d82e9-033c-418b-9ac3-f04422fc49cc")
    assert not valid_chat_id("")
    assert not valid_chat_id("../etc/passwd")
    assert not valid_chat_id("https://evil.example/x")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        mp4 = root / "clip.mp4"
        txt = root / "notes.txt"
        mp4.write_bytes(b"fake")
        txt.write_text("no")
        assert media_path(str(mp4)) == mp4.resolve()
        assert media_path(str(txt)) is None
        assert media_path("missing.mp4") is None


def test_collect_content_env_and_mtime() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        named = root / "named.webp"
        extra = root / "extra.png"
        old = root / "old.mp4"
        fresh = root / "fresh.webm"
        named.write_bytes(b"n")
        extra.write_bytes(b"e")
        old.write_bytes(b"o")
        fresh.write_bytes(b"f")
        old_mtime = time.time() - 120
        os.utime(old, (old_mtime, old_mtime))
        since = int(time.time()) - 5
        env = {
            "ACN_CHAT_ATTACH_FILES": str(extra),
            "ACN_CHAT_MEDIA_DIR": str(root),
        }
        got = collect(f"see {named} please", since, env)
        names = {p.name for p in got}
        assert "named.webp" in names
        assert "extra.png" in names
        assert "fresh.webm" in names
        assert "old.mp4" not in names
        skipped = collect("no paths here", None, {"ACN_CHAT_MEDIA_DIR": str(root)})
        assert skipped == []


def test_complete_payload() -> None:
    assert complete_payload("hi", []) == {"content": "hi"}
    assert complete_payload("hi", ["mbx:abc"]) == {
        "content": "hi",
        "attachments": ["mbx:abc"],
    }
    line = json.dumps(complete_payload("已附上。", ["mbx:1"]), ensure_ascii=False)
    assert "attachments" in line
    assert "已附上" in line


if __name__ == "__main__":
    test_media_path_and_chat_id()
    test_collect_content_env_and_mtime()
    test_complete_payload()
    print("ok")
