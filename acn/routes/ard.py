"""ARD (Agentic Resource Discovery) compatibility layer.

Exposes ACN's agent registry as an ARD discovery service so that any
ARD-compliant client (GitHub Agent Finder, Hugging Face Discover, …)
can find ACN-registered agents without knowing anything about ACN's
native API.

Spec: https://agenticresourcediscovery.org/spec/ (v0.9 draft).

Scope (MVP):
- ``GET /.well-known/ai-catalog.json`` — static capability manifest
  advertising this deployment as a searchable ``application/ai-registry+json``.
- ``POST /search`` — natural-language + structured search returning ranked
  ARD catalog entries.

This is a thin *adapter* over the existing ``AgentService.search_agents``;
it does not touch any ACN business logic. ARD is discovery-only — clients
invoke the resource through its own A2A mechanism (the ``url`` in each
entry points at the agent's A2A Agent Card), exactly as the spec mandates
(ARD §3.6 "discovery sits before invocation").

trustManifest mapping (ERC-8004 identity / reputation → ARD trust layer)
is intentionally deferred to a later iteration; entries here carry only
the baseline discovery fields.
"""

import base64
import json
import re
from typing import Any
from urllib.parse import quote

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from ..config import get_settings
from ..core.entities.agent import Agent
from ..core.errors import ACN_DEFAULT_RESPONSES, ACNHTTPError, ErrorCode
from ..identity import build_agent_urn, resolve_publisher_domain
from .dependencies import AgentServiceDep, MetricsDep, limiter

router = APIRouter(tags=["ard"], responses=ACN_DEFAULT_RESPONSES)
logger = structlog.get_logger()
settings = get_settings()

# Media type ACN agents are published as. Every ACN agent resolves to an
# A2A Agent Card, so this is the single type we emit and filter on.
_ACN_ENTRY_TYPE = "application/a2a-agent-card+json"
_AI_REGISTRY_TYPE = "application/ai-registry+json"

# Relevance cutoff (ARD §7.2 / §7.3): entries scoring at or below this are
# dropped from the matched set. Kept deliberately low (the floor is "shares
# at least one query token") so honest discovery isn't starved, while pure
# noise (zero overlap) never surfaces.
_RELEVANCE_CUTOFF = 0

# ARD §4.2 recommends 2–5 representative queries per entry; cap emission so a
# misconfigured agent can't bloat the catalog with hundreds of strings.
_MAX_REPRESENTATIVE_QUERIES = 5

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _registry_base_url() -> str:
    """Base URL clients use to reach this registry's ARD endpoints."""
    return settings.gateway_base_url.rstrip("/")


def _agent_card_url(agent_id: str) -> str:
    """Public A2A Agent Card URL for an agent (the entry's ``url``)."""
    return (
        f"{_registry_base_url()}/api/v1/agents/"
        f"{quote(agent_id, safe='')}/.well-known/agent-card.json"
    )


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _representative_queries(agent: Agent) -> list[str]:
    """Extract an agent's ARD ``representativeQueries`` from its metadata.

    Agents declare sample natural-language queries (ARD §4.2) under
    ``metadata.representative_queries`` (snake_case, ACN convention) or
    ``metadata.representativeQueries`` (camelCase, ARD-native) — both are
    accepted so registrants can use whichever they reach for. Non-string
    entries are dropped and the list is capped at
    ``_MAX_REPRESENTATIVE_QUERIES``.
    """
    meta = agent.metadata or {}
    raw = meta.get("representative_queries")
    if raw is None:
        raw = meta.get("representativeQueries")
    if not isinstance(raw, list):
        return []
    queries = [q.strip() for q in raw if isinstance(q, str) and q.strip()]
    return queries[:_MAX_REPRESENTATIVE_QUERIES]


def _relevance_score(query_text: str, agent: Agent) -> int:
    """Compute a 0–100 semantic-relevance score (ARD §7.2).

    MVP heuristic: fraction of query tokens that appear across the
    agent's name + description + tags + representativeQueries, with a
    boost when the full query phrase is a substring of that haystack.
    This is a *relevance* signal only — never a trust/safety rating (ARD
    is explicit about this). A future iteration can swap this for vector
    similarity without changing the endpoint contract.
    """
    query_tokens = _tokenize(query_text)
    if not query_tokens:
        return 50
    haystack = " ".join(
        [
            agent.name or "",
            agent.description or "",
            " ".join(agent.tags or []),
            " ".join(_representative_queries(agent)),
        ]
    ).lower()
    haystack_tokens = _tokenize(haystack)
    if not haystack_tokens:
        return 0
    overlap = query_tokens & haystack_tokens
    score = round(len(overlap) / len(query_tokens) * 100)
    # Phrase boost: a literal substring match is a strong relevance signal
    # the bag-of-words fraction underweights for multi-word queries.
    if query_text.strip().lower() in haystack:
        score = min(100, score + 15)
    return int(score)


