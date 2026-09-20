#!/usr/bin/env python3
"""Unit checks for conversation-orchestrator host helper (no network)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrate_chat import (
    InvokeError,
    callee_status,
    complete_chat,
    extract_reply_text,
    invoke_and_summarize,
    normalize_base,
    summarize,
)


def test_normalize_base() -> None:
    assert normalize_base("https://api.acnlabs.dev") == (
        "https://api.acnlabs.dev/api/v1"
    )
    assert normalize_base("https://acn.acnlabs.cn/api/v1/") == (
        "https://acn.acnlabs.cn/api/v1"
    )


def test_extract_reply_from_mode_a_and_relay() -> None:
    assert (
        extract_reply_text(
            {"delivery": {"response": {"parts": [{"kind": "text", "text": "hi"}]}}}
        )
        == "hi"
    )
    assert (
        extract_reply_text({"result": {"message": {"content": "done"}}}) == "done"
    )
    assert extract_reply_text({"status": "accepted"}) is None
    assert extract_reply_text("accepted") is None


def test_summarize_completed_does_not_copy_usage() -> None:
    out = summarize(
        user_text="画只鸭",
        callee_id="peer-9",
        callee_name="Peer",
        payload={
            "to": "peer-9",
            "hop_id": "hop:invoke:z",
            "status": "delivered",
            "usage": {"input_tokens": 999, "output_tokens": 9},
            "delivery": {"response": {"text": "一只黄鸭"}},
        },
        error=None,
    )
    assert out["content"] == "一只黄鸭"
    assert "usage" not in out
    assert out["orchestration"]["callees"] == [
        {
            "agent_id": "peer-9",
            "status": "completed",
            "hop_id": "hop:invoke:z",
            "name": "Peer",
        }
    ]


def test_summarize_accepted_and_failed() -> None:
    pending = summarize(
        user_text="帮忙",
        callee_id="peer-1",
        callee_name="",
        payload={"to": "peer-1", "hop_id": "hop:invoke:a", "status": "accepted"},
        error=None,
    )
    assert pending["content"].startswith("已请 peer-1")
    assert pending["orchestration"]["callees"][0]["status"] == "accepted"
    failed = summarize(
        user_text="帮忙",
        callee_id="peer-1",
        callee_name="X",
        payload=None,
        error="invoke HTTP 403: spend_capped",
    )
    assert "没叫到帮手 X" in failed["content"]
    assert failed["orchestration"]["callees"][0]["status"] == "failed"


def test_invoke_and_summarize_injectable() -> None:
    def fake(_base: str, _key: str, body: dict) -> dict:
        assert body["to"] == "peer-9"
        assert body["message"]["text"] == "画只鸭"
        return {
            "to": "peer-9",
            "hop_id": "hop:invoke:z",
            "status": "delivered",
            "usage": {"input_tokens": 50, "output_tokens": 20},
            "delivery": {"content": "好了"},
        }

    out = invoke_and_summarize(
        "画只鸭",
        {
            "ACN_API_KEY": "acn_test",
            "ACN_ORCH_TO": "peer-9",
            "ACN_ORCH_NAME": "Peer",
            "ACN_ORCH_USAGE_JSON": json.dumps(
                {"input_tokens": 3, "output_tokens": 1, "meter_source": "peer_self"}
            ),
        },
        invoke_fn=fake,
    )
    assert out["content"] == "好了"
    assert out["usage"] == {
        "input_tokens": 3,
        "output_tokens": 1,
        "meter_source": "peer_self",
    }
    assert "collab_request" not in out


def test_invoke_error_and_missing_key() -> None:
    def boom(_base: str, _key: str, _body: dict) -> dict:
        raise InvokeError(403, "spend_capped")

    out = invoke_and_summarize(
        "hi",
        {"ACN_API_KEY": "acn_test", "ACN_ORCH_TO": "peer-1"},
        invoke_fn=boom,
    )
    assert out["orchestration"]["callees"][0]["status"] == "failed"
    missing = invoke_and_summarize("hi", {"ACN_ORCH_TO": "peer-1"})
    assert "ACN_API_KEY" in missing["content"]


def test_complete_chat_skips_invoke_envelope() -> None:
    out = complete_chat(
        {"invoke": {"hop_id": "hop:invoke:x", "request_id": "r"}},
        {"ACN_ORCH_TO": "peer-1", "ACN_API_KEY": "x"},
        invoke_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no invoke")),
    )
    assert "invoke 跳" in out["content"]


def test_callee_status() -> None:
    assert callee_status({"status": "accepted"}, None) == "accepted"
    assert callee_status({"status": "inbox"}, None) == "accepted"
    assert callee_status({"status": "failed"}, None) == "failed"
    assert callee_status({"status": "accepted"}, "body") == "completed"


if __name__ == "__main__":
    test_normalize_base()
    test_extract_reply_from_mode_a_and_relay()
    test_summarize_completed_does_not_copy_usage()
    test_summarize_accepted_and_failed()
    test_invoke_and_summarize_injectable()
    test_invoke_error_and_missing_key()
    test_complete_chat_skips_invoke_envelope()
    test_callee_status()
    print("ok")
