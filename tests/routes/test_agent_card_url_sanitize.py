"""Tests for ``GET /api/v1/agents/{id}/.well-known/agent-card.json``
top-level ``url`` sanitization.

Phase 1 review finding (L422):
    The well-known agent card is the *public* discovery surface
    described by the A2A v0.3.0 spec — a successor to the
    ``robots.txt`` / OpenAPI ``servers[]`` patterns. Pre-fix, the
    handler returned whatever the agent submitted at registration
    verbatim, and the auto-generated fallback wrote ``url =
    agent.endpoint``. Either path leaked the agent's real backend
    URL, which is exactly the data the ACN proxy was designed to
    hide. Once a caller has it, ``communication_policy``
    enforcement is moot — they bypass ACN entirely.

The fix rewrites the top-level ``url`` to the ACN proxy address
(``{base_url}/api/v1/agents/{id}``) on both the registration-card
path and the auto-generated fallback. Phase 1 deliberately
*doesn't* deep-walk nested fields (``services[]``,
``additionalInterfaces[]``, etc.) — that's a Phase 2 expansion
once we've surveyed what third-party cards actually embed (see
docs/features/acn-communication-economic-model.md L433).

These tests pin:

* the registration card has its ``url`` overwritten with the
  proxy URL,
* unrelated top-level fields (``name``, ``description``,
  ``tags``, ``capabilities``, ...) survive unchanged — we
  *sanitize*, not strip,
* the auto-generated fallback never embeds the real endpoint,
* the rewrite is non-mutating w.r.t. the cached entity (a second
  request observes the same original card content), so a second
  caller can't accidentally see a stale or doubly-rewritten URL.

Why we don't test "no real endpoint anywhere in response" via
recursive walk: that's the Phase-2 contract. Phase 1 explicitly
limits the guarantee to the top-level ``url``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException
from acn.routes.dependencies import get_agent_service, limiter


@pytest.fixture(autouse=True)
def _reset_state():
    limiter.enabled = False
    yield
    limiter.enabled = True
    app.dependency_overrides.clear()


def _make_agent(
    *,
    agent_id: str = "agent-target",
    endpoint: str = "https://real-backend.example.com:9443/a2a",
    agent_card: dict | None = None,
    name: str = "Target",
    description: str = "test agent",
    tags: list[str] | None = None,
):
    """Stand-in for the Agent entity.

    Only the attributes ``get_agent_card`` reads are populated so
    tests stay independent of unrelated entity churn.
    """
    a = MagicMock()
    a.agent_id = agent_id
    a.endpoint = endpoint
    a.agent_card = agent_card
    a.name = name
    a.description = description
    a.tags = tags or []
    return a


@pytest.fixture
def stub_agent_service():
    svc = AsyncMock()
    svc._agent = None  # filled per-test

    async def _get_agent(agent_id: str):
        if svc._agent is None or svc._agent.agent_id != agent_id:
            raise AgentNotFoundException(agent_id)
        return svc._agent

    svc.get_agent = AsyncMock(side_effect=_get_agent)
    return svc


def _wire(svc) -> None:
    app.dependency_overrides[get_agent_service] = lambda: svc


def _proxy_url(agent_id: str) -> str:
    """Reconstruct the proxy URL the route is expected to use.

    ``settings.gateway_base_url`` is unset in the test config, so
    the route falls back to ``http://localhost:{port}``. We pull
    the same default so this stays a contract test, not a config
    snapshot test.
    """
    from acn.routes.dependencies import settings

    base_url = settings.gateway_base_url or f"http://localhost:{settings.port}"
    return f"{base_url}/api/v1/agents/{agent_id}"


# --------------------------------------------------------------------------- #
# Caller-supplied card: url overwritten, other fields preserved
# --------------------------------------------------------------------------- #


class TestRegisteredCardSanitization:
    def test_top_level_url_rewritten_to_proxy(self, stub_agent_service):
        """Pin the core invariant: the URL clients dial *cannot*
        be the agent's real endpoint, no matter what the agent
        registered."""
        original = {
            "name": "RegisteredName",
            "version": "1.2.3",
            "url": "https://real-backend.example.com:9443/a2a",
            "description": "registered description",
            "tags": [{"id": "search", "name": "Search"}],
        }
        stub_agent_service._agent = _make_agent(agent_card=original)
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/agents/agent-target/.well-known/agent-card.json")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["url"] == _proxy_url("agent-target")
        # Defense-in-depth: explicitly assert the real endpoint is
        # NOT present at the top-level url. The previous assertion
        # already implies this, but we want a test that fails
        # *loudly* if someone "fixes" the rewrite to be a no-op.
        assert body["url"] != original["url"]
        assert "real-backend.example.com" not in body["url"]

    def test_unrelated_top_level_fields_preserved(self, stub_agent_service):
        """We sanitize one field; everything else must round-trip
        verbatim, otherwise we'd be a lossy proxy of A2A discovery.

        Concretely: name / description / version / capabilities /
        tags carry the agent's identity and skill metadata that
        clients use to *decide* whether to call the agent. Mangling
        those would break legitimate discovery."""
        original = {
            "name": "RegisteredName",
            "version": "1.2.3",
            "url": "https://real-backend.example.com/a2a",
            "description": "ABC",
            "capabilities": {"streaming": True},
            "tags": [{"id": "search"}, {"id": "summarize"}],
            "extra_custom_field": {"nested": [1, 2, 3]},
        }
        stub_agent_service._agent = _make_agent(agent_card=original)
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/agents/agent-target/.well-known/agent-card.json")

        body = r.json()
        # All non-``url`` keys must match the original byte-for-byte.
        for key in ("name", "version", "description", "capabilities", "tags",
                    "extra_custom_field"):
            assert body[key] == original[key], f"field {key!r} was mutated"

    def test_url_added_when_registration_card_omits_it(self, stub_agent_service):
        """A2A v0.3.0 cards SHOULD have a ``url`` but third-party
        generators sometimes omit it. Phase 1 guarantees the
        public response always carries the proxy URL — a missing
        ``url`` is filled in (not left absent)."""
        original = {"name": "NoUrlCard", "tags": []}
        stub_agent_service._agent = _make_agent(agent_card=original)
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/agents/agent-target/.well-known/agent-card.json")

        body = r.json()
        assert body["url"] == _proxy_url("agent-target")

    def test_rewrite_does_not_mutate_cached_entity(self, stub_agent_service):
        """The repository is allowed to hand the route a *shared*
        dict reference (e.g. an in-memory cached entity). If the
        route mutated it in-place, every subsequent caller would
        see the rewritten URL persisted on the cache — fine for
        the public path but catastrophic if the same entity were
        ever consumed for an internal operation that needs the
        real endpoint.

        Pin: after the response is built, the ``agent.agent_card``
        reference still carries the original real URL.
        """
        original = {"name": "X", "url": "https://real-backend.example.com/a2a"}
        agent = _make_agent(agent_card=original)
        stub_agent_service._agent = agent
        _wire(stub_agent_service)

        with TestClient(app) as client:
            client.get("/api/v1/agents/agent-target/.well-known/agent-card.json")

        # The shared dict reference is untouched; defensive copy
        # was made before rewriting.
        assert agent.agent_card["url"] == "https://real-backend.example.com/a2a"
        assert agent.agent_card is original  # same reference, unmodified


# --------------------------------------------------------------------------- #
# Auto-generated fallback: never embed real endpoint
# --------------------------------------------------------------------------- #


class TestFallbackCardSanitization:
    def test_autogenerated_card_uses_proxy_url(self, stub_agent_service):
        """The fallback path runs when the agent didn't register a
        card. Pre-fix this wrote ``url=agent.endpoint`` directly,
        leaking the real backend URL with zero attacker effort."""
        # No agent_card → triggers fallback branch.
        stub_agent_service._agent = _make_agent(
            agent_card=None,
            tags=["coding", "search"],
        )
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/agents/agent-target/.well-known/agent-card.json")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["url"] == _proxy_url("agent-target")
        # Real endpoint must not appear anywhere in the (single-
        # level) fallback shape. We don't recursively walk, but
        # the auto-generated card has a flat structure so a
        # top-level scan is sufficient to catch a regression.
        flat = str(body)
        assert "real-backend.example.com" not in flat
        assert ":9443" not in flat


# --------------------------------------------------------------------------- #
# 404 — agent not found
# --------------------------------------------------------------------------- #


class TestAgentNotFound:
    def test_unknown_agent_returns_404(self, stub_agent_service):
        # No agent set on the stub → service raises
        # ``AgentNotFoundException`` for any ID.
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/does-not-exist/.well-known/agent-card.json"
            )

        assert r.status_code == 404, r.text
