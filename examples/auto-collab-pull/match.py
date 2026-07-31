"""MVP-2a/2b: hard filter + tag and/or semantic recall (no Router Agent).

See docs/auto-collab-pull-mvp-v0.md §3 · sparse-collab-contract §7 P2.
"""

from __future__ import annotations

from typing import Any, Literal

import os

from effective_cap import effective_cap_from_task, parse_sparse_collab
from performance import performance_scores
from semantic import rank_agents, task_query_text

MatchMode = Literal["tags", "semantic", "hybrid"]

# Product default: small until completion_rate has real volume (P8).
DEFAULT_PERF_WEIGHT = 0.15


def _perf_weight() -> float:
    raw = (os.environ.get("MATCH_PERF_WEIGHT") or "").strip()
    if not raw:
        return DEFAULT_PERF_WEIGHT
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return DEFAULT_PERF_WEIGHT


class MatchForbiddenError(ValueError):
    """Public/auto match not allowed (e.g. confidential)."""


class MatchEmptyError(ValueError):
    """Hard filter + tag recall produced no candidates."""


def sensitivity_of(task: dict[str, Any]) -> str:
    sc = parse_sparse_collab(task.get("metadata") if isinstance(task, dict) else None)
    raw = str(sc.get("sensitivity") or task.get("sensitivity") or "public").strip().lower()
    return raw or "public"


def assert_public_match_allowed(task: dict[str, Any]) -> None:
    """P2: confidential must not use public auto-match (MVP-2)."""
    if sensitivity_of(task) == "confidential":
        raise MatchForbiddenError(
            "sensitivity=confidential forbids MVP-2 public match; "
            "use MVP-1 invite list or Org member pool"
        )


def required_tags_of(task: dict[str, Any]) -> list[str]:
    """Tags/skills the matcher must look for (exact)."""
    raw = task.get("required_tags") or task.get("required_skills") or []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for x in raw:
        t = str(x or "").strip().lower()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    sc = parse_sparse_collab(task.get("metadata") if isinstance(task, dict) else None)
    for key in ("skills", "tags"):
        extra = sc.get(key)
        if isinstance(extra, list):
            for x in extra:
                t = str(x or "").strip().lower()
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
    return out


def recall_limit(cap: int) -> int:
    """K ≈ min(64, max(effective_cap×3, 8)) — MVP doc §3.1."""
    c = max(1, int(cap))
    return min(64, max(c * 3, 8))


def agent_tags(agent: dict[str, Any]) -> set[str]:
    tags = agent.get("tags") or []
    out: set[str] = set()
    if isinstance(tags, list):
        for t in tags:
            s = str(t or "").strip().lower()
            if s:
                out.add(s)
    skills = agent.get("skills")
    if isinstance(skills, list):
        for s in skills:
            if isinstance(s, dict):
                name = str(s.get("id") or s.get("name") or "").strip().lower()
            else:
                name = str(s or "").strip().lower()
            if name:
                out.add(name)
    return out


def tag_overlap_score(agent: dict[str, Any], required: list[str]) -> int:
    if not required:
        return 0
    have = agent_tags(agent)
    return sum(1 for t in required if t in have)


def hard_filter(
    agents: list[dict[str, Any]],
    *,
    exclude_ids: set[str] | None = None,
    require_online: bool = True,
) -> list[dict[str, Any]]:
    """Drop excluded / offline / malformed rows (status side-car filter)."""
    ban = exclude_ids or set()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in agents:
        if not isinstance(row, dict):
            continue
        aid = str(row.get("agent_id") or "").strip()
        if not aid or aid in ban or aid in seen:
            continue
        st = str(row.get("status") or "").lower()
        if require_online and st and st not in ("online", "active", "available"):
            continue
        seen.add(aid)
        out.append(row)
    return out


def select_invitees(
    agents: list[dict[str, Any]],
    *,
    required_tags: list[str],
    limit: int,
    exclude_ids: set[str] | None = None,
    require_online: bool = True,
    require_tag_hit: bool = True,
    mode: MatchMode = "tags",
    query: str | None = None,
) -> list[str]:
    """Rank candidates; return up to ``limit`` agent_ids.

    Modes:
      tags     — tag overlap only (MVP-2a)
      semantic — Agent Card / profile similarity only (MVP-2b)
      hybrid   — tag boost + semantic (default product path)
    """
    filtered = hard_filter(
        agents, exclude_ids=exclude_ids, require_online=require_online
    )
    if not filtered:
        return []

    if mode == "tags":
        scored: list[tuple[int, int, str]] = []
        for i, row in enumerate(filtered):
            aid = str(row.get("agent_id") or "").strip()
            score = tag_overlap_score(row, required_tags)
            if required_tags and require_tag_hit and score <= 0:
                continue
            scored.append((-score, i, aid))
        scored.sort()
        return [aid for _, _, aid in scored[: max(0, limit)]]

    # semantic / hybrid (+ optional performance term)
    n_req = max(1, len(required_tags)) if required_tags else 1
    tag_norms = [
        (tag_overlap_score(row, required_tags) / n_req) if required_tags else 0.0
        for row in filtered
    ]
    q = (query or "").strip()
    perfs = performance_scores(filtered)
    if mode == "semantic":
        tw, sw, pw = 0.0, 1.0, 0.0
        min_score = 0.05
    else:  # hybrid
        tw, sw = (0.30, 0.55) if required_tags else (0.0, 0.85)
        pw = _perf_weight()
        min_score = 0.05

    ranked = rank_agents(
        q,
        filtered,
        tag_scores=tag_norms,
        perf_scores=perfs,
        tag_weight=tw,
        semantic_weight=sw,
        perf_weight=pw,
        min_score=min_score,
    )
    if mode == "hybrid" and required_tags and require_tag_hit:
        # Prefer tag hits; if none survive, fall back to pure semantic top-K
        tagged = [r for r in ranked if r[3] > 0]
        if tagged:
            ranked = tagged
    return [aid for aid, _, _, _ in ranked[: max(0, limit)]]


