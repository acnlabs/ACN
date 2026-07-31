"""MVP-2b: semantic rank over Agent Card-ish profile text.

Default engine = stdlib lexical (token overlap + phrase boost), same spirit as
ACN ARD relevance — swap-in HTTP embeddings via env without changing callers.

Env (optional HTTP engine):
  ACN_EMBEDDING_URL   OpenAI-compatible embeddings endpoint
  ACN_EMBEDDING_API_KEY / OPENAI_API_KEY
  ACN_EMBEDDING_MODEL default text-embedding-3-small
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from typing import Any, Protocol

from summary import redact_text

_TOKEN = re.compile(r"[a-z0-9_\u4e00-\u9fff]{2,}", re.I)


def tokenize(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN.finditer(text or "")}


def task_query_text(task: dict[str, Any]) -> str:
    """Desensitized intent string used as the recall query."""
    parts = [
        redact_text(str(task.get("title") or ""), max_len=120),
        redact_text(str(task.get("description") or ""), max_len=240),
    ]
    tags = task.get("required_tags") or task.get("required_skills") or []
    if isinstance(tags, list) and tags:
        parts.append("tags: " + " ".join(str(t) for t in tags if t))
    return " ".join(p for p in parts if p).strip()


def agent_profile_text(agent: dict[str, Any]) -> str:
    """Stable capability blurb — name / description / tags / card snippets."""
    parts: list[str] = [
        str(agent.get("name") or ""),
        str(agent.get("description") or ""),
        " ".join(str(t) for t in (agent.get("tags") or []) if t),
    ]
    card = agent.get("agent_card")
    if isinstance(card, dict):
        parts.append(str(card.get("description") or ""))
        skills = card.get("skills")
        if isinstance(skills, list):
            for s in skills:
                if isinstance(s, dict):
                    parts.append(str(s.get("id") or s.get("name") or ""))
                    parts.append(str(s.get("description") or ""))
                else:
                    parts.append(str(s or ""))
        rq = card.get("representative_queries") or card.get("examples")
        if isinstance(rq, list):
            parts.extend(str(x) for x in rq if x)
    skills = agent.get("skills")
    if isinstance(skills, list):
        for s in skills:
            if isinstance(s, dict):
                parts.append(str(s.get("id") or s.get("name") or ""))
            else:
                parts.append(str(s or ""))
    return " ".join(p for p in parts if p).strip()


def lexical_similarity(query: str, profile: str) -> float:
    """0..1 relevance (ARD-like token overlap + phrase boost)."""
    q = (query or "").strip().lower()
    p = (profile or "").strip().lower()
    qt = tokenize(q)
    if not qt:
        return 0.0
    pt = tokenize(p)
    if not pt:
        return 0.0
    overlap = qt & pt
    score = len(overlap) / len(qt)
    if q and q in p:
        score = min(1.0, score + 0.15)
    return float(score)


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class LexicalEmbedder:
    """Deterministic hashed bag-of-words (stdlib). Good for smoke / offline."""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in tokenize(text):
                vec[hash(tok) % self.dim] += 1.0
            out.append(_l2_normalize(vec))
        return out


class HttpEmbedder:
    """OpenAI-compatible embeddings API."""

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        model: str = "text-embedding-3-small",
        timeout: float = 30,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        body = json.dumps({"model": self.model, "input": texts}).encode()
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode())
        data = payload.get("data") or []
        # API may return unsorted; sort by index
        data = sorted(data, key=lambda r: int(r.get("index") or 0))
        return [_l2_normalize(list(r.get("embedding") or [])) for r in data]


def resolve_embedder() -> tuple[str, Embedder]:
    url = (os.environ.get("ACN_EMBEDDING_URL") or "").strip()
    key = (
        os.environ.get("ACN_EMBEDDING_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()
    model = (os.environ.get("ACN_EMBEDDING_MODEL") or "text-embedding-3-small").strip()
    if url and key:
        return "http", HttpEmbedder(url, key, model=model)
    return "lexical", LexicalEmbedder()


def _l2_normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    if n <= 0:
        return vec
    return [x / n for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return float(sum(x * y for x, y in zip(a, b)))


def semantic_scores(
    query: str,
    agents: list[dict[str, Any]],
    *,
    embedder: Embedder | None = None,
    engine_name: str | None = None,
) -> tuple[str, list[float]]:
    """Return (engine_name, per-agent scores in 0..1)."""
    if embedder is None:
        engine_name, embedder = resolve_embedder()
    else:
        engine_name = engine_name or "custom"

    profiles = [agent_profile_text(a) for a in agents]
    if engine_name == "lexical" and isinstance(embedder, LexicalEmbedder):
        # Prefer ARD-like overlap for interpretability in default engine
        return engine_name, [lexical_similarity(query, p) for p in profiles]

    try:
        vectors = embedder.embed([query, *profiles])
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        # Fail soft to lexical
        return "lexical_fallback", [lexical_similarity(query, p) for p in profiles]
    if len(vectors) != 1 + len(profiles):
        return "lexical_fallback", [lexical_similarity(query, p) for p in profiles]
    qv = vectors[0]
    return engine_name, [max(0.0, min(1.0, cosine(qv, v))) for v in vectors[1:]]


def rank_agents(
    query: str,
    agents: list[dict[str, Any]],
    *,
    tag_scores: list[float] | None = None,
    perf_scores: list[float | None] | None = None,
    tag_weight: float = 0.35,
    semantic_weight: float = 0.65,
    perf_weight: float = 0.0,
    min_score: float = 0.05,
    embedder: Embedder | None = None,
) -> list[tuple[str, float, float, float]]:
    """Return (agent_id, combined, semantic, tag) sorted best-first.

    ``perf_scores[i] is None`` → omit performance term for that row
    (cold start does not drag the score down).
    """
    if not agents:
        return []
    engine, sem = semantic_scores(query, agents, embedder=embedder)
    _ = engine
    tags = tag_scores or [0.0] * len(agents)
    if len(tags) != len(agents):
        tags = [0.0] * len(agents)
    perfs: list[float | None]
    if perf_scores is None or len(perf_scores) != len(agents):
        perfs = [None] * len(agents)
    else:
        perfs = list(perf_scores)
    tw = max(0.0, tag_weight)
    sw = max(0.0, semantic_weight)
    pw = max(0.0, perf_weight)
    ranked: list[tuple[str, float, float, float, int]] = []
    for i, agent in enumerate(agents):
        aid = str(agent.get("agent_id") or "").strip()
        if not aid:
            continue
        t = float(tags[i])
        s = float(sem[i])
        p = perfs[i]
        num = tw * t + sw * s
        den = tw + sw
        if pw > 0 and p is not None:
            num += pw * float(p)
            den += pw
        combined = num / den if den > 0 else 0.0
        if combined < min_score and t <= 0:
            continue
        ranked.append((aid, combined, s, t, i))
    ranked.sort(key=lambda r: (-r[1], r[4]))
    return [(a, c, s, t) for a, c, s, t, _ in ranked]


def _self_test() -> None:
    q = "fix login page authentication bug"
    good = {
        "agent_id": "g",
        "name": "Auth fixer",
        "description": "I fix login and authentication bugs",
        "tags": ["coding"],
        "status": "online",
    }
    bad = {
        "agent_id": "b",
        "name": "Chef bot",
        "description": "I write recipes",
        "tags": ["cooking"],
        "status": "online",
    }
    assert lexical_similarity(q, agent_profile_text(good)) > lexical_similarity(
        q, agent_profile_text(bad)
    )
    ranked = rank_agents(q, [bad, good], tag_scores=[0.0, 0.0], min_score=0.01)
    assert ranked[0][0] == "g", ranked
    # hashed embedder cosine path
    eng = LexicalEmbedder()
    name, scores = semantic_scores(q, [good, bad], embedder=eng, engine_name="hash")
    assert name == "hash" and len(scores) == 2
    print("semantic self-test OK")


if __name__ == "__main__":
    _self_test()
