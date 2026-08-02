#!/usr/bin/env python3
"""Offline checks for MVP-1 (no ACN network)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ORCH = _HERE.parent / "org-orchestrator"
sys.path.insert(0, str(_ORCH))
sys.path.insert(0, str(_HERE))

from effective_cap import (  # noqa: E402
    candidates_to_wake,
    effective_cap,
    effective_cap_from_task,
    seats_taken,
)
from handle_collab_pull import parse_collab_pull, resolve_idempotency_key  # noqa: E402
from idempotency import IdempotencyStore  # noqa: E402
from match import (  # noqa: E402
    MatchEmptyError,
    MatchForbiddenError,
    assert_public_match_allowed,
    plan_invites_for_task,
    recall_limit,
    select_invitees,
)
from run_puller import build_envelope, notified_invitees, pull_key  # noqa: E402
from summary import redact_text, task_summary  # noqa: E402


def main() -> int:
    assert effective_cap(100, None) == 16
    assert effective_cap(3, None) == 3
    assert effective_cap(None, None) == 1
    assert (
        effective_cap_from_task(
            {"max_participants": 20, "metadata": {"sparse_collab": {"active_cap": 5}}}
        )
        == 5
    )
    assert seats_taken({"assignee_id": "agt_x"}, None) == 1

    assert (
        candidates_to_wake(
            invited=["a", "b", "c"], already_active=set(), cap=2, seats_used=2
        )
        == []
    )
    assert candidates_to_wake(
        invited=["a", "b", "c"], already_active={"a"}, cap=2, seats_used=1
    ) == ["b"]

    # B1: after a,b notified, next tick pulls c,d
    assert candidates_to_wake(
        invited=["a", "b", "c", "d"],
        already_active=set(),
        cap=2,
        seats_used=0,
        already_notified={"a", "b"},
    ) == ["c", "d"]

    dirty = (
        "title api_key=sk-abcdefghijklmnopqrstuvwxyz123456 "
        "and acn_abcdefghijklmnopqrstuv"
    )
    clean = redact_text(dirty)
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in clean
    assert "acn_abcdefghijklmnopqrstuv" not in clean
    assert "REDACTED" in clean

    task = {
        "task_id": "task_demo",
        "title": "Fix login",
        "description": "password=supersecret123 do not leak",
        "invited_agent_ids": ["agt_1", "agt_2"],
        "max_participants": 2,
    }
    summary = task_summary(task)
    assert "supersecret123" not in summary
    env = build_envelope(task, "agt_1")
    assert env["type"] == "acn.task.collab_pull"
    assert env["idempotency_key"] == pull_key("task_demo", "agt_1")
    assert "supersecret123" not in json.dumps(env)

    with tempfile.TemporaryDirectory() as td:
        store = IdempotencyStore(Path(td) / "idem.json")
        assert store.try_claim(
            pull_key("task_demo", "agt_1"), work_id="task_demo", assignee="agt_1"
        )
        store.confirm(pull_key("task_demo", "agt_1"))
        assert notified_invitees(store, "task_demo") == {"agt_1"}
        nxt = candidates_to_wake(
            invited=["agt_1", "agt_2", "agt_3"],
            already_active=set(),
            cap=1,
            seats_used=0,
            already_notified=notified_invitees(store, "task_demo"),
        )
        assert nxt == ["agt_2"], nxt

    parsed = parse_collab_pull(env)
    assert parsed is not None and parsed["type"] == "acn.task.collab_pull"
    assert resolve_idempotency_key(parsed) == pull_key("task_demo", "agt_1")
    wrapped = {"message": {"text": json.dumps(env)}}
    assert parse_collab_pull(wrapped) is not None
    assert parse_collab_pull({"type": "other"}) is None

    # MVP-2a match
    assert recall_limit(1) == 8
    try:
        assert_public_match_allowed(
            {"metadata": {"sparse_collab": {"sensitivity": "confidential"}}}
        )
        raise AssertionError("confidential should forbid")
    except MatchForbiddenError:
        pass
    agents = [
        {"agent_id": "a", "tags": ["smoke"], "status": "online"},
        {"agent_id": "b", "tags": ["smoke"], "status": "offline"},
        {"agent_id": "c", "tags": ["other"], "status": "online"},
    ]
    assert select_invitees(
        agents, required_tags=["smoke"], limit=5, exclude_ids=set()
    ) == ["a"]
    try:
        plan_invites_for_task(
            {"required_tags": ["nosuch"], "max_participants": 1},
            agents,
            mode="tags",
        )
        raise AssertionError("empty expected")
    except MatchEmptyError:
        pass

    from semantic import agent_profile_text, lexical_similarity, task_query_text

    t = {
        "title": "fix login authentication",
        "description": "cannot sign in",
        "max_participants": 1,
    }
    agents_sem = [
        {
            "agent_id": "dev",
            "description": "login authentication specialist",
            "tags": [],
            "status": "online",
        },
        {
            "agent_id": "chef",
            "description": "pasta recipes",
            "tags": [],
            "status": "online",
        },
    ]
    assert lexical_similarity(
        task_query_text(t), agent_profile_text(agents_sem[0])
    ) > lexical_similarity(task_query_text(t), agent_profile_text(agents_sem[1]))
    assert plan_invites_for_task(t, agents_sem, mode="semantic")[0] == "dev"

    from performance import performance_score

    assert performance_score({"metadata": {}})[0] is None
    s_ok, _ = performance_score(
        {
            "inbound_reachable": True,
            "metadata": {"performance": {"completion_rate": 0.9, "load": 0.1}},
        }
    )
    assert s_ok is not None and s_ok > 0.7

    from completion import PerfCache, aggregate_history, performance_patch_from_aggregate

    agg = aggregate_history(
        [
            {"status": "completed"},
            {"status": "completed"},
            {"status": "rejected"},
        ],
        min_samples=3,
    )
    assert agg["completion_rate"] == round(2 / 3, 4)
    with tempfile.TemporaryDirectory() as td:
        cpath = Path(td) / "p.json"
        PerfCache(cpath).upsert("x", performance_patch_from_aggregate(agg))
        merged = PerfCache(cpath).merge_into_agents(
            [{"agent_id": "x", "metadata": {}}]
        )
        assert "completion_rate" in merged[0]["metadata"]["performance"]

    print("offline_checks OK")
    print(json.dumps({"sample_envelope": env}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