def _agent_to_catalog_entry(
    agent: Agent,
    *,
    publisher: str,
    source: str,
    score: int | None = None,
) -> dict[str, Any]:
    """Map an ACN ``Agent`` to an ARD catalog entry (ARD §4.2).

    Uses ``url`` (not ``data``) per the Value-or-Reference rule (§3.4):
    the agent's live A2A Agent Card is the authoritative artifact.
    """
    tags = list(agent.tags or [])
    entry: dict[str, Any] = {
        "identifier": build_agent_urn(agent.agent_id, publisher=publisher),
        "displayName": agent.name,
        "type": _ACN_ENTRY_TYPE,
        "url": _agent_card_url(agent.agent_id),
        "source": source,
    }
    if agent.description:
        entry["description"] = agent.description
    if tags:
        # ``tags`` are the ACN skill identifiers; expose them both as ARD
        # ``tags`` (filtering keywords) and ``capabilities`` (fast skill
        # filter without a full artifact fetch, §4.2).
        entry["tags"] = tags
        entry["capabilities"] = tags
    representative_queries = _representative_queries(agent)
    if representative_queries:
        # §4.2: lets indexing registries build semantic embeddings without
        # fetching the full artifact.
        entry["representativeQueries"] = representative_queries
    if score is not None:
        entry["score"] = score
    return entry


def _as_list(value: Any) -> list[Any]:
    """Normalize an ARD filter value to a list (a bare scalar = 1-elem list)."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _passes_filter(
    agent: Agent,
    *,
    filter_obj: dict[str, Any],
    publisher: str,
) -> bool:
    """Apply ARD structured filter semantics (§7.1).

    Within a single key values are OR'd; across keys they are AND'd.
    Only the common standard fields are supported; an unsupported field
    path raises so the caller can return a 400 (spec-permitted behavior).
    """
    for key, raw in filter_obj.items():
        wanted = {str(v) for v in _as_list(raw)}
        if not wanted:
            continue
        if key == "type":
            if _ACN_ENTRY_TYPE not in wanted:
                return False
        elif key == "publisher":
            if publisher not in wanted:
                return False
        elif key in ("tags", "capabilities"):
            agent_tags = {str(t) for t in (agent.tags or [])}
            if not (agent_tags & wanted):
                return False
        else:
            raise ACNHTTPError(
                ErrorCode.INVALID_REQUEST,
                status_code=400,
                details={"reason": "unsupported_filter_field", "field": key},
            )
    return True


def _encode_page_token(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"offset": offset}).encode()).decode()


def _decode_page_token(token: str) -> int:
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.encode()).decode())
        offset = int(payload.get("offset", 0))
        return max(offset, 0)
    except Exception as exc:  # noqa: BLE001 — malformed token is a client error
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            status_code=400,
            details={"reason": "invalid_page_token"},
        ) from exc


# ---------------------------------------------------------------------------
# Observability — lightweight adoption signal for the ARD adapter.
#
# Goal (deliberately minimal): answer "are external ARD clients calling us,
# from where, and for what" with data, so future ARD investment is demand-led
# rather than speculative. Two cheap signals:
#   - a bounded Prometheus counter (acn_ard_requests_total) for volume, and
#   - a structured access log carrying the client *source* (UA / IP / referer).
# Both are strictly best-effort: a metrics/Redis hiccup must NEVER turn a
# discovery request into a 500, so every recording path swallows its errors.
# ---------------------------------------------------------------------------

# Cap the logged query so the access log can't be turned into an unbounded
# write sink, while still capturing enough to see *what* clients search for.
_QUERY_LOG_PREVIEW_LEN = 120


def _client_source(request: Request) -> dict[str, str]:
    """Best-effort caller fingerprint for the access log (no PII storage).

    The User-Agent is the high-value signal here — it's how we tell which ARD
    client (GitHub Agent Finder, HF Discover, a crawler, …) is reaching us.
    """
    return {
        "client_ip": request.client.host if request.client else "",
        "user_agent": request.headers.get("user-agent", ""),
        "referer": request.headers.get("referer", ""),
    }


async def _record_request(metrics: Any, *, endpoint: str, outcome: str) -> None:
    """Increment ``acn_ard_requests_total`` — never raises (observability only)."""
    if metrics is None:
        return
    try:
        await metrics.inc_counter(
            "ard_requests_total",
            labels={"endpoint": endpoint, "outcome": outcome},
        )
    except Exception:  # noqa: BLE001 — discovery must not break on a metrics fault
        logger.debug(
            "ard_metric_record_failed", endpoint=endpoint, outcome=outcome, exc_info=True
        )


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ARDQuery(BaseModel):
    """Common ARD query object (§7.1)."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(
        default=None,
        max_length=2000,
        description="Natural-language description of the need.",
    )
    filter: dict[str, Any] | None = Field(
        default=None,
        description="Structured constraints (type, tags, capabilities, publisher).",
    )


