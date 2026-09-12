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

from chat_attach import (
    chat_api_base,
    collect,
    complete_payload,
    content_path_strings,
    envelope_chat_id,
    envelope_since,
    media_path,
    parse_since,
    unwrap_complete,
    valid_chat_id,
)


def test_media_path_and_chat_id() -> None:
    assert valid_chat_id("ae5d82e9-033c-418b-9ac3-f04422fc49cc")
    assert valid_chat_id("chat_1")
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


def test_chat_api_base_prefers_acn_chat() -> None:
    assert chat_api_base({}) == "https://api.agentplanet.org"
    assert (
        chat_api_base({"CHAT_API_BASE": "https://chat.example"})
        == "https://chat.example"
    )
    assert (
        chat_api_base(
            {
                "ACN_CHAT_API_BASE": "https://api.acnlabs.cn",
                "AGENTPLANET_API_BASE": "https://api.agentplanet.org",
                "CHAT_API_BASE": "https://ignored.example",
            }
        )
        == "https://api.acnlabs.cn"
    )


def test_collect_content_env_mtime_and_spaces() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        named = root / "named.webp"
        extra = root / "extra.png"
        spaced = root / "my clip.mp4"
        old = root / "old.mp4"
        named.write_bytes(b"n")
        extra.write_bytes(b"e")
        spaced.write_bytes(b"s")
        old.write_bytes(b"o")
        old_mtime = time.time() - 120
        os.utime(old, (old_mtime, old_mtime))
        since = int(time.time()) - 5
        env = {
            "ACN_CHAT_ATTACH_FILES": str(extra),
            "ACN_CHAT_MEDIA_DIR": str(root),
        }
        got = collect(f'see "{spaced}" and {named} please', since, env)
        names = {p.name for p in got}
        assert "named.webp" in names
        assert "extra.png" in names
        assert "my clip.mp4" in names
        assert "old.mp4" not in names
        skipped = collect("no paths here", None, {"ACN_CHAT_MEDIA_DIR": str(root)})
        assert skipped == []
        zero = collect("no paths here", 0, {"ACN_CHAT_MEDIA_DIR": str(root)})
        assert {p.name for p in zero} >= {"old.mp4", "named.webp"}


def test_collect_dir_newest_first() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        files = []
        now = time.time()
        for i in range(6):
            path = root / f"clip{i}.webm"
            path.write_bytes(b"x")
            os.utime(path, (now - 60 + i, now - 60 + i))
            files.append(path)
        got = collect("", int(now - 90), {"ACN_CHAT_MEDIA_DIR": str(root)})
        assert [p.name for p in got] == [
            "clip5.webm",
            "clip4.webm",
            "clip3.webm",
            "clip2.webm",
        ]


def test_content_paths_quoted() -> None:
    text = 'saved "/tmp/foo bar.mp4" and ./rel.png'
    got = content_path_strings(text)
    assert "/tmp/foo bar.mp4" in got
    assert "./rel.png" in got


def test_complete_payload_and_unwrap() -> None:
    assert complete_payload("hi", []) == {"content": "hi"}
    assert complete_payload("hi", ["mbx:abc"], {"input_tokens": 1}) == {
        "content": "hi",
        "usage": {"input_tokens": 1},
        "attachments": ["mbx:abc"],
    }
    inner, usage, refs = unwrap_complete(
        json.dumps(
            {
                "content": "4秒短视频已附上。",
                "attachments": ["mbx:old", "https://x"],
                "usage": {"input_tokens": 9},
            },
            ensure_ascii=False,
        )
    )
    assert inner == "4秒短视频已附上。"
    assert usage == {"input_tokens": 9}
    assert refs == ["mbx:old"]


def test_envelope_fields() -> None:
    event = {
        "event_type": "a2a_message",
        "received_at": "2026-09-12T04:00:00+00:00",
        "chat": {"chat_id": "ae5d82e9-033c-418b-9ac3-f04422fc49cc"},
    }
    assert envelope_chat_id(event) == "ae5d82e9-033c-418b-9ac3-f04422fc49cc"
    assert envelope_since(event) == parse_since("2026-09-12T04:00:00+00:00")
    assert parse_since("1789142426") == 1789142426
    assert parse_since(0) == 0
    assert envelope_chat_id({"content": "hi"}) == ""


def test_stdin_complete_keeps_usage() -> None:
    import subprocess

    script = Path(__file__).resolve().parent / "chat_attach.py"
    payload = {"content": "hi", "usage": {"input_tokens": 3, "output_tokens": 1}}
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--chat-id",
            "ae5d82e9-033c-418b-9ac3-f04422fc49cc",
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got == payload


def test_wrapped_content_keeps_mbx() -> None:
    import subprocess

    script = Path(__file__).resolve().parent / "chat_attach.py"
    wrapped = json.dumps(
        {"content": "已附上。", "attachments": ["mbx:3d179f6a-9948-45f8-a01a-e86a4f64ad1e"]},
        ensure_ascii=False,
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--chat-id",
            "ae5d82e9-033c-418b-9ac3-f04422fc49cc",
            "--content",
            wrapped,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got["content"] == "已附上。"
    assert got["attachments"] == ["mbx:3d179f6a-9948-45f8-a01a-e86a4f64ad1e"]


if __name__ == "__main__":
    test_media_path_and_chat_id()
    test_chat_api_base_prefers_acn_chat()
    test_collect_content_env_mtime_and_spaces()
    test_collect_dir_newest_first()
    test_content_paths_quoted()
    test_complete_payload_and_unwrap()
    test_envelope_fields()
    test_stdin_complete_keeps_usage()
    test_wrapped_content_keeps_mbx()
    print("ok")
