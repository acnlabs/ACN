"""Unit tests for examples/org-orchestrator/handle_wake.py parsers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "org-orchestrator"
sys.path.insert(0, str(EXAMPLES))

from handle_wake import assignee_matches_me, parse_wake, resolve_idempotency_key  # noqa: E402


def test_parse_direct_wake_object() -> None:
    wake = {
        "type": "acn.org.work_wake",
        "schema_version": 1,
        "org_id": "org_1",
        "work_id": "work_1",
        "assignee": "agt_a",
        "idempotency_key": "org_1:work_1:wake:1:agt_a",
    }
    assert parse_wake(wake)["work_id"] == "work_1"


def test_parse_text_field() -> None:
    wake = {
        "type": "acn.org.work_wake",
        "org_id": "org_1",
        "work_id": "work_2",
        "assignee": "agt_a",
    }
    assert parse_wake({"text": json.dumps(wake)})["work_id"] == "work_2"


def test_parse_mode_b_raw_envelope() -> None:
    wake = {
        "type": "acn.org.work_wake",
        "org_id": "org_1",
        "work_id": "work_3",
        "assignee": "agt_b",
    }
    event = {
        "event_type": "a2a_message",
        "raw": {
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": json.dumps(wake)}],
                }
            }
        },
    }
    assert parse_wake(event)["work_id"] == "work_3"


def test_parse_ignores_unrelated() -> None:
    assert parse_wake({"hello": "world"}) is None


def test_resolve_idempotency_key_prefers_envelope() -> None:
    wake = {
        "idempotency_key": "custom-key",
        "org_id": "org_1",
        "work_id": "work_1",
        "assignee": "agt_a",
    }
    assert resolve_idempotency_key(wake) == "custom-key"


def test_resolve_idempotency_key_derives() -> None:
    wake = {"org_id": "org_1", "work_id": "work_1", "assignee": "agt_a"}
    assert resolve_idempotency_key(wake) == "org_1:work_1:wake:1:agt_a"


def test_assignee_matches_requires_api_assignee() -> None:
    ok, reason = assignee_matches_me(
        envelope_assignee="agt_a",
        work_assignee=None,
        my_id="agt_a",
    )
    assert ok is False
    assert "no assignee" in reason


def test_assignee_matches_me() -> None:
    ok, _ = assignee_matches_me(
        envelope_assignee="agt_a",
        work_assignee="agt_a",
        my_id="agt_a",
    )
    assert ok is True
