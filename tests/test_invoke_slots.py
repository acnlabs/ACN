"""Unit tests for AgentRouter P2 slot helpers."""

from acn.invoke_slots import (
    SlotContractError,
    agent_declares_slot,
    list_slot_candidates,
    normalize_invoke_slots,
    parse_declared_slots,
    pick_slot_provider,
    require_platform_slot,
)


class _Agent:
    def __init__(self, agent_id, *, slots=None, mode="open"):
        self.agent_id = agent_id
        self.metadata = {"invoke_slots": slots or []}
        self.communication_policy = {"mode": mode}


def test_require_platform_slot():
    spec = require_platform_slot("text.reply")
    assert spec["input"] == "text"
    try:
        require_platform_slot("match_collab")
        raise AssertionError("expected unknown slot")
    except SlotContractError as exc:
        assert exc.reason == "unknown_slot"


def test_normalize_fills_platform_contract():
    out = normalize_invoke_slots([{"id": "text.reply", "input": "hack"}])
    assert out == [
        {"id": "text.reply", "input": "text", "output": "text", "pricing": "l2_token"}
    ]


def test_parse_ignores_unknown_stored_slots():
    parsed = parse_declared_slots(
        {"invoke_slots": [{"id": "text.reply"}, {"id": "ghost.slot"}]}
    )
    assert [item["id"] for item in parsed] == ["text.reply"]


def test_pick_online_first_then_id():
    offline = _Agent("aaa", slots=[{"id": "text.reply"}])
    online = _Agent("zzz", slots=[{"id": "text.reply"}])
    picked = pick_slot_provider(
        [offline, online],
        slot_id="text.reply",
        alive_ids={"zzz"},
        caller_kind="host",
    )
    assert picked is online


def test_pick_skips_closed_for_host():
    closed = _Agent("aaa", slots=[{"id": "text.reply"}], mode="closed")
    opened = _Agent("bbb", slots=[{"id": "text.reply"}], mode="open")
    picked = pick_slot_provider(
        [closed, opened],
        slot_id="text.reply",
        alive_ids={"aaa", "bbb"},
        caller_kind="host",
    )
    assert picked is opened


def test_host_allowlist_keeps_closed_declarer():
    closed = _Agent("aaa", slots=[{"id": "text.reply"}], mode="closed")
    opened = _Agent("bbb", slots=[{"id": "text.reply"}], mode="open")
    ordered = list_slot_candidates(
        [closed, opened],
        slot_id="text.reply",
        alive_ids={"aaa", "bbb"},
        caller_kind="host",
        allowed_ids={"aaa", "bbb"},
    )
    assert [x.agent_id for x in ordered] == ["aaa", "bbb"]


def test_list_candidates_preferred_first():
    a = _Agent("aaa", slots=[{"id": "text.reply"}])
    z = _Agent("zzz", slots=[{"id": "text.reply"}])
    ordered = list_slot_candidates(
        [a, z],
        slot_id="text.reply",
        alive_ids={"aaa", "zzz"},
        caller_kind="host",
        preferred="zzz",
    )
    assert [x.agent_id for x in ordered] == ["zzz", "aaa"]


def test_agent_declares_slot():
    agent = _Agent("x", slots=[{"id": "text.reply"}])
    assert agent_declares_slot(agent, "text.reply")
    assert not agent_declares_slot(agent, "other")