class ARDSearchRequest(BaseModel):
    """POST /search request body (§7.2)."""

    model_config = ConfigDict(extra="ignore")

    query: ARDQuery
    federation: str = Field(
        default="auto",
        description="auto | referrals | none. ACN runs no upstreams yet, so "
        "all modes return an empty referrals list.",
    )
    pageSize: int = Field(default=10, ge=1, le=100)
    pageToken: str | None = None


class ARDCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    identifier: str
    displayName: str
    type: str
    url: str


class ARDSearchResponse(BaseModel):
    results: list[ARDCatalogEntry]
    referrals: list[ARDCatalogEntry] = Field(default_factory=list)
    pageToken: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/.well-known/ai-catalog.json")
@limiter.limit("60/minute")
async def ard_catalog_manifest(request: Request, metrics: MetricsDep = None):
    """ARD static capability manifest (§4.1).

    Advertises this deployment as a dynamic, searchable Agent Registry.
    ARD clients read the ``application/ai-registry+json`` entry's ``url``
    as the base for ``POST /search``.
    """
    base = _registry_base_url()
    publisher = resolve_publisher_domain()
    await _record_request(metrics, endpoint="manifest", outcome="ok")
    logger.info("ard_manifest", **_client_source(request))
    return {
        "specVersion": "1.0",
        "host": {
            "displayName": settings.service_name,
            "identifier": publisher,
            "documentationUrl": f"{base}/skill.md",
        },
        "entries": [
            {
                "identifier": f"urn:air:{publisher}:registry:acn",
                "displayName": "ACN Agent Registry",
                "type": _AI_REGISTRY_TYPE,
                "url": base,
                "description": (
                    "Searchable registry of agents on the Agent Collaboration "
                    "Network. POST /search with an ARD query to discover agents."
                ),
                "tags": ["registry", "search", "agents"],
            }
        ],
    }


@router.post("/search", response_model=ARDSearchResponse)
@limiter.limit("60/minute")
async def ard_search(
    request: Request,
    body: ARDSearchRequest,
    agent_service: AgentServiceDep = None,
    metrics: MetricsDep = None,
):
    """ARD search (§7.2): natural-language + structured agent discovery.

    Returns ACN agents as ranked ARD catalog entries. ``query.text`` is
    required for Search; ``query.filter`` is optional. Federation is
    accepted but ACN currently runs no upstream registries, so
    ``referrals`` is always empty regardless of mode.
    """
    publisher = resolve_publisher_domain()
    source = _registry_base_url()
    filter_obj = body.query.filter or {}

    # All client-error (400) paths live inside this block so they can be
    # recorded as outcome="invalid" before re-raising — observability must
    # never swallow the error itself, only count it.
    try:
        text = (body.query.text or "").strip()
        if not text:
            raise ACNHTTPError(
                ErrorCode.INVALID_REQUEST,
                status_code=400,
                details={"reason": "query.text_required_for_search"},
            )

        # Discovery is about the *existence* of a capability, not momentary
        # liveness — index every registered agent (status="all") and let the
        # client handle invocation-time reachability, mirroring how a web
        # search engine indexes pages regardless of current uptime.
        agents = await agent_service.search_agents(tags=None, status="all")

        # Data-hygiene: agents without an explicit visibility tag are treated
        # as production ("real"); hidden / spam / archived agents never surface
        # in public discovery (matches the registry list endpoint contract).
        agents = [
            a for a in agents if (a.metadata or {}).get("visibility", "real") == "real"
        ]

        # Structured filter (may raise 400 on an unsupported field path).
        agents = [
            a for a in agents if _passes_filter(a, filter_obj=filter_obj, publisher=publisher)
        ]

        # Score + relevance cutoff, then rank by descending relevance. Ties are
        # broken by name for deterministic, stable pagination.
        scored: list[tuple[int, Agent]] = []
        for agent in agents:
            score = _relevance_score(text, agent)
            if score > _RELEVANCE_CUTOFF:
                scored.append((score, agent))
        scored.sort(key=lambda pair: (-pair[0], pair[1].name or ""))

        offset = _decode_page_token(body.pageToken) if body.pageToken else 0
        window = scored[offset : offset + body.pageSize]

        results = [
            _agent_to_catalog_entry(agent, publisher=publisher, source=source, score=score)
            for score, agent in window
        ]

        next_token = (
            _encode_page_token(offset + body.pageSize)
            if offset + body.pageSize < len(scored)
            else None
        )
    except ACNHTTPError:
        await _record_request(metrics, endpoint="search", outcome="invalid")
        raise

    await _record_request(
        metrics, endpoint="search", outcome="ok" if results else "empty"
    )
    logger.info(
        "ard_search",
        **_client_source(request),
        federation=body.federation,
        page_size=body.pageSize,
        has_filter=bool(filter_obj),
        query_len=len(text),
        query_preview=text[:_QUERY_LOG_PREVIEW_LEN],
        matched=len(scored),
        returned=len(results),
    )

    return ARDSearchResponse(results=results, referrals=[], pageToken=next_token)
