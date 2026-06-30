"""Tests for the ARD (Agentic Resource Discovery) compatibility layer.

Covers the two MVP endpoints in ``acn/routes/ard.py``:
- ``GET /.well-known/ai-catalog.json`` — static capability manifest.
- ``POST /search`` — ranked agent discovery with structured filters.

Spec: https://agenticresourcediscovery.org/spec/ (v0.9 draft).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.entities.agent import Agent
from acn.identity import resolve_publisher_domain
from acn.routes.dependencies import get_agent_service

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _agent(agent_id: str, name: str, description: str, tags: list[str], **meta) -> Agent:
    return Agent(
        agent_id=agent_id,
        name=name,
        description=description,
        tags=tags,
        metadata=meta.get("metadata", {}),
    )


@pytest.fixture
def flight_agent() -> Agent:
    return _agent(
        "agent-flight",
        "Flight Booking Agent",
        "Book flights to anywhere in the world",
        ["travel", "booking"],
    )


@pytest.fixture
def weather_agent() -> Agent:
    return _agent(
        "agent-weather",
        "Weather Bot",
        "Live weather forecasts",
        ["weather"],
    )


@pytest.fixture
def hidden_agent() -> Agent:
    return _agent(
        "agent-hidden",
        "Flight Internal Bot",
        "Internal flight tooling",
        ["travel"],
        metadata={"visibility": "hidden"},
    )


@pytest.fixture
def repq_agent() -> Agent:
    # Name/description deliberately avoid the word "passport" so a match on
    # "renew my passport" can ONLY come from representativeQueries.
    return _agent(
        "agent-docs",
        "Document Helper",
        "Helps with official paperwork",
        ["documentation"],
        metadata={
            "representative_queries": [
                "renew my passport",
                "fill out a visa application",
            ]
        },
    )


@pytest.fixture
def stub_agent_service(flight_agent, weather_agent, hidden_agent, repq_agent):
    svc = AsyncMock()
    all_agents = [flight_agent, weather_agent, hidden_agent, repq_agent]

    async def _search(tags=None, status="all", slug=None):
        return list(all_agents)

    svc.search_agents = AsyncMock(side_effect=_search)
    return svc


@pytest.fixture
def client(stub_agent_service):
    app.dependency_overrides[get_agent_service] = lambda: stub_agent_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class TestCatalogManifest:
    def test_manifest_advertises_registry(self, client):
        r = client.get("/.well-known/ai-catalog.json")
        assert r.status_code == 200
        body = r.json()
        assert body["specVersion"] == "1.0"
        assert "host" in body
        registry_entries = [
            e for e in body["entries"] if e["type"] == "application/ai-registry+json"
        ]
        assert len(registry_entries) == 1
        entry = registry_entries[0]
        assert entry["identifier"].startswith("urn:air:")
        assert entry["url"]  # base URL clients POST /search against


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_text_required(self, client):
        r = client.post("/search", json={"query": {}})
        assert r.status_code == 400
        assert r.json()["error_code"] == "invalid_request"

    def test_ranks_relevant_agent_and_drops_noise(self, client):
        r = client.post("/search", json={"query": {"text": "book me a flight"}})
        assert r.status_code == 200
        results = r.json()["results"]
        ids = [e["identifier"] for e in results]
        # The flight agent matches; the weather agent has zero overlap and
        # is dropped by the relevance cutoff.
        assert any("agent-flight" in i for i in ids)
        assert all("agent-weather" not in i for i in ids)

    def test_entry_shape(self, client):
        r = client.post("/search", json={"query": {"text": "flight booking"}})
        assert r.status_code == 200
        entry = r.json()["results"][0]
        publisher = resolve_publisher_domain()
        assert entry["identifier"] == f"urn:air:{publisher}:agent:agent-flight"
        assert entry["type"] == "application/a2a-agent-card+json"
        assert entry["url"].endswith(
            "/api/v1/agents/agent-flight/.well-known/agent-card.json"
        )
        assert entry["capabilities"] == ["travel", "booking"]
        assert 0 < entry["score"] <= 100

    def test_hidden_agent_not_discoverable(self, client):
        r = client.post("/search", json={"query": {"text": "flight"}})
        assert r.status_code == 200
        ids = [e["identifier"] for e in r.json()["results"]]
        assert all("agent-hidden" not in i for i in ids)

    def test_filter_type_excludes_non_a2a(self, client):
        r = client.post(
            "/search",
            json={
                "query": {
                    "text": "flight",
                    "filter": {"type": ["application/mcp-server-card+json"]},
                }
            },
        )
        assert r.status_code == 200
        assert r.json()["results"] == []

    def test_filter_tags_or_semantics(self, client):
        r = client.post(
            "/search",
            json={"query": {"text": "weather forecast", "filter": {"tags": ["weather"]}}},
        )
        assert r.status_code == 200
        ids = [e["identifier"] for e in r.json()["results"]]
        assert any("agent-weather" in i for i in ids)
        assert all("agent-flight" not in i for i in ids)

    def test_unsupported_filter_field_400(self, client):
        r = client.post(
            "/search",
            json={"query": {"text": "flight", "filter": {"price": ["low"]}}},
        )
        assert r.status_code == 400
        assert r.json()["error_code"] == "invalid_request"

    def test_pagination(self, client):
        # "agent" appears in both flight + weather haystacks (name/desc),
        # so both score > 0 and are paginated.
        first = client.post(
            "/search", json={"query": {"text": "agent bot"}, "pageSize": 1}
        )
        assert first.status_code == 200
        body1 = first.json()
        assert len(body1["results"]) == 1
        assert body1["pageToken"]

        second = client.post(
            "/search",
            json={
                "query": {"text": "agent bot"},
                "pageSize": 1,
                "pageToken": body1["pageToken"],
            },
        )
        assert second.status_code == 200
        body2 = second.json()
        assert len(body2["results"]) == 1
        assert body1["results"][0]["identifier"] != body2["results"][0]["identifier"]

    def test_invalid_page_token_400(self, client):
        r = client.post(
            "/search",
            json={"query": {"text": "flight"}, "pageToken": "!!!not-base64!!!"},
        )
        assert r.status_code == 400
        assert r.json()["error_code"] == "invalid_request"

    def test_representative_queries_match_and_emit(self, client):
        # The query only overlaps the agent's representativeQueries — neither
        # its name nor description contains "passport".
        r = client.post("/search", json={"query": {"text": "renew my passport"}})
        assert r.status_code == 200
        results = r.json()["results"]
        docs = [e for e in results if "agent-docs" in e["identifier"]]
        assert docs, "representativeQueries should make the agent discoverable"
        entry = docs[0]
        assert entry["representativeQueries"] == [
            "renew my passport",
            "fill out a visa application",
        ]
        assert entry["score"] > 0


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


class TestObservability:
    """The adapter records a bounded (endpoint, outcome) counter on every
    call. We assert the *wiring* (correct outcome per path) by intercepting
    ``_record_request`` — no Redis/event-loop coupling in the test."""

    @staticmethod
    def _capture(monkeypatch) -> list[tuple[str, str]]:
        import acn.routes.ard as ard_mod

        calls: list[tuple[str, str]] = []

        async def _fake(metrics, *, endpoint: str, outcome: str) -> None:
            calls.append((endpoint, outcome))

        monkeypatch.setattr(ard_mod, "_record_request", _fake)
        return calls

    def test_manifest_records_ok(self, client, monkeypatch):
        calls = self._capture(monkeypatch)
        assert client.get("/.well-known/ai-catalog.json").status_code == 200
        assert ("manifest", "ok") in calls

    def test_search_records_ok(self, client, monkeypatch):
        calls = self._capture(monkeypatch)
        assert client.post(
            "/search", json={"query": {"text": "book me a flight"}}
        ).status_code == 200
        assert ("search", "ok") in calls

    def test_search_records_empty(self, client, monkeypatch):
        calls = self._capture(monkeypatch)
        # A token that overlaps no agent haystack → zero results → "empty".
        assert client.post(
            "/search", json={"query": {"text": "zzzznomatchqqq"}}
        ).status_code == 200
        assert ("search", "empty") in calls

    def test_search_records_invalid(self, client, monkeypatch):
        calls = self._capture(monkeypatch)
        assert client.post("/search", json={"query": {}}).status_code == 400
        assert ("search", "invalid") in calls
