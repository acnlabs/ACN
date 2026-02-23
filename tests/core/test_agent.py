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
            skills=["task-planning", "code-generation"],
        )

        assert agent.has_skill("task-planning") is True
        assert agent.has_skill("code-generation") is True
        assert agent.has_skill("data-analysis") is False

    def test_has_all_skills(self):
        """Test checking multiple skills"""
        agent = Agent(
            agent_id="agent-123",
            owner="user-456",
            name="Test Agent",
            endpoint="https://agent.example.com",
            skills=["task-planning", "code-generation", "data-analysis"],
        )

        assert agent.has_all_skills(["task-planning", "code-generation"]) is True
        assert agent.has_all_skills(["task-planning", "missing-skill"]) is False

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
            skills=["task-planning"],
            subnet_ids=["public"],
        )

        data = agent.to_dict()

        assert data["agent_id"] == "agent-123"
        assert data["owner"] == "user-456"
        assert data["name"] == "Test Agent"
        assert data["skills"] == ["task-planning"]
        assert data["subnet_ids"] == ["public"]

    def test_from_dict(self):
        """Test deserialization from dict"""
        data = {
            "agent_id": "agent-123",
            "owner": "user-456",
            "name": "Test Agent",
            "endpoint": "https://agent.example.com",
            "status": "online",
            "skills": ["task-planning"],
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
        assert agent.skills == ["task-planning"]

