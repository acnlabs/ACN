#!/usr/bin/env python3
"""Unit checks for official hop wake parsing (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from official_hop import (
    _byo_cmd,
    _user_text,
    allow_host_url,
    complete_official,
    door_already_open,
    extract_completion_content,
    official_runtime_env,
    resolve_wake,
)


def test_allow_host_url() -> None:
    assert (
        allow_host_url("https://api.agentplanet.org/api/inference/v1")
        == "https://api.agentplanet.org/api/inference/v1"
    )
    assert allow_host_url("https://evil.example/api/inference/v1") == ""
    assert allow_host_url("http://127.0.0.1:8000/api/inference/v1") == (
        "http://127.0.0.1:8000/api/inference/v1"
    )
    assert allow_host_url("http://evil.example/api/inference/v1") == ""


def test_resolve_official_from_chat_and_env() -> None:
    ev = {
        "chat": {
            "inference_path": "official",
            "hop_id": "hop:dialog:c:m:agent-1",
            "host_inference_url": "https://api.agentplanet.org/api/inference/v1",
            "requested_model": "tencenttokenplan/kimi-k2.5",
        }
    }
    got = resolve_wake(
        ev, {"ACN_AGENT_JWT": "jwt-1", "ACN_AGENT_ID": "agent-1"}
    )
    assert got["inference_path"] == "official"
    assert got["hop_id"] == "hop:dialog:c:m:agent-1"
    assert got["requested_model"] == "tencenttokenplan/kimi-k2.5"
    assert got["jwt"] == "jwt-1"
    assert got["user_text"] == ""
    assert got["max_output_tokens"] == ""


def test_official_without_jwt_stays_byo() -> None:
    ev = {
        "raw": {
            "params": {
                "message": {
                    "metadata": {
                        "agentplanet": {
                            "inference_path": "official",
                            "hop_id": "hop:dialog:c:m:agent-1",
                            "host_inference_url": "https://api.agentplanet.org/api/inference/v1",
                        }
                    }
                }
            }
        }
    }
    got = resolve_wake(ev, {})
    assert got["inference_path"] == "byo"
    assert got["hop_id"] == "hop:dialog:c:m:agent-1"


def test_door_already_open() -> None:
    assert door_already_open(
        {"OPENAI_BASE_URL": "http://127.0.0.1:8123/v1", "OPENAI_API_KEY": "jwt"}
    )
    assert not door_already_open({"OPENAI_BASE_URL": "http://127.0.0.1:8123/v1"})
    assert not door_already_open(
        {"OPENAI_BASE_URL": "https://api.openai.com/v1", "OPENAI_API_KEY": "sk"}
    )


def test_user_text_and_completion_content() -> None:
    assert _user_text({"chat": {"user_text": "hi there"}}) == "hi there"
    long_text = "x" * 300
    assert _user_text({"chat": {"user_text": long_text}}) == long_text
    assert (
        _user_text({"chat": {"user_text": "line1\nline2"}}) == "line1\nline2"
    )
    assert (
        _user_text(
            {
                "raw": {
                    "params": {
                        "message": {
                            "parts": [{"kind": "text", "text": "from parts"}]
                        }
                    }
                }
            }
        )
        == "from parts"
    )
    assert (
        extract_completion_content(
            {"choices": [{"message": {"content": "official hi"}}]}
        )
        == "official hi"
    )
    assert extract_completion_content({"content": "plain"}) == "plain"
    assert _byo_cmd(["--complete", "--", "echo", "x"]) == ["echo", "x"]
    assert _byo_cmd(["--complete"]) == []


def test_official_runtime_env_requires_door_and_strips_vendor_keys() -> None:
    assert official_runtime_env({"OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}) is None
    assert (
        official_runtime_env(
            {
                "OPENAI_BASE_URL": "https://api.openai.com/v1",
                "OPENAI_API_KEY": "sk",
            }
        )
        is None
    )
    got = official_runtime_env(
        {
            "OPENAI_BASE_URL": "http://127.0.0.1:8123/v1",
            "OPENAI_API_KEY": "jwt",
            "OPENROUTER_API_KEY": "sk-or",
            "ANTHROPIC_API_KEY": "sk-ant",
            "KEEP_ME": "yes",
        }
    )
    assert got is not None
    assert got["OPENAI_API_KEY"] == "jwt"
    assert got["OPENAI_API_BASE"] == "http://127.0.0.1:8123/v1"
    assert got["ACN_INFERENCE_PATH"] == "official"
    assert got["KEEP_ME"] == "yes"
    assert "OPENROUTER_API_KEY" not in got
    assert "ANTHROPIC_API_KEY" not in got


def test_official_cmd_requires_door_then_runs_guarded() -> None:
    import json
    import os
    import subprocess

    hop = Path(__file__).resolve().parent / "official_hop.py"
    event = json.dumps(
        {
            "chat": {
                "inference_path": "official",
                "hop_id": "hop:dialog:c:m:agent-1",
                "host_inference_url": "https://api.agentplanet.org/api/inference/v1",
                "requested_model": "kimi",
                "user_text": "hi",
            }
        }
    ).encode()
    denied = subprocess.run(
        [sys.executable, str(hop), "--complete", "--", "true"],
        input=event,
        env={
            **os.environ,
            "ACN_AGENT_JWT": "jwt",
            "ACN_AGENT_ID": "agent-1",
            "OPENAI_BASE_URL": "",
            "OPENAI_API_KEY": "",
        },
        capture_output=True,
        check=False,
    )
    assert denied.returncode == 2
    assert b"official_door_required" in denied.stderr

    inner = (
        "import os,sys;"
        "ok = os.environ.get('OPENAI_BASE_URL','').startswith('http://127.0.0.1')"
        " and 'OPENROUTER_API_KEY' not in os.environ;"
        "sys.stdout.write('{\"content\":\"ok\"}');"
        "sys.exit(0 if ok else 9)"
    )
    guarded = subprocess.run(
        [sys.executable, str(hop), "--complete", "--", sys.executable, "-c", inner],
        input=event,
        env={
            **os.environ,
            "ACN_AGENT_JWT": "jwt",
            "ACN_AGENT_ID": "agent-1",
            "OPENAI_BASE_URL": "http://127.0.0.1:8123/v1",
            "OPENAI_API_KEY": "jwt",
            "OPENROUTER_API_KEY": "leak",
        },
        capture_output=True,
        check=False,
    )
    assert guarded.returncode == 0
    assert b'"content": "ok"' in guarded.stdout or b'"content":"ok"' in guarded.stdout


def test_complete_official_rejects_missing_and_byo() -> None:
    assert (
        complete_official(
            {
                "requested_model": "",
                "host_inference_url": "https://api.agentplanet.org/api/inference/v1",
                "jwt": "jwt",
                "hop_id": "hop:1",
                "agent_id": "",
            },
            "",
        )
        == 2
    )
    import json
    import subprocess

    hop = Path(__file__).resolve().parent / "official_hop.py"
    p = subprocess.run(
        [sys.executable, str(hop), "--complete"],
        input=json.dumps({"chat": {"inference_path": "byo"}}).encode(),
        capture_output=True,
        check=False,
    )
    assert p.returncode == 3
    assert b"byo_use_complete_exec" in p.stderr


if __name__ == "__main__":
    test_allow_host_url()
    test_resolve_official_from_chat_and_env()
    test_official_without_jwt_stays_byo()
    test_door_already_open()
    test_user_text_and_completion_content()
    test_official_runtime_env_requires_door_and_strips_vendor_keys()
    test_official_cmd_requires_door_then_runs_guarded()
    test_complete_official_rejects_missing_and_byo()
    print("ok")
