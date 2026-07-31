"""effective_cap — sparse-collab-contract §1.4 (stdlib only)."""

from __future__ import annotations

from typing import Any

# Product defaults (configurable; NOT ACN kernel constants).
PRODUCT_DEFAULT_ACTIVE_CAP = 16
PRODUCT_DEFAULT_ACTIVE_CAP_WHEN_UNLIMITED = 1


def parse_sparse_collab(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    sc = metadata.get("sparse_collab")
    return sc if isinstance(sc, dict) else {}


def effective_cap(
    max_participants: int | None,
    active_cap: int | None = None,
    *,
    default_when_finite: int = PRODUCT_DEFAULT_ACTIVE_CAP,
    default_when_unlimited: int = PRODUCT_DEFAULT_ACTIVE_CAP_WHEN_UNLIMITED,
) -> int:
    """Return Admit hard cap (always a positive int)."""
    if max_participants is not None:
        if max_participants < 1:
            raise ValueError("max_participants must be >= 1 when set")
        cap = active_cap if active_cap is not None else default_when_finite
        if cap < 1:
            raise ValueError("active_cap must be >= 1")
        return min(max_participants, cap)
    cap = active_cap if active_cap is not None else default_when_unlimited
    if cap < 1:
        raise ValueError("active_cap must be >= 1")
    return cap


def effective_cap_from_task(task: dict[str, Any]) -> int:
    sc = parse_sparse_collab(task.get("metadata") if isinstance(task, dict) else None)
    raw_ac = sc.get("active_cap")
    active_cap: int | None
    if raw_ac is None or raw_ac == "":
        active_cap = None
    else:
        active_cap = int(raw_ac)
    mp = task.get("max_participants")
    if mp is None:
        max_participants = None
    else:
        max_participants = int(mp)
    return effective_cap(max_participants, active_cap)


def seats_taken(task: dict[str, Any], participations: list[dict[str, Any]] | None) -> int:
    """Count seats that block further Admit (active + completed-ish)."""
    if participations:
        n = 0
        for row in participations:
            if not isinstance(row, dict):
                continue
            st = str(row.get("status") or "").lower()
            if st in ("active", "submitted", "completed"):
                n += 1
        return n
    # Single-participant fallback (TaskResponse uses assignee_id)
    if (
        task.get("assignee_id")
        or task.get("assigned_agent_id")
        or task.get("assignee_agent_id")
    ):
        return 1
    return int(task.get("active_participants_count") or 0) + int(
        task.get("completed_count") or 0
    )


def active_agent_ids(participations: list[dict[str, Any]] | None) -> set[str]:
    out: set[str] = set()
    for row in participations or []:
        if not isinstance(row, dict):
            continue
        st = str(row.get("status") or "").lower()
        if st in ("active", "submitted", "completed"):
            aid = str(
                row.get("participant_id")
                or row.get("agent_id")
                or row.get("assignee_id")
                or ""
            ).strip()
            if aid:
                out.add(aid)
    return out


def candidates_to_wake(
    *,
    invited: list[str],
    already_active: set[str],
    cap: int,
    seats_used: int,
    already_notified: set[str] | None = None,
) -> list[str]:
    """Invitees not yet Active, limited by remaining seats.

    Skip ``already_notified`` (idempotent successful wakes) so later invitees
    can be pulled when earlier ones never accept (audit B1).
    Admit capacity is still enforced by ACN accept.
    """
    remaining = max(0, cap - seats_used)
    if remaining == 0:
        return []
    notified = already_notified or set()
    out: list[str] = []
    seen: set[str] = set()
    for aid in invited:
        a = str(aid or "").strip()
        if not a or a in seen or a in already_active or a in notified:
            continue
        seen.add(a)
        out.append(a)
        if len(out) >= remaining:
            break
    return out


def _self_test() -> None:
    assert effective_cap(3, None) == 3
    assert effective_cap(100, None) == 16
    assert effective_cap(100, 8) == 8
    assert effective_cap(None, None) == 1
    assert effective_cap(None, 5) == 5
    assert effective_cap(50, 100) == 50
    task = {
        "max_participants": 10,
        "metadata": {"sparse_collab": {"active_cap": 4}},
    }
    assert effective_cap_from_task(task) == 4
    assert seats_taken({"assignee_id": "agt_x"}, None) == 1
    invited = ["a", "b", "c", "a"]
    got = candidates_to_wake(
        invited=invited, already_active={"b"}, cap=2, seats_used=1
    )
    assert got == ["a"], got
    got2 = candidates_to_wake(
        invited=invited, already_active=set(), cap=2, seats_used=2
    )
    assert got2 == []
    # B1: after a,b notified but not active, pull c next
    got3 = candidates_to_wake(
        invited=["a", "b", "c", "d"],
        already_active=set(),
        cap=2,
        seats_used=0,
        already_notified={"a", "b"},
    )
    assert got3 == ["c", "d"], got3
    print("effective_cap self-test OK")


if __name__ == "__main__":
    _self_test()
