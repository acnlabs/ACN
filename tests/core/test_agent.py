"""Unit Tests for Agent Entity

Tests pure business logic without framework dependencies.
"""

from datetime import datetime

import pytest

from acn.core.entities import Agent, AgentStatus


class TestAgentEntity:
    """Test Agent domain entity"""

    def test_agent_creation(self):
        """Test creating a valid agent"""
        agent = Agent(
            agent_id="agent-123",
            owner="user-456",
            name="Test Agent",
            endpoint="https://agent.example.com",
        )

        assert agent.agent_id == "agent-123"
        assert agent.owner == "user-456"
        assert agent.name == "Test Agent"
        assert agent.endpoint == "https://agent.example.com"
        assert agent.status == AgentStatus.ONLINE
        assert agent.subnet_ids == ["public"]

    def test_agent_validation_empty_id(self):
        """Test agent requires non-empty ID"""
        with pytest.raises(ValueError, match="agent_id cannot be empty"):
            Agent(
                agent_id="",
                owner="user-456",
                name="Test Agent",
                endpoint="https://agent.example.com",
            )

    def test_agent_owner_is_optional(self):
        """Test agent allows empty or None owner (autonomous agents)"""
        agent = Agent(
            agent_id="agent-123",
            owner="",
            name="Test Agent",
            endpoint="https://agent.example.com",
        )
        assert agent.owner == ""

        agent_no_owner = Agent(
            agent_id="agent-456",
            name="Test Agent 2",
            endpoint="https://agent.example.com",
        )
        assert agent_no_owner.owner is None

    def test_is_online(self):
        """Test is_online check"""
        agent = Agent(
            agent_id="agent-123",
            owner="user-456",
            name="Test Agent",
            endpoint="https://agent.example.com",
            status=AgentStatus.ONLINE,
        )

        assert agent.is_online() is True

        agent.status = AgentStatus.OFFLINE
        assert agent.is_online() is False

    def test_has_skill(self):
        """Test skill checking"""
        agent = Agent(
            agent_id="agent-123",
            owner="user-456",
            name="Test Agent",
            endpoint="https://agent.example.com",
            tags=["task-planning", "code-generation"],
        )

        assert agent.has_tag("task-planning") is True
        assert agent.has_tag("code-generation") is True
        assert agent.has_tag("data-analysis") is False

    def test_has_all_skills(self):
        """Test checking multiple tags"""
        agent = Agent(
            agent_id="agent-123",
            owner="user-456",
            name="Test Agent",
            endpoint="https://agent.example.com",
            tags=["task-planning", "code-generation", "data-analysis"],
        )

        assert agent.has_all_tags(["task-planning", "code-generation"]) is True
        assert agent.has_all_tags(["task-planning", "missing-skill"]) is False

    def test_subnet_management(self):
        """Test subnet add/remove"""
        agent = Agent(
            agent_id="agent-123",
            owner="user-456",
            name="Test Agent",
            endpoint="https://agent.example.com",
            subnet_ids=["public"],
        )

        # Add subnet
        agent.add_to_subnet("private-subnet")
        assert "private-subnet" in agent.subnet_ids

        # Remove subnet
        agent.remove_from_subnet("private-subnet")
        assert "private-subnet" not in agent.subnet_ids

        # Cannot remove last subnet (ensures at least one)
        agent.remove_from_subnet("public")
        assert agent.subnet_ids == ["public"]

    def test_update_heartbeat(self):
        """Test heartbeat update"""
        agent = Agent(
            agent_id="agent-123",
            owner="user-456",
            name="Test Agent",
            endpoint="https://agent.example.com",
        )

        assert agent.last_heartbeat is None

        agent.update_heartbeat()
        assert agent.last_heartbeat is not None
        assert isinstance(agent.last_heartbeat, datetime)

    def test_mark_offline_online(self):
        """Test status transitions"""
        agent = Agent(
            agent_id="agent-123",
            owner="user-456",
            name="Test Agent",
            endpoint="https://agent.example.com",
        )

        assert agent.status == AgentStatus.ONLINE

        agent.mark_offline()
        assert agent.status == AgentStatus.OFFLINE

        agent.mark_online()
        assert agent.status == AgentStatus.ONLINE

    def test_to_dict(self):
        """Test serialization to dict"""
        agent = Agent(
            agent_id="agent-123",
            owner="user-456",
            name="Test Agent",
            endpoint="https://agent.example.com",
            tags=["task-planning"],
            subnet_ids=["public"],
        )

        data = agent.to_dict()

        assert data["agent_id"] == "agent-123"
        assert data["owner"] == "user-456"
        assert data["name"] == "Test Agent"
        assert data["tags"] == ["task-planning"]
        assert data["subnet_ids"] == ["public"]

    def test_from_dict(self):
        """Test deserialization from dict"""
        data = {
            "agent_id": "agent-123",
            "owner": "user-456",
            "name": "Test Agent",
            "endpoint": "https://agent.example.com",
            "status": "online",
            "tags": ["task-planning"],
            "subnet_ids": ["public"],
            "metadata": {},
            "registered_at": "2024-01-01T12:00:00",
            "last_heartbeat": None,
            "wallet_address": None,
            "accepts_payment": False,
            "payment_methods": [],
        }

        agent = Agent.from_dict(data)

        assert agent.agent_id == "agent-123"
        assert agent.owner == "user-456"
        assert agent.status == AgentStatus.ONLINE
        assert agent.tags == ["task-planning"]

    # ========================================================================
    # communication_policy (gateway-level access control, Phase 1)
    # ========================================================================
    #
    # See docs/features/acn-communication-economic-model.md.
    # The default {"mode": "open"} backfill is the contract gateway code
    # depends on — without it every read site would need a None-guard and
    # the "legacy agents stay open" promise becomes implicit instead of
    # explicit. Locking it in here makes accidental regressions loud.

    def test_communication_policy_default_open(self):
        """New agents default to communication_policy={'mode': 'open'} so
        legacy callers and gateway code can rely on the field always being
        a dict with a mode."""
        agent = Agent(agent_id="agent-1", name="Default")
        assert agent.communication_policy == {"mode": "open"}

    def test_communication_policy_explicit_value_preserved(self):
        """An explicitly provided policy must round-trip unchanged — the
        default backfill should only fire for None, not overwrite caller
        intent."""
        policy = {
            "mode": "closed",
            "reject_reason": "Only accepting task-related messages",
        }
        agent = Agent(
            agent_id="agent-1",
            name="Closed",
            communication_policy=policy,
        )
        assert agent.communication_policy == policy

    def test_communication_policy_round_trip(self):
        """to_dict / from_dict must preserve the policy verbatim. Phase 2
        will add allowlist + rate_limit nested fields; this test pins the
        contract so those additions don't silently get dropped."""
        policy = {
            "mode": "closed",
            "reject_reason": "busy",
            "rate_limit": {"max_per_minute_per_sender": 5},
        }
        agent = Agent(
            agent_id="agent-1",
            name="Test",
            communication_policy=policy,
        )

        restored = Agent.from_dict(agent.to_dict())
        assert restored.communication_policy == policy

    def test_communication_policy_empty_dict_backfilled(self):
        """An empty dict (e.g. accidental ``{}`` from a misconfigured caller)
        must be treated as "no policy set" and get the default mode, so the
        gateway never KeyErrors on ``policy['mode']``."""
        agent = Agent(
            agent_id="agent-1",
            name="Empty",
            communication_policy={},
        )
        assert agent.communication_policy == {"mode": "open"}

    def test_communication_policy_partial_payload_filled_with_default_mode(self):
        """A partial policy that forgot ``mode`` (e.g. only ``reject_reason``
        was sent over the wire) keeps caller fields and fills in the default
        mode — gateway code can still read ``policy['mode']`` safely."""
        agent = Agent(
            agent_id="agent-1",
            name="Partial",
            communication_policy={"reject_reason": "busy"},
        )
        assert agent.communication_policy == {
            "mode": "open",
            "reject_reason": "busy",
        }

    def test_communication_policy_legacy_dict_backfilled(self):
        """Dicts produced before this field existed (e.g. cached payloads in
        Redis) must still hydrate, with the missing field defaulting to
        open. Without this, deploying the gateway change would
        retroactively close older agents."""
        legacy = {
            "agent_id": "legacy-1",
            "name": "Legacy",
            "endpoint": "https://legacy.example.com",
            "status": "online",
            "tags": [],
            "subnet_ids": ["public"],
            "metadata": {},
            "registered_at": "2024-01-01T12:00:00",
            "last_heartbeat": None,
            # Note: no communication_policy key
        }
        agent = Agent.from_dict(legacy)
        assert agent.communication_policy == {"mode": "open"}