def plan_invites_for_task(
    task: dict[str, Any],
    agents: list[dict[str, Any]],
    *,
    exclude_ids: set[str] | None = None,
    mode: MatchMode = "hybrid",
) -> list[str]:
    """P2 gate → K from cap → select. Raises on forbid/empty."""
    assert_public_match_allowed(task)
    tags = required_tags_of(task)
    cap = effective_cap_from_task(task)
    k = recall_limit(cap)
    already = set(exclude_ids or set())
    for x in task.get("invited_agent_ids") or []:
        s = str(x or "").strip()
        if s:
            already.add(s)
    creator = str(
        task.get("creator_id") or task.get("owner_id") or task.get("created_by") or ""
    ).strip()
    if creator:
        already.add(creator)

    # tags mode: require tag hit when tags present; hybrid/semantic can soft-match
    require_tag = bool(tags) and mode == "tags"
    picked = select_invitees(
        agents,
        required_tags=tags,
        limit=k,
        exclude_ids=already,
        require_tag_hit=require_tag,
        mode=mode,
        query=task_query_text(task),
    )
    if not picked:
        raise MatchEmptyError(
            "no candidates after hard filter + recall "
            f"(mode={mode}, tags={tags!r}, k={k}, excluded={len(already)})"
        )
    return picked


def _self_test() -> None:
    assert recall_limit(1) == 8
    assert recall_limit(3) == 9
    assert recall_limit(30) == 64

    conf = {"metadata": {"sparse_collab": {"sensitivity": "confidential"}}}
    try:
        assert_public_match_allowed(conf)
        raise AssertionError("expected forbid")
    except MatchForbiddenError:
        pass

    agents = [
        {"agent_id": "a", "tags": ["smoke", "coding"], "status": "online"},
        {"agent_id": "b", "tags": ["smoke"], "status": "offline"},
        {"agent_id": "c", "tags": ["other"], "status": "online"},
        {"agent_id": "d", "tags": ["smoke"], "status": "online"},
    ]
    got = select_invitees(
        agents, required_tags=["smoke"], limit=2, exclude_ids={"a"}, require_online=True
    )
    assert got == ["d"], got  # b offline, c no tag, a excluded

    task = {
        "required_tags": ["smoke"],
        "max_participants": 2,
        "metadata": {"sparse_collab": {"active_cap": 1}},
        "creator_id": "owner",
        "invited_agent_ids": ["d"],
    }
    picked = plan_invites_for_task(task, agents, exclude_ids=set(), mode="tags")
    assert picked == ["a"], picked

    try:
        plan_invites_for_task(
            {"required_tags": ["nosuch"], "max_participants": 1},
            agents,
            mode="tags",
        )
        raise AssertionError("expected empty")
    except MatchEmptyError:
        pass

    # semantic: description beats bare tag-less mismatch
    agents2 = [
        {
            "agent_id": "chef",
            "name": "Chef",
            "description": "cook recipes",
            "tags": [],
            "status": "online",
        },
        {
            "agent_id": "dev",
            "name": "Dev",
            "description": "fix login authentication bugs",
            "tags": [],
            "status": "online",
        },
    ]
    sem_task = {
        "title": "fix login authentication",
        "description": "users cannot sign in",
        "max_participants": 1,
    }
    got_sem = plan_invites_for_task(sem_task, agents2, mode="semantic")
    assert got_sem[0] == "dev", got_sem

    # hybrid + performance: same semantic, higher completion_rate wins
    os.environ["MATCH_PERF_WEIGHT"] = "0.5"
    agents3 = [
        {
            "agent_id": "weak",
            "description": "fix login authentication",
            "tags": ["auth"],
            "status": "online",
            "metadata": {"performance": {"completion_rate": 0.2}},
        },
        {
            "agent_id": "strong",
            "description": "fix login authentication",
            "tags": ["auth"],
            "status": "online",
            "metadata": {"performance": {"completion_rate": 0.95}},
        },
    ]
    perf_task = {
        "title": "fix login authentication",
        "required_tags": ["auth"],
        "max_participants": 1,
    }
    got_p = plan_invites_for_task(perf_task, agents3, mode="hybrid")
    assert got_p[0] == "strong", got_p
    os.environ.pop("MATCH_PERF_WEIGHT", None)

    print("match self-test OK")


if __name__ == "__main__":
    _self_test()
