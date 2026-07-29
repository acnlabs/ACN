"""Unit tests for examples/org-orchestrator/handle_handoff.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "org-orchestrator"
sys.path.insert(0, str(EXAMPLES))

from handle_handoff import (  # noqa: E402
    extract_transport_sender,
    parse_handoff,
    resolve_idempotency_key,
    verify_sender_anti_spoof,
)
from send_handoff import build_envelope  # noqa: E402


def test_parse_direct_handoff() -> None:
    h = {
        "type": "acn.org.work_handoff",
        "org_id": "org_1",
        "work_id": "work_1",
        "from_agent": "agt_a",
        "to_agent": "agt_b",
    }
    assert parse_handoff(h)["work_id"] == "work_1"


def test_parse_ignores_wake() -> None:
    assert parse_handoff({"type": "acn.org.work_wake", "work_id": "w"}) is None


def test_extract_transport_sender() -> None:
    assert (
        extract_transport_sender(
            {"from_agent": "agt_a", "message": {"text": "{}"}}
        )
        == "agt_a"
    )


def test_bare_handoff_envelope_is_not_transport_sender() -> None:
    assert (
        extract_transport_sender(
            {
                "type": "acn.org.work_handoff",
                "from_agent": "agt_evil",
                "to_agent": "agt_b",
            }
        )
        == ""
    )


def test_verify_sender_anti_spoof_ok() -> None:
    ok, _ = verify_sender_anti_spoof(
        envelope_from="agt_a",
        trusted_sender="agt_a",
        my_id="agt_b",
        envelope_to="agt_b",
    )
    assert ok


def test_verify_sender_rejects_spoof() -> None:
    ok, reason = verify_sender_anti_spoof(
        envelope_from="agt_evil",
        trusted_sender="agt_a",
        my_id="agt_b",
        envelope_to="agt_b",
    )
    assert not ok
    assert "from_agent" in reason


def test_verify_sender_fail_closed_no_transport() -> None:
    ok, reason = verify_sender_anti_spoof(
        envelope_from="agt_a",
        trusted_sender="",
        my_id="agt_b",
        envelope_to="agt_b",
    )
    assert not ok
    assert "HANDOFF_TRUSTED_SENDER" in reason or "sender" in reason


def test_build_envelope_idem_key() -> None:
    env = build_envelope(
        org_id="org_1",
        work_id="work_1",
        from_agent="agt_a",
        to_agent="agt_b",
        title="t",
        note="n",
        generation=1,
        kb_refs=None,
    )
    assert env["idempotency_key"] == "org_1:work_1:handoff:1:agt_a:agt_b"
    assert resolve_idempotency_key(env) == env["idempotency_key"]


def test_parse_embedded_in_history_shape() -> None:
    env = build_envelope(
        org_id="org_1",
        work_id="work_9",
        from_agent="agt_a",
        to_agent="agt_b",
        title="t",
        note="",
        generation=1,
        kb_refs=None,
    )
    outer = {
        "from_agent": "agt_a",
        "message": {"text": json.dumps(env)},
    }
    assert parse_handoff(outer)["work_id"] == "work_9"
    assert extract_transport_sender(outer) == "agt_a"