class TestSocialCardUrl:
    """Cover the SOCIAL.md pointer added on the Agent entity.

    Why these tests live next to the entity (not the route): we
    want the *entity-layer* invariants — validation, round-trip,
    legacy-payload tolerance — pinned independently of any route
    plumbing, because all four entry points (POST /register, POST
    /join, PATCH /social-card-url, raw repository writes during
    migration) eventually funnel through ``Agent.__post_init__``
    and ``Agent.from_dict``.
    """

    def test_default_is_none(self):
        """Agents created without specifying a URL must default
        to ``None``. This is the implicit contract every existing
        caller relies on — adding a new field with a non-None
        default would silently change the meaning of every
        already-written ``Agent(...)`` call."""
        agent = Agent(
            agent_id="agent-1",
            name="No card",
            endpoint="https://example.com",
        )
        assert agent.social_card_url is None

    def test_https_url_accepted(self):
        agent = Agent(
            agent_id="agent-1",
            name="Has card",
            endpoint="https://example.com",
            social_card_url="https://acme.example.com/.well-known/social.md",
        )
        assert agent.social_card_url == (
            "https://acme.example.com/.well-known/social.md"
        )

    def test_http_url_accepted_for_dev(self):
        """Why http:// is allowed: dev / preview environments
        often serve plaintext (localhost, pull-request preview
        URLs without TLS). The route layer enforces production-
        appropriate constraints separately; the entity layer
        stays permissive so unit tests against in-memory fixtures
        don't need to mint TLS certs."""
        agent = Agent(
            agent_id="agent-1",
            name="Dev card",
            endpoint="http://localhost:8080",
            social_card_url="http://localhost:3000/social.md",
        )
        assert agent.social_card_url == "http://localhost:3000/social.md"

    def test_empty_string_normalized_to_none(self):
        """Empty input collapses to ``None`` so downstream code
        can rely on a single sentinel for "no card published"."""
        agent = Agent(
            agent_id="agent-1",
            name="Empty card",
            endpoint="https://example.com",
            social_card_url="   ",
        )
        assert agent.social_card_url is None

    def test_non_http_scheme_rejected(self):
        """Same reasoning as the route layer: the URL is meant
        to be HTTP-fetched. Allowing other schemes here would
        let raw repository writes (migrations, admin scripts)
        bypass route validation."""
        with pytest.raises(ValueError, match="https://"):
            Agent(
                agent_id="agent-1",
                name="Bad scheme",
                endpoint="https://example.com",
                social_card_url="ftp://example.com/social.md",
            )

    def test_round_trip_to_dict_from_dict(self):
        """Round-trip through serialization is the contract Redis
        relies on — agents persist via ``to_dict`` and rehydrate
        via ``from_dict``, so the URL must survive both
        directions byte-identical."""
        original = Agent(
            agent_id="agent-1",
            name="Round trip",
            endpoint="https://example.com",
            social_card_url="https://acme.example.com/social.md",
        )
        rehydrated = Agent.from_dict(original.to_dict())
        assert rehydrated.social_card_url == original.social_card_url

    def test_legacy_dict_without_field_hydrates_to_none(self):
        """Cached payloads written before this field existed
        (Redis hashes mid-migration) must still hydrate cleanly,
        with the missing field defaulting to ``None``. Without
        this, a single deploy would crash every running gateway
        trying to read pre-existing agents."""
        legacy = {
            "agent_id": "legacy-1",
            "name": "Legacy",
            "endpoint": "https://legacy.example.com",
            "status": "online",
            "tags": [],
            "subnet_ids": ["public"],
            "metadata": {},
            "registered_at": "2024-01-01T12:00:00",
            "last_heartbeat": None,
            # Note: no social_card_url key
        }
        agent = Agent.from_dict(legacy)
        assert agent.social_card_url is None
