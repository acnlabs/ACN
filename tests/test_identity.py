"""Unit tests for the canonical ARD identity helpers (acn/identity.py)."""

from __future__ import annotations

from acn.core.entities.agent import Agent
from acn.identity import build_agent_urn, parse_agent_urn, resolve_publisher_domain


class TestAgentUrn:
    def test_build_uses_resolved_publisher_by_default(self):
        urn = build_agent_urn("abc-123")
        publisher = resolve_publisher_domain()
        assert urn == f"urn:air:{publisher}:agent:abc-123"

    def test_build_with_explicit_publisher(self):
        assert build_agent_urn("a1", publisher="acme.com") == "urn:air:acme.com:agent:a1"

    def test_roundtrip(self):
        urn = build_agent_urn("uuid-xyz", publisher="example.org")
        assert parse_agent_urn(urn) == ("example.org", "uuid-xyz")

    def test_parse_rejects_non_agent_urn(self):
        assert parse_agent_urn("urn:air:example.org:registry:acn") is None
        assert parse_agent_urn("not-a-urn") is None
        assert parse_agent_urn("") is None
        assert parse_agent_urn(None) is None  # type: ignore[arg-type]

    def test_publisher_is_bare_fqdn(self):
        # No scheme / path leaks into the URN authority anchor (ARD §4.2.1).
        publisher = resolve_publisher_domain()
        assert "://" not in publisher
        assert "/" not in publisher


class TestAgentInfoUrn:
    def test_serializer_populates_urn(self):
        # Direct unit test of the serializer so we don't need HTTP/redis.
        from acn.routes.registry import _agent_entity_to_info

        agent = Agent(agent_id="agent-xyz", name="Test", tags=["coding"])
        info = _agent_entity_to_info(agent, is_online=True, strip_sensitive=True)
        assert info.urn == build_agent_urn("agent-xyz")
